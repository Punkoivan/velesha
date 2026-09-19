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
import time
import uuid

import requests
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from tools import TOOLS, call_tool

CHAT_URL = os.environ.get("CHAT_URL", "http://localhost:8084/v1/chat/completions")
MAX_TOOL_ITERATIONS = 4


def system_prompt() -> str:
    # The model has no notion of "today" on its own — without this it
    # guesses a training-era year (observed: "18.09" -> "2022-09-18")
    # when calling get_energy_usage with a dateless user phrase. See
    # ADR-0018.
    today = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=3))).strftime("%Y-%m-%d")
    return (
        f"Ти — Велеша, персональний асистент. Сьогодні {today}. "
        "Відповідай українською, коротко і по суті.\n\n"
        "У тебе є інструменти:\n"
        "- search_knowledge: рецепти (Tandoor), Jellyfin, та ІСТОРІЯ подій Home "
        "Assistant (коли щось вмикали/вимикали раніше — 'коли востаннє X')\n"
        "- get_live_state: ПОТОЧНЕ значення будь-якого пристрою, сенсора чи лічильника "
        "(відчинені двері, температура, скільки запитів заблокував AdGuard зараз) — "
        "не для минулих подій 'коли востаннє' і не для підрахунку за конкретну дату\n"
        "- get_sensor_history: підсумок за ПЕРІОД (скільки кВт·год, як змінився лічильник, "
        "скільки разів і як довго було увімкнено, мін/макс температури) за дату, 'вчора', 'сьогодні' "
        "(якщо рік не вказано — бери поточний)\n\n"
        "Використовуй інструмент, коли для відповіді потрібні конкретні дані — "
        "не вигадуй факти. Якщо інструмент не знайшов відповіді, так і скажи."
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


def run_agent(messages: list[dict]) -> str:
    for _ in range(MAX_TOOL_ITERATIONS):
        payload = {
            "messages": messages,
            "tools": TOOLS,
            "tool_choice": "auto",
            "max_tokens": 400,
            "temperature": 0.2,
            "repeat_penalty": 1.15,  # Qwen2.5-3B loops on longer answers otherwise (ADR-0018)
        }
        # llama-server answers 500 when the model emits output its
        # tool-call parser rejects (occasional garbled tokens from the
        # quantized 3B model, seen in testing) — one retry usually
        # succeeds since sampling isn't deterministic; never surface a 500.
        for attempt in range(2):
            resp = requests.post(CHAT_URL, json=payload, timeout=120)
            if resp.status_code != 500:
                break
        if resp.status_code == 500:
            return "Не вдалося сформувати відповідь — спробуй перефразувати питання."
        resp.raise_for_status()
        message = resp.json()["choices"][0]["message"]

        tool_calls = message.get("tool_calls")
        if not tool_calls:
            return message.get("content") or ""

        messages.append(message)
        for tc in tool_calls:
            fn = tc["function"]
            result = call_tool(fn["name"], fn["arguments"])
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})

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
    messages = [{"role": "system", "content": system_prompt()}]
    messages += [{"role": m.role, "content": m.content or ""} for m in req.messages if m.role != "system"]

    answer = run_agent(messages)

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
