"""Velesha's agent loop on Google ADK (ADR-0036).

Tools, gates, secrets masking, budget and memory keep their own logic
(`tools.py`, `guardrails.py`); ADK owns the loop, sessions and tracing. The
gates plug in as: a per-message toolset (`tools_for`), before/after callbacks
(masking, usage, loop stop, "did an action really succeed"), and
`skip_summarization` for tools whose output must reach the user verbatim.
"""
import asyncio
import contextvars
import json
import os
import datetime
import pathlib
import re
import time

from google.adk.agents import Agent
from google.adk.agents.run_config import RunConfig
from google.adk.models.lite_llm import LiteLlm
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools import BaseTool
from google.adk.tools.base_toolset import BaseToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset
from google.genai import types
from mcp import StdioServerParameters

import guardrails
import jellyfin_client
import tools as legacy
import users

APP = "velesha"
MAX_LLM_CALLS = 6
SESSION_TTL = 60 * 60  # HA forgets a conversation after minutes; we keep a person's for an hour (ADR-0034)
KEEP_USER_TURNS = 3

_req: contextvars.ContextVar[dict] = contextvars.ContextVar("velesha_req")


def _text(content: types.Content | None) -> str:
    return " ".join(p.text for p in (content.parts if content else []) if getattr(p, "text", None))


def _recent_user_text(ctx, n: int = KEEP_USER_TURNS) -> str:
    """Current message plus the last few user turns from the session — a word
    gate that only looked at the latest message lost multi-turn requests
    (ADR-0029 hit this for Toloka; reminders had the same bug)."""
    if ctx is None:
        return ""
    session = getattr(ctx, "session", None)
    past = [_text(e.content) for e in (session.events if session else []) if e.author == "user"]
    return " ".join([t for t in past if t][-n:] + [_text(ctx.user_content)])


class LegacyTool(BaseTool):
    """One of our JSON-schema tools, run through `tools.call_tool` unchanged."""

    def __init__(self, schema: dict):
        fn = schema["function"]
        super().__init__(name=fn["name"], description=fn["description"])
        self._params = fn.get("parameters") or {"type": "object", "properties": {}}

    def _get_declaration(self):
        return types.FunctionDeclaration(
            name=self.name, description=self.description, parameters_json_schema=self._params)

    async def run_async(self, *, args, tool_context):
        text = _text(tool_context.user_content)
        result = await asyncio.to_thread(legacy.call_tool, self.name, json.dumps(args, ensure_ascii=False), text)
        return {"result": result}


class PerMessageTools(BaseToolset):
    """`tools_for`: which tools this message may use (action tools need an explicit phrase)."""

    async def get_tools(self, readonly_context=None):
        text = _recent_user_text(readonly_context)
        return [LegacyTool(s) for s in legacy.tools_for(text)]

    async def close(self):
        pass

# Jellyfin comes from the jellyfin-mcp server (ADR-0038), not from our own code.
# Two locks: the tool filter below decides what the model may see for this
# message, and `_jellyfin_guard` re-checks every state-changing call.
MCP_BIN = os.environ.get("JELLYFIN_MCP_BIN") or str(pathlib.Path(__file__).parent / "bin" / "jellyfin-mcp")
JF_READ = {"jellyfin_search", "jellyfin_browse", "jellyfin_get_item", "jellyfin_recommendations",
           "jellyfin_people", "jellyfin_tv_shows", "jellyfin_analytics", "jellyfin_sessions"}
JF_PLAY = {"jellyfin_play", "jellyfin_playback_control"}  # only with a command phrase
JF_MARK = {"jellyfin_user_data"}  # only with a mark/unmark phrase
JF_WRITE = JF_PLAY | JF_MARK
PLAY_COMMANDS = {"Pause", "Unpause", "Stop", "NextTrack", "PreviousTrack", "Seek", "Mute", "Unmute", "ToggleMute", "SetVolume"}
MARK_ACTIONS = {"mark_played", "mark_unplayed", "get_user_data"}
KODI = "kodi"  # the only device playback may ever be sent to (ADR-0021)
_MARK_RE = re.compile(r"(познач|відміт|проставл|проставт|проставити|галочк|зніми\s+(позначк|мітк|галочк))", re.IGNORECASE)
_UNMARK_RE = re.compile(r"(зніми|скасуй|прибери|не\s+(переглянут|дивив|бачив))", re.IGNORECASE)


_NOW_RE = re.compile(r'"name": "([^"]+)",\s*"runtime_minutes": (\d+),.*?"is_paused": (true|false),\s*"position_seconds": (\d+)', re.S)


