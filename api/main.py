"""OpenAI-compatible chat API — Velesha as a tool-calling agent, not blind RAG.

A long-running HTTP server — the integration point for Home Assistant's
built-in `llama_cpp` integration (ADR-0013), or anything else speaking
the OpenAI chat API.

Replaces the earlier always-on retrieve-then-generate pipeline
(ADR-0011) with real tool calling (ADR-0018): the model decides when to
search Velesha's indexed knowledge, check a device's live state, or
compute energy usage, instead of every question always getting the same
blind context injection regardless of whether it's relevant or even
answerable that way (a live-state or delta-computation question can't be
answered from a static indexed snapshot at all — ADR-0018's whole point).

Usage:
    sops exec-env ../secrets.enc.env \\
      'sops exec-env secrets.enc.env "uv run uvicorn main:app --host 0.0.0.0 --port 8090"'
"""

import asyncio
import datetime
import json
import os
import re
import time
import uuid

import requests
from fastapi import FastAPI, Header
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

import guardrails
import users
from tools import CONTROL_TOOLS, PASSTHROUGH_TOOLS, call_tool, tools_for, reminder_poll_loop

# Local llama-server (always available as the private/fallback lane).
# Optional hosted model (any OpenAI-compatible endpoint): set CHAT_API_KEY
# (and CHAT_MODEL, and CHAT_URL if not OpenAI) in api/secrets.enc.env.
# Unset = local only, exactly as before. See ADR-0022, guardrails: ADR-0025.
CHAT_API_KEY = os.environ.get("CHAT_API_KEY") or (
    os.environ.get("OPENAI_API_KEY") if os.environ.get("CHAT_PROVIDER") == "openai" else None
)  # CHAT_PROVIDER=openai reuses OPENAI_API_KEY, so the key isn't stored twice (ADR-0026)
CHAT_MODEL = os.environ.get("CHAT_MODEL", "gpt-4o-mini")
LOCAL_URL = os.environ.get("LOCAL_CHAT_URL") or (
    None if CHAT_API_KEY else os.environ.get("CHAT_URL")
) or "http://localhost:8084/v1/chat/completions"
HOSTED_URL = (os.environ.get("CHAT_URL") or "https://api.openai.com/v1/chat/completions") if CHAT_API_KEY else None
MAX_TOOL_ITERATIONS = 4
# Reasoning models (gpt-5.6-*) refuse function tools on chat/completions unless
# reasoning_effort is set explicitly; "none" is also the fastest (ADR-0037).
CHAT_REASONING = os.environ.get("CHAT_REASONING") or None


