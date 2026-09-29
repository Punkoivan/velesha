# ADR-0059: OpenTelemetry traces до наявного OTLP-колектора

- **Status**: accepted
- **Date**: 2026-09-27

## Контекст

Ця сесія неодноразово діагностувалась через grep по `journalctl` —
повільно й вручну для кожного «чому тулза не викликалась». У користувача
вже розгорнутий OTLP-колектор (`192.168.88.7:4318` HTTP / `4317` gRPC),
лишалось лише під'єднати сервер.

## Рішення

`api/telemetry.py` — тонка обгортка: `setup()` створює `TracerProvider`
з `OTLPSpanExporter` (читає стандартні env-змінні
`OTEL_EXPORTER_OTLP_ENDPOINT`/`_TRACES_ENDPOINT` сам, нічого свого не
винаходили) і `RequestsInstrumentor` (усі виклики `ha_client`/
`grocy_client`/`jellyfin_client`/OpenAI йдуть через `requests` —
автоматично стають дочірніми спанами). `instrument_fastapi(app)` додає
HTTP-рівень спанів на весь `/v1/chat/completions`.

Головна знахідка: **Google ADK вже сам генерує повноцінні OTel-спани**
(`google.adk.telemetry.tracing`) з правильними GenAI semantic-convention
атрибутами (`gen_ai.tool.name`, `gen_ai.conversation.id`, модель,
tool_call_id тощо) — нічого інструментувати вручну під самі
tool-виклики/LLM-виклики не треба, досить лише виставити глобальний
`TracerProvider` до того, як ADK почне створювати спани (`setup()`
викликається на рівні модуля в `main.py`, до імпорту `tools`/
`adk_agent`).

Якщо `OTEL_EXPORTER_OTLP_ENDPOINT` не задано — усе залишається
no-op, нічого не ламається без колектора.

## Наслідки

- На `acer`: `OTEL_EXPORTER_OTLP_ENDPOINT=http://192.168.88.7:4318` в
  `run-server.sh`.
- Дані, що йдуть через `requests` (HA/Grocy/Jellyfin/OpenAI) —
  автоматично трасуються; сам `jellyfin-mcp` (окремий Go-процес) і
  локальний `llama-server` (embedding) не інструментовані — окрема
  робота, якщо знадобиться.

## Доповнення: Phoenix і OpenInference (2026-09-28)

MLflow розбирав трейси нормально, а Phoenix — ні: він читає не
`gen_ai.*`, а атрибути **OpenInference** (`openinference.span.kind`,
`input.value`/`output.value`, `llm.input_messages.*`, `tool.*`).

Рішення — `openinference-instrumentation-google-adk`:
`GoogleADKInstrumentor().instrument(tracer_provider=...)` у
`telemetry.setup()`. Він підміняє трейсер ADK власним, але все одно
викликає ADK-шні `trace_call_llm`/`trace_tool_call`, тож кожен спан
несе обидва набори атрибутів (перевірено in-memory експортером на
реальному запиті: `invocation`=CHAIN, `agent_run`=AGENT,
`call_llm`=LLM, `execute_tool`=TOOL, `gen_ai.*` на місці) — і MLflow,
і Phoenix парсять.

Ціна: промпти пишуться у спан двічі (важчі спани); ліміт 4 КБ на
атрибут обрізає довгий системний промпт у `llm.input_messages.*`.

**Оновлення 2026-09-29:** `OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT` піднято з 4096
до 32768 — 4 КБ обрізали системний промпт у спанах.
