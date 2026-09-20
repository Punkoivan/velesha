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
import time

from google.adk.agents import Agent
from google.adk.agents.run_config import RunConfig
from google.adk.models.lite_llm import LiteLlm
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools import BaseTool
from google.adk.tools.base_toolset import BaseToolset
from google.genai import types

import guardrails
import tools as legacy
import users

APP = "velesha"
MAX_LLM_CALLS = 6
SESSION_TTL = 60 * 60  # HA forgets a conversation after minutes; we keep a person's for an hour (ADR-0034)
KEEP_USER_TURNS = 3

_req: contextvars.ContextVar[dict] = contextvars.ContextVar("velesha_req")


def _text(content: types.Content | None) -> str:
    return " ".join(p.text for p in (content.parts if content else []) if getattr(p, "text", None))


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
        text = _text(readonly_context.user_content) if readonly_context else ""
        return [LegacyTool(s) for s in legacy.tools_for(text)]

    async def close(self):
        pass


def _model(hosted: bool, chat_model: str, chat_key: str | None, hosted_base: str | None, local_base: str):
    local = dict(model="openai/local", api_base=local_base, api_key="none", max_tokens=400,
                 temperature=0.2, extra_body={"repeat_penalty": 1.15})  # Qwen2.5-3B loops otherwise (ADR-0018)
    if not hosted:
        return LiteLlm(**local)
    return LiteLlm(model=f"openai/{chat_model}", api_key=chat_key, api_base=hosted_base,
                   max_completion_tokens=600, fallbacks=[local])


class Engine:
    def __init__(self, system_prompt, action_ok: tuple, *, chat_model: str, chat_key: str | None,
                 hosted_base: str | None, local_base: str):
        self._system_prompt = system_prompt
        self._action_ok = action_ok
        self._hosted_ok = bool(chat_key and hosted_base)
        self.sessions = InMemorySessionService()
        self._last: dict[str, tuple[str, float]] = {}
        self._runners = {}
        for hosted in (True, False):
            agent = Agent(
                name=APP,
                model=_model(hosted, chat_model, chat_key, hosted_base, local_base),
                instruction=self._instruction,
                tools=[PerMessageTools()],
                before_model_callback=self._before_model(hosted),
                after_model_callback=self._after_model(hosted),
                before_tool_callback=self._before_tool,
                after_tool_callback=self._after_tool,
            )
            self._runners[hosted] = Runner(agent=agent, app_name=APP, session_service=self.sessions)

    def _instruction(self, ctx) -> str:
        names = {t["function"]["name"] for t in legacy.tools_for(_text(ctx.user_content))}
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

    def _before_tool(self, tool, args, tool_context):
        print(f"TOOL {tool.name} {json.dumps(args, ensure_ascii=False)}", flush=True)  # audit trail
        seen = _req.get()["seen"]
        key = (tool.name, json.dumps(args, sort_keys=True, ensure_ascii=False))
        if key in seen:  # same call twice = a loop; stop spending
            return {"result": "Цей самий виклик уже виконано з тим самим результатом. Дай користувачу відповідь."}
        seen.add(key)
        return None

    def _after_tool(self, tool, args, tool_context, tool_response):
        result = tool_response.get("result", "") if isinstance(tool_response, dict) else ""
        if tool.name in legacy.CONTROL_TOOLS and isinstance(result, str) and result.startswith(self._action_ok):
            _req.get()["acted"] = True
        if tool.name in legacy.PASSTHROUGH_TOOLS:  # a numbered list must reach the user exactly (ADR-0024)
            tool_context.actions.skip_summarization = True
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
        state = {"acted": False, "seen": set()}
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
        return (answer or "\n\n".join(verbatim)), state["acted"], hosted