_CONTROL_LINES = {
    "jellyfin_play": (
        "- jellyfin_play: запустити на сесії Kodi: спершу jellyfin_sessions(list) → session_id Kodi; "
        "jellyfin_search → id фільму; для серіалу jellyfin_tv_shows (next unplayed) → id серії. "
        "Немає сесії Kodi (вимкнений) — так і скажи\n"
    ),
    "jellyfin_playback_control": (
        "- jellyfin_playback_control: пауза (Pause), продовжити (Unpause), зупинити (Stop), "
        "NextTrack/PreviousTrack, Seek, гучність — для сесії Kodi з jellyfin_sessions\n"
    ),
    "jellyfin_user_data": (
        "- jellyfin_user_data: позначити переглянутим (mark_played) або зняти позначку (mark_unplayed) "
        "за id з jellyfin_search; «цей фільм» = те, що зараз грає в jellyfin_sessions\n"
    ),
    "grocy_consume": "- grocy_consume: списати використане зі запасів (кількість в одиницях продукту у Grocy)\n",
    "grocy_recipe_consume": "- grocy_recipe_consume: рецепт з Grocy приготовано — списати інгредієнти\n",
    "grocy_recipe_shopping": "- grocy_recipe_shopping: додати до списку покупок нестачу для рецепта з Grocy\n",
    "grocy_recipe_import": "- grocy_recipe_import: перенести рецепт з Tandoor у Grocy (чернетка → підтвердження користувача → confirm=true)\n",
    "recipe_add": (
        "- recipe_add: НОВИЙ рецепт з нуля напряму в Grocy — власний або знайдений в інтернеті (web_search), "
        "коли його нема ні в Grocy, ні в Tandoor (чернетка → підтвердження користувача → confirm=true)\n"
    ),
    "recipe_cook": (
        "- recipe_cook: покроково провести голосом по рецепту з Grocy — action=start (з name) починає, "
        "next/repeat/restart керують кроками; передавай текст кроку користувачу дослівно\n"
    ),
    "grocy_recipes_not_imported": "- grocy_recipes_not_imported: точний перелік рецептів Tandoor, яких ще немає в Grocy\n",
    "remind_me": (
        "- remind_me: разове нагадування, надішле пуш о вказаному часі (time \'19\' чи \'19:30\', або "
        "in_minutes); get_reminders — список запланованих; cancel_reminder — скасувати за частиною тексту "
        "(перенести/змінити = скасувати старе + поставити нове)\n"
    ),
    "ha_switch": "- ha_switch: увімкнути/вимкнути пристрій (розетку, лампу тощо) — спробуй викликати навіть якщо не певен, чи він у дозволеному списку, інструмент сам скаже, якщо ні; «вимкни, коли закінчиться фільм» = when=after_playback, «через N хвилин» = when=in_minutes, «що заплановано» = status, «скасуй» = cancel (ставить таймер HA)\n",
    "vacuum_control": "- vacuum_control: керування роботом-пилососом ЗАРАЗ — start/stop/pause/dock/locate, за потреби room (кімната) і mode (vacuum=без води/mop/vac_and_mop); стан/заряд — get_live_state\n",
    "vacuum_schedule": "- vacuum_schedule: запланувати запуск пилососа на пізніше (time/date або in_minutes), за потреби room і mode — той самий календар, що й нагадування\n",
    "note_add": "- note_add: записати особисту нотатку користувача; notes_search — знайти його нотатки (чужих не бачиш)\n",
    "grocy_add_stock": "- grocy_add_stock: додати куплене до запасів\n",
    "grocy_product_add": "- grocy_product_add: завести НОВИЙ товар у Grocy з початковим запасом — коли grocy_add_stock каже, що товару ще нема\n",
    "grocy_shopping_add": "- grocy_shopping_add: додати продукт до списку покупок\n",
    "web_search": "- web_search: пошук в інтернеті (свіже, новини, нові фільми) — коли користувач просить пошукати в інтернеті\n",
    "toloka_search": "- toloka_search: пошук роздач на Толоці за назвою (розмір, сідери)\n",
    "toloka_add": (
        "- toloka_add: додати варіант з останнього пошуку на Толоці за номером; категорію бери ЛИШЕ "
        "зі слів користувача, інакше запитай яку. Якщо користувач уже обрав варіант, а тепер лише назвав "
        "категорію (напр. «фільми») — виклич toloka_add з цією категорією\n"
    ),
    "qbittorrent_add": (
        "- qbittorrent_add: додати торрент за посиланням з повідомлення; категорію бери ЛИШЕ з "
        "слів користувача, інакше запитай яку\n"
    ),
}


