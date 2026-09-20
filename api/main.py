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

import datetime
import json
import os
import re
import time
import uuid

import requests
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import guardrails
from tools import CONTROL_TOOLS, PASSTHROUGH_TOOLS, call_tool, tools_for

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


_CONTROL_LINES = {
    "play_on_jellyfin_device": "- play_on_jellyfin_device: запустити фільм/серіал за назвою на Kodi\n",
    "control_jellyfin_playback": (
        "- control_jellyfin_playback: керувати тим, що зараз грає на Kodi — пауза, продовжити, "
        "зупинити, наступна/попередня серія (НЕ передавай це як назву фільму)\n"
    ),
    "grocy_consume": "- grocy_consume: списати використане зі запасів (кількість в одиницях продукту у Grocy)\n",
    "grocy_recipe_consume": "- grocy_recipe_consume: рецепт з Grocy приготовано — списати інгредієнти\n",
    "grocy_recipe_shopping": "- grocy_recipe_shopping: додати до списку покупок нестачу для рецепта з Grocy\n",
    "grocy_recipe_import": "- grocy_recipe_import: перенести рецепт з Tandoor у Grocy (чернетка → підтвердження користувача → confirm=true)\n",
    "grocy_add_stock": "- grocy_add_stock: додати куплене до запасів\n",
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
        "- jellyfin_find: чи Є фільм/серіал у бібліотеці Jellyfin за назвою (точний пошук)\n"
        "- get_live_state: ПОТОЧНЕ значення будь-якого пристрою, сенсора чи лічильника "
        "(відчинені двері, температура, скільки запитів заблокував AdGuard зараз) — "
        "не для минулих подій 'коли востаннє' і не для підрахунку за конкретну дату\n"
        "- get_sensor_history: ЖИВА історія за ПЕРІОД (скільки кВт·год, як змінився лічильник, "
        "скільки разів і коли востаннє було увімкнено, мін/макс температури) за дату, 'вчора', 'сьогодні', "
        "'тиждень'; на 'коли востаннє вмикали/відчиняли X' бери її з 'тиждень' "
        "(якщо рік не вказано — бери поточний)\n"
        "- qbittorrent_status: стан qBittorrent (віддано за сесію, ratio, місце, скільки з нульовою віддачею)\n"
        "- qbittorrent_list: список торрентів за фільтром; «рейтинг 0» без уточнення = ratio 0 за весь час, "
        "а нуль за сесію лише коли прямо питають про сесію\n"
        + "".join(line for name, line in _CONTROL_LINES.items() if name in offered)
        + "\n"
        "Знайти фільм/серіал (\"знайди X\", \"є X?\", \"хочу подивитись X\"): спершу перевір бібліотеку Jellyfin "
        "інструментом jellyfin_find; якщо його відповідь «немає» — шукай на Толоці (toloka_search). Схожий фільм "
        "НІКОЛИ не видавай за шуканий. Питання про вже додані торренти (ratio, віддача, що качається) — qbittorrent_*, "
        "а не пошук нового.\n"
        "Інструменти — для даних користувача (дім, файли, торренти, рецепти). На загальні питання "
        "(знання про фільми, людей, речі; порада, що подивитись) відповідай самостійно, без інструментів. "
        "Не вигадуй дані користувача: якщо інструмент не знайшов відповіді, так і скажи."
    )

app = FastAPI(title="Velesha API")


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
_ACTION_OK = ("Запущено", "Додано", "Поставлено на паузу", "Продовжено", "Зупинено", "Списано", "Створено")
_CLAIM_RE = re.compile(
    r"\b(додав|додала|додано|запустив|запустила|запущено|поставив на паузу|поставлено на паузу|"
    r"продовжив|продовжено|зупинив|зупинено|списав|списала|списано)\b", re.IGNORECASE)


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


@app.post("/v1/chat/completions")
def chat_completions(req: ChatCompletionRequest):
    if os.environ.get("LOG_CALLER_PROMPT"):  # diagnostic: does HA mark voice vs typed input?
        with open("/tmp/velesha-caller.log", "a") as f:
            f.write(json.dumps({
                "t": time.strftime("%H:%M:%S"),
                "system": [m.content for m in req.messages if m.role == "system"],
                "last_user": next((m.content for m in reversed(req.messages) if m.role == "user"), None),
                "n_messages": len(req.messages),
            }, ensure_ascii=False) + "\n")
    messages = [{"role": "system", "content": ""}]
    messages += [{"role": m.role, "content": m.content or ""} for m in req.messages if m.role != "system"]

    last_user = next((m.content or "" for m in reversed(req.messages) if m.role == "user"), "")
    offered = tools_for(last_user)
    messages[0]["content"] = system_prompt({t["function"]["name"] for t in offered})
    state = {"acted": False}
    answer = unfounded_claim(run_agent(messages, offered, last_user, state), state["acted"])
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