def _with_remaining(tool_response: dict) -> dict:
    """Time left is arithmetic: computed here and appended, not left to the model (ADR-0038)."""
    parts = tool_response.get("content") or []
    for part in parts:
        text = part.get("text") if isinstance(part, dict) else None
        if not text:
            continue
        notes = []
        for name, runtime, paused, pos in _NOW_RE.findall(text):
            left = max(int(runtime) * 60 - int(pos), 0)
            h, m = divmod(left // 60, 60)
            end = "" if paused == "true" else f", закінчиться близько {(datetime.datetime.now() + datetime.timedelta(seconds=left)):%H:%M}"
            notes.append(f"«{name}»: залишилось {h} год {m} хв{end}{' (на паузі)' if paused == 'true' else ''}")
        if notes:
            part["text"] = text + "\n\nЗалишок (порахований кодом): " + "; ".join(notes)
    return tool_response


def _jellyfin_filter(tool, readonly_context=None) -> bool:
    text = _text(readonly_context.user_content) if readonly_context else ""
    if legacy.in_recipe_import(text):  # a bare dish name mid-import is not a film title
        return False
    if tool.name in JF_READ:
        return True
    if tool.name in JF_PLAY:
        return bool(legacy._COMMAND_RE.search(text))
    if tool.name in JF_MARK:
        return bool(_MARK_RE.search(text))
    return False


def jellyfin_names_for(text: str) -> set[str]:
    """Which Jellyfin tools this message gets (for the system prompt)."""
    names = set(JF_READ)
    if legacy._COMMAND_RE.search(text):
        names |= JF_PLAY
    if _MARK_RE.search(text):
        names |= JF_MARK
    return names


def _jellyfin_toolset():
    if not os.path.exists(MCP_BIN):
        print(f"WARNING: jellyfin-mcp binary not found at {MCP_BIN}; Jellyfin tools are off", flush=True)
        return None
    env = {k: os.environ[k] for k in ("JELLYFIN_URL", "JELLYFIN_API_KEY", "JELLYFIN_USER_ID") if k in os.environ}
    params = StdioServerParameters(command=MCP_BIN, args=["--toolsets", "discovery,media,user,playback,analytics"], env=env)
    return McpToolset(connection_params=StdioConnectionParams(server_params=params, timeout=30),
                      tool_filter=_jellyfin_filter)


def _model(hosted: bool, chat_model: str, chat_key: str | None, hosted_base: str | None, local_base: str,
           reasoning_effort: str | None = None):
    local = dict(model="openai/local", api_base=local_base, api_key="none", max_tokens=400,
                 temperature=0.2, extra_body={"repeat_penalty": 1.15})  # Qwen2.5-3B loops otherwise (ADR-0018)
    if not hosted:
        return LiteLlm(**local)
    extra = {"reasoning_effort": reasoning_effort} if reasoning_effort else {}
    return LiteLlm(model=f"openai/{chat_model}", api_key=chat_key, api_base=hosted_base,
                   max_completion_tokens=600, fallbacks=[local], **extra)


class Engine:
    def __init__(self, system_prompt, action_ok: tuple, *, chat_model: str, chat_key: str | None,
                 hosted_base: str | None, local_base: str, reasoning_effort: str | None = None):
        self._system_prompt = system_prompt
        self._action_ok = action_ok
        self._hosted_ok = bool(chat_key and hosted_base)
        self.sessions = InMemorySessionService()
        self._last: dict[str, tuple[str, float]] = {}
        self._runners = {}
        self.jellyfin = _jellyfin_toolset()
        self.last_calls: list[str] = []
        for hosted in (True, False):
            agent = Agent(
                name=APP,
                model=_model(hosted, chat_model, chat_key, hosted_base, local_base, reasoning_effort),
                instruction=self._instruction,
                tools=[PerMessageTools()] + ([self.jellyfin] if self.jellyfin else []),
                before_model_callback=self._before_model(hosted),
                after_model_callback=self._after_model(hosted),
                before_tool_callback=self._before_tool,
                after_tool_callback=self._after_tool,
            )
            self._runners[hosted] = Runner(agent=agent, app_name=APP, session_service=self.sessions)

    def _instruction(self, ctx) -> str:
        text = _recent_user_text(ctx)
        names = {t["function"]["name"] for t in legacy.tools_for(text)}
        if self.jellyfin and not legacy.in_recipe_import(text):
            names |= jellyfin_names_for(text)
        return self._system_prompt(names)

    def _before_model(self, hosted: bool):
        def cb(callback_context, llm_request):
            contents = llm_request.contents
            starts = [i for i, c in enumerate(contents)
                      if c.role == "user" and any(getattr(p, "text", None) for p in c.parts)]
            if len(starts) > KEEP_USER_TURNS:
                del contents[:starts[-KEEP_USER_TURNS]]
            if hosted:  # everything that leaves the machine is masked (ADR-0025)
                for c in contents:
                    for p in c.parts:
                        if getattr(p, "text", None):
                            p.text = guardrails.redact(p.text)
                        elif p.function_response and isinstance(p.function_response.response, dict):
                            r = p.function_response.response
                            if isinstance(r.get("result"), str):
                                r["result"] = guardrails.redact(r["result"])
            print(f"LLM {'hosted' if hosted else 'local'}", flush=True)  # audit trail
            return None
        return cb

    def _after_model(self, hosted: bool):
        def cb(callback_context, llm_response):
            usage = getattr(llm_response, "usage_metadata", None)
            if hosted and usage and usage.total_token_count:
                guardrails.record_usage(usage.total_token_count)
            return None
        return cb

    async def _before_tool(self, tool, args, tool_context):
        print(f"TOOL {tool.name} {json.dumps(args, ensure_ascii=False)}", flush=True)  # audit trail
        state = _req.get()
        state["calls"].append(tool.name)
        key = (tool.name, json.dumps(args, sort_keys=True, ensure_ascii=False))
        if key in state["seen"]:  # same call twice = a loop; stop spending
            return {"result": "Цей самий виклик уже виконано з тим самим результатом. Дай користувачу відповідь."}
        state["seen"].add(key)
        if tool.name in JF_WRITE:
            blocked = await self._jellyfin_guard(tool.name, args, _text(tool_context.user_content))
            if blocked:
                return {"result": "НЕ ЗМІНЕНО. " + blocked}
        return None

    async def _jellyfin_guard(self, name: str, args: dict, user_text: str) -> str | None:
        if name in JF_MARK:
            action = args.get("action")
            if action not in MARK_ACTIONS:
                return f"Дія «{action}» не дозволена."
            unmark = bool(_UNMARK_RE.search(user_text))  # the user's words decide the direction, not the model
            if (action == "mark_played" and unmark) or (action == "mark_unplayed" and not unmark):
                return "Напрямок дії не збігається з проханням користувача."
            return None
        if name == "jellyfin_playback_control" and args.get("command") not in PLAY_COMMANDS:
            return f"Команда «{args.get('command')}» не дозволена."
        sessions = await asyncio.to_thread(jellyfin_client.sessions)
        target = next((s for s in sessions if s.get("Id") == args.get("session_id")), None)
        label = f"{(target or {}).get('DeviceName', '')} {(target or {}).get('Client', '')}".lower()
        if not target or KODI not in label:
            return "Керувати можна лише сесією Kodi."
        return None

    def _after_tool(self, tool, args, tool_context, tool_response):
        result = tool_response.get("result", "") if isinstance(tool_response, dict) else ""
        if tool.name in legacy.CONTROL_TOOLS and isinstance(result, str) and result.startswith(self._action_ok):
            _req.get()["acted"] = True
        if tool.name in JF_WRITE and isinstance(tool_response, dict):
            blocked = isinstance(result, str) and result.startswith("НЕ ЗМІНЕНО")
            if not blocked and not (tool_response.get("isError") or tool_response.get("is_error")) and args.get("action") != "get_user_data":
                _req.get()["acted"] = True
        if tool.name in legacy.PASSTHROUGH_TOOLS:  # a numbered list must reach the user exactly (ADR-0024)
            tool_context.actions.skip_summarization = True
        if tool.name == "jellyfin_sessions" and isinstance(tool_response, dict):
            return _with_remaining(tool_response)
        return None

    async def _session(self, user: str, now: float) -> str:
        sid, last = self._last.get(user, (None, 0.0))
        if sid is None or now - last > SESSION_TTL:
            sid = f"{user}-{int(now)}"
            await self.sessions.create_session(app_name=APP, user_id=user, session_id=sid)
        self._last[user] = (sid, now)
        return sid

    async def run(self, user: str, text: str) -> tuple[str, bool, bool]:
        """(answer, an action tool really succeeded, hosted model was used)."""
        now = time.time()
        hosted = self._hosted_ok and guardrails.budget_left() > 0
        state = {"acted": False, "seen": set(), "calls": []}
        _req.set(state)
        sid = await self._session(user, now)
        message = types.Content(role="user", parts=[types.Part(text=text)])
        answer, verbatim = "", []
        try:
            async for ev in self._runners[hosted].run_async(
                    user_id=user, session_id=sid, new_message=message,
                    run_config=RunConfig(max_llm_calls=MAX_LLM_CALLS)):
                if not ev.content or not ev.is_final_response():
                    continue
                for p in ev.content.parts:
                    if getattr(p, "text", None):
                        answer += p.text
                    elif p.function_response and isinstance(p.function_response.response, dict):
                        verbatim.append(str(p.function_response.response.get("result", "")))
        except Exception as e:  # never surface a 500 to HA
            print(f"ADK error {type(e).__name__}: {e}", flush=True)
            if "llm call" in str(e).lower() or "limit" in str(e).lower():
                return "Забагато кроків міркування — не вдалось отримати остаточну відповідь.", state["acted"], hosted
            return "Не вдалося сформувати відповідь — спробуй ще раз.", state["acted"], hosted
        self.last_calls = state["calls"]
        return (answer or "\n\n".join(verbatim)), state["acted"], hosted

    async def close(self) -> None:
        if self.jellyfin:
            await self.jellyfin.close()