def system_prompt(offered: set[str]) -> str:
    # The model has no notion of "today" on its own — without this it
    # guesses a training-era year (observed: "18.09" -> "2022-09-18")
    # when calling get_energy_usage with a dateless user phrase. See
    # ADR-0018.
    today = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=3))).strftime("%Y-%m-%d")
    return (
        f"Ти — Велеша, персональний асистент. Сьогодні {today}. "
        "Відповідай українською, коротко і по суті, простим текстом без markdown (без зірочок, "
        "заголовків, таблиць і посилань) — відповідь можуть озвучувати.\n"
        "Запит може надходити ГОЛОСОМ: тоді це розпізнана мова з можливими помилками (схожі слова, "
        "зіпсовані назви, немає розділових знаків). Тлумач його за змістом і за назвами пристроїв, "
        "фільмів тощо; не кажи, що користувач «ввів» чи «написав» запит.\n\n"
        "У тебе є інструменти:\n"
        "- search_knowledge: рецепти (Tandoor) і Jellyfin (що дивився, поради); знімок історії HA може бути застарілим\n"
        "- grocy_stock / grocy_shopping_list: домашні запаси (їжа, господарські товари) і список покупок у Grocy\n"
        "- grocy_recipes: рецепти, заведені в Grocy, і чи вистачає для них запасів (пошук рецептів за змістом — search_knowledge)\n"
        "- jellyfin_search: чи Є фільм/серіал у бібліотеці Jellyfin за назвою (точний пошук); "
        "jellyfin_browse (за жанром, роком, студією, актором, «переглянуто»), jellyfin_tv_shows (сезони, серії, "
        "наступна непереглянута), jellyfin_people, jellyfin_recommendations, jellyfin_get_item, jellyfin_analytics\n"
        "- jellyfin_sessions: що зараз відтворюється на Kodi; у відповіді вже є порахований кодом «Залишок» — "
        "переказуй його, сам не рахуй\n"
        "- get_live_state: ПОТОЧНЕ значення будь-якого пристрою, сенсора чи лічильника "
        "(відчинені двері, температура, скільки запитів заблокував AdGuard зараз) — "
        "не для минулих подій 'коли востаннє' і не для підрахунку за конкретну дату\n"
        "- get_sensor_history: ЖИВА історія за ПЕРІОД (скільки кВт·год, як змінився лічильник, "
        "скільки разів і коли востаннє було увімкнено, мін/макс температури) за дату, 'вчора', 'сьогодні', "
        "'тиждень'; на 'коли востаннє вмикали/відчиняли X' бери її з 'тиждень' "
        "(якщо рік не вказано — бери поточний)\n"
        "- get_activity_periods: ТРИВАЛОСТІ — кожен цикл/сеанс роботи пристрою (скільки тривало прання, коли "
        "почалось і закінчилось, скільки кВт·год за цикл); для суми за день — get_sensor_history\n"
        "- qbittorrent_status: стан qBittorrent (віддано за сесію, ratio, місце, скільки з нульовою віддачею)\n"
        "- qbittorrent_list: список торрентів за фільтром; «рейтинг 0» без уточнення = ratio 0 за весь час, "
        "а нуль за сесію лише коли прямо питають про сесію\n"
        + "".join(line for name, line in _CONTROL_LINES.items() if name in offered)
        + "\n"
        "Знайти фільм/серіал (\"знайди X\", \"є X?\", \"хочу подивитись X\"): спершу перевір бібліотеку Jellyfin "
        "інструментом jellyfin_search. Слово «фільм» у мовленні користувача НЕ означає обов'язково type=Movie — "
        "люди так само кажуть «фільм» про серіали; якщо перший виклик з одним type нічого не знайшов, спробуй "
        "ще раз з іншим type (Movie↔Series), і лише тоді — Толока (toloka_search), якщо справді немає в обох. "
        "Схожий фільм НІКОЛИ не видавай за шуканий. Питання про вже додані торренти (ratio, віддача, що качається) — "
        "qbittorrent_*, а не пошук нового.\n"
        "Інструменти — для даних користувача (дім, файли, торренти, рецепти). На загальні питання "
        "(знання про фільми, людей, речі; порада, що подивитись) відповідай самостійно, без інструментів. "
        "Не вигадуй дані користувача: якщо інструмент не знайшов відповіді, так і скажи."
    )

app = FastAPI(title="Velesha API")


@app.on_event("startup")
async def _start_reminder_poller():
    asyncio.create_task(reminder_poll_loop())


@app.on_event("shutdown")
async def _shutdown():
    if _engine is not None:
        await _engine.close()


class ChatMessage(BaseModel):
    role: str
    content: str | None = None
    tool_calls: list[dict] | None = None
    tool_call_id: str | None = None


class ChatCompletionRequest(BaseModel):
    model: str = "velesha"
    messages: list[ChatMessage]
    max_tokens: int = 400
    temperature: float = 0.2
    stream: bool = False  # always answered non-streamed internally, see sse_chunk


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/v1/models")
def list_models():
    return {"object": "list", "data": [{"id": "velesha", "object": "model", "owned_by": "velesha"}]}


def _complete_local(messages: list[dict], tools: list[dict]) -> dict:
    payload = {
        "messages": messages, "tools": tools, "tool_choice": "auto",
        "max_tokens": 400, "temperature": 0.2,
        "repeat_penalty": 1.15,  # Qwen2.5-3B loops on longer answers otherwise (ADR-0018)
    }
    # llama-server answers 500 when the model emits output its tool-call
    # parser rejects (occasional garbled tokens from the quantized 3B
    # model) — one retry usually succeeds; never surface a 500.
    try:
        for _ in range(2):
            resp = requests.post(LOCAL_URL, json=payload, timeout=180)
            if resp.status_code != 500:
                break
    except requests.exceptions.RequestException:
        # slow CPU model + long tool schemas can exceed the timeout;
        # HA should get an answer, not a 500 (ADR-0023)
        return {"content": "Модель не встигла відповісти — спробуй ще раз."}
    if resp.status_code == 500:
        return {"content": "Не вдалося сформувати відповідь — спробуй перефразувати питання."}
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]


