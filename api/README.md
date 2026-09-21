# API

OpenAI-compatible `/v1/chat/completions` server — Velesha as a
tool-calling agent (ADR-0018), not blind RAG. The model decides when to
call `search_knowledge` (recipes/Jellyfin/HA history), `get_live_state`
(current device/sensor state), or `get_sensor_history` (period summary: kWh, counter change, on/off
counts) — instead of every question getting the same fixed context
regardless of whether it's even answerable that way. Wired into Home
Assistant via HA's built-in `llama_cpp` integration (**not** "OpenAI
Conversation" — see ADR-0013 for why).

## Running it

Needs the embedding server (8083) and a **tool-calling-capable** chat
server (8084, `--jinja`, Qwen2.5-3B-Instruct verified working) from the
repo root README already up, plus Qdrant.

```bash
uv sync
sops exec-env ../secrets.enc.env \
  'sops --config /dev/null exec-env secrets.enc.env "uv run uvicorn main:app --host 0.0.0.0 --port 8090"'
```

```bash
curl http://localhost:8090/v1/chat/completions -H 'Content-Type: application/json' -d '{
  "messages": [{"role": "user", "content": "скільки 18.09 числа пралка використала електроенергії?"}]
}'
```

## HA setup

Add the **`llama_cpp`** integration (Settings → Devices & Services → Add
Integration → "llama.cpp") — not "OpenAI Conversation", which dropped
custom-`base_url` support entirely (ADR-0013). Point it at
`http://<this-host-LAN-IP>:8090/v1`, leave the API key empty, pick the
`velesha` model it discovers. Creates a `conversation.<name>` entity
usable via `/api/conversation/process` or HA's Assist pipeline.

## Limitations

- Read tools (`search_knowledge`/`get_live_state`/`get_sensor_history`)
  never change anything. One action tool: `play_on_jellyfin_device`
  (Kodi only, ADR-0021), offered to the model only when the user message
  has a command verb. HA's Assist device-control schema
  (`intent__HassTurnOn` etc.) is still received but dropped — device
  power stays with HA's own intents.
- Any system message the caller sends (e.g. HA's own house-description
  prompt) is replaced with Velesha's own tool-aware one, not merged.
- Small-model residual: even with correct, correctly-sorted tool
  output, Qwen2.5-3B occasionally misreads which line in a list is the
  answer (see ADR-0018's Consequences) — not common, but not zero.

## Jellyfin (MCP)

Jellyfin — через MCP-сервер `jellyfin-mcp` (ADR-0038): бінарник `api/bin/jellyfin-mcp`
(не в git, збірка і патч для Jellyfin 12.1 — в ADR). Змінна `JELLYFIN_MCP_BIN`
задає інший шлях. Працює лише з `AGENT_ENGINE=adk` (за замовчуванням).

## Кілька користувачів

`VELESHA_USERS="ключ1:ivan,ключ2:olha"` і `VELESHA_ADMINS="ivan"` в
`api/secrets.enc.env` (ADR-0035). У HA — окремий запис `llama_cpp` на людину з
її ключем в полі API key. Без `VELESHA_USERS` працює як раніше (один
користувач). Торренти/Толока — лише адміну; нотатки, пам'ять розмов окремі.

## Grocy

Домашні запаси (їжа, побутова хімія) — у Grocy (ADR-0032): `GROCY_URL`,
`GROCY_API_KEY` в `api/secrets.enc.env`. Читання: `grocy_stock`,
`grocy_shopping_list`; дії (лише за явною фразою): `grocy_consume`,
`grocy_add_stock`, `grocy_shopping_add`.

## Model selection

Local Qwen2.5-3B by default. `CHAT_PROVIDER=openai CHAT_MODEL=gpt-4.1-mini`
(reuses `OPENAI_API_KEY` from `api/secrets.enc.env`) switches to OpenAI —
see ADR-0026 for the comparison and ADR-0025 for the guardrails (secret
masking, daily token budget, local fallback). Compare models with
`api/eval_models.py local|openai|gemini` (`EVAL_RUNS=3` for a rate).