def _complete_hosted(messages: list[dict], tools: list[dict]) -> dict | None:
    """One hosted-model step, or None to make the caller fall back to local.

    Everything that leaves the machine is redacted first (ADR-0025) and the
    token usage is recorded against the daily budget."""
    payload = {
        "model": CHAT_MODEL, "messages": guardrails.redact_messages(messages),
        "tools": tools, "tool_choice": "auto", "max_completion_tokens": 600,
    }
    if CHAT_REASONING:
        payload["reasoning_effort"] = CHAT_REASONING
    try:
        resp = requests.post(
            HOSTED_URL, json=payload, headers={"Authorization": f"Bearer {CHAT_API_KEY}"}, timeout=120
        )
    except requests.exceptions.RequestException as e:
        print(f"HOSTED error {type(e).__name__} -> local", flush=True)
        return None
    if resp.status_code >= 400:
        print(f"HOSTED http {resp.status_code} -> local", flush=True)
        return None
    data = resp.json()
    guardrails.record_usage(data.get("usage", {}).get("total_tokens", 0))
    return data["choices"][0]["message"]


# Every action tool starts its success message with one of these; anything else
# ("НЕ ДОДАНО …", "Не …", errors) means nothing was changed.
_ACTION_OK = ("Запущено", "Додано", "Поставлено на паузу", "Продовжено", "Зупинено", "Списано", "Створено", "Записано", "Позначено", "Вимкнено", "Увімкнено", "Заплановано", "Скасовано")
_CLAIM_RE = re.compile(
    r"(?<!не )\b(додав|додала|додано|запустив|запустила|запущено|поставив на паузу|поставлено на паузу|"
    r"продовжив|продовжено|зупинив|зупинено|списав|списала|списано|позначив|позначила|позначено|вимкнув|вимкнула|вимкнено|увімкнув|увімкнула|увімкнено|заплановано|скасував|скасувала|скасовано)\b", re.IGNORECASE)


def unfounded_claim(answer: str, acted: bool) -> str:
    """The model said it did something but no action tool succeeded — say so.

    Found in real use: the add-torrent tool was not offered for "давай перший
    варіант", and the model answered "додано" having called nothing (ADR-0030)."""
    if acted or not _CLAIM_RE.search(answer):
        return answer
    return answer + "\n\n⚠ Насправді нічого не змінено: жодну дію не виконано. Скажи ще раз, що саме зробити."


def run_agent(messages: list[dict], tools: list[dict], user_text: str = "", state: dict | None = None) -> str:
    tool_names: dict[str, str] = {}  # tool_call_id -> tool name, for data-class routing
    seen_calls: set[tuple[str, str]] = set()
    for _ in range(MAX_TOOL_ITERATIONS):
        # Hosted only while there is budget AND no result of a blocked
        # (sensitive) tool is in the conversation — those stay local.
        hosted = (
            HOSTED_URL is not None
            and guardrails.budget_left() > 0
            and not guardrails.has_blocked_result(messages, tool_names)
        )
        message = _complete_hosted(messages, tools) if hosted else None
        backend = "hosted" if message is not None else "local"
        if message is None:
            message = _complete_local(messages, tools)
        print(f"LLM {backend}", flush=True)  # audit trail

        tool_calls = message.get("tool_calls")
        if not tool_calls:
            return message.get("content") or ""

        messages.append(message)
        results = []
        for tc in tool_calls:
            fn = tc["function"]
            key = (fn["name"], fn["arguments"])
            if key in seen_calls:  # same call twice = a loop; stop spending
                return "Агент зациклився на повторному виклику — зупинено."
            seen_calls.add(key)
            tool_names[tc["id"]] = fn["name"]
            print(f"TOOL {fn['name']} {fn['arguments']}", flush=True)  # audit trail
            result = call_tool(fn["name"], fn["arguments"], user_text)
            if state is not None and fn["name"] in CONTROL_TOOLS and result.startswith(_ACTION_OK):
                state["acted"] = True
            results.append(result)
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
        # A numbered list the user will refer back to must reach them
        # exactly — the 3B model rewrites/drops lines and costs a whole
        # extra call. Return such tool output verbatim (ADR-0024).
        if all(tc["function"]["name"] in PASSTHROUGH_TOOLS for tc in tool_calls):
            return "\n\n".join(results)

    return "Забагато кроків міркування — не вдалось отримати остаточну відповідь."


def sse_chunk(content: str) -> bytes:
    created = int(time.time())
    chat_id = f"chatcmpl-{uuid.uuid4().hex}"
    delta = {
        "id": chat_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": "velesha",
        "choices": [{"index": 0, "delta": {"role": "assistant", "content": content}, "finish_reason": None}],
    }
    done = {
        "id": chat_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": "velesha",
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    body = f"data: {json.dumps(delta, ensure_ascii=False)}\n\n"
    body += f"data: {json.dumps(done, ensure_ascii=False)}\n\n"
    body += "data: [DONE]\n\n"
    return body.encode()


# Short-term memory (ADR-0034): HA starts a fresh conversation after a few idle
# minutes, so a short reply ("Ранч") arrives with no history. Keep the last few
# exchanges and replay them when a request comes in cold and recent.
MEMORY_TTL = 60 * 60
MEMORY_TURNS = 3
_memory: dict[str, list[dict]] = {}


def _recall(now: float) -> list[dict]:
    fresh = [t for t in _memory.get(users.current(), []) if now - t["at"] < MEMORY_TTL][-MEMORY_TURNS:]
    return [{"role": role, "content": t[role]} for t in fresh for role in ("user", "assistant")]


def _remember(user: str, assistant: str, now: float) -> None:
    turns = _memory.setdefault(users.current(), [])
    turns.append({"at": now, "user": user[:500], "assistant": assistant[:800]})
    del turns[:-10]


_engine = None


def _adk_engine():
    global _engine
    if _engine is None:
        import adk_agent
        _engine = adk_agent.Engine(
            system_prompt, _ACTION_OK, chat_model=CHAT_MODEL, chat_key=CHAT_API_KEY, reasoning_effort=CHAT_REASONING,
            hosted_base=HOSTED_URL.removesuffix("/chat/completions") if HOSTED_URL else None,
            local_base=LOCAL_URL.removesuffix("/chat/completions"))
    return _engine


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionRequest, authorization: str | None = Header(None)):
    caller = users.resolve(authorization)
    if caller is None:
        return JSONResponse({"error": {"message": "Unknown API key", "type": "invalid_request_error"}}, status_code=401)
    users.set_current(caller)
    if os.environ.get("LOG_CALLER_PROMPT"):  # diagnostic: does HA mark voice vs typed input?
        with open("/tmp/velesha-caller.log", "a") as f:
            f.write(json.dumps({
                "t": time.strftime("%H:%M:%S"),
                "system": [m.content for m in req.messages if m.role == "system"],
                "last_user": next((m.content for m in reversed(req.messages) if m.role == "user"), None),
                "n_messages": len(req.messages),
            }, ensure_ascii=False) + "\n")
    last_user = next((m.content or "" for m in reversed(req.messages) if m.role == "user"), "")

    if os.environ.get("AGENT_ENGINE", "adk") == "adk":
        answer, acted, _ = await _adk_engine().run(caller, last_user)
        answer = unfounded_claim(answer, acted)
    else:
        messages = [{"role": "system", "content": ""}]
        messages += [{"role": m.role, "content": m.content or ""} for m in req.messages if m.role != "system"]
        now = time.time()
        if len(messages) == 2:  # system + one user message: HA gave no history
            messages[1:1] = _recall(now)
        offered = tools_for(last_user)
        messages[0]["content"] = system_prompt({t["function"]["name"] for t in offered})
        state = {"acted": False}
        answer = await asyncio.to_thread(run_agent, messages, offered, last_user, state)
        answer = unfounded_claim(answer, state["acted"])
        _remember(last_user, answer, now)
    # A silent drop to the weak local model looked like the assistant getting
    # stupid (the user's torrent-search confusion, ADR-0029) — say so.
    if HOSTED_URL and guardrails.budget_left() <= 0:
        answer = "(Денний ліміт токенів вичерпано — відповідає слабша локальна модель.) " + answer

    if req.stream:
        return StreamingResponse(iter([sse_chunk(answer)]), media_type="text/event-stream")

    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": "velesha",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": answer}, "finish_reason": "stop"}],
        "usage": {},
    }
