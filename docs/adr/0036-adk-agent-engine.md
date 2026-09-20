# ADR-0036: Агентний цикл на Google ADK

## Контекст
Власний цикл у `main.py` (`run_agent`, `_complete_hosted/_local`, пам'ять,
облік токенів) працює, але хочеться підключати спостережуваність (OpenTelemetry,
Phoenix), agentgateway і майбутню мультиагентність готовими бібліотеками, а не
власними милицями, і щоб код можна було розібрати за стандартними поняттями.
Розглянуто OpenAI Agents SDK, Pydantic AI, LangGraph, ADK; обрано **ADK**
(вбудований OpenTelemetry, callbacks, сесії, MCP, multi-agent, eval, Python і Go).

## Рішення
- `api/adk_agent.py`: `Engine` на ADK; модель через LiteLLM (`gpt-4.1-mini`, з
  `fallbacks` на локальну Qwen; окремий локальний агент, коли бюджет вичерпано).
  Перемикач `AGENT_ENGINE=adk|legacy` (за замовчуванням `legacy`, поки гілка не
  прийнята); FastAPI-шар, `/v1/chat/completions`, SSE, користувачі лишились.
- Що перенесено на точки розширення ADK, без зміни логіки:
  - наші JSON-схеми і `call_tool` — `LegacyTool(BaseTool)` (`parameters_json_schema`);
  - вибір інструментів за повідомленням (`tools_for`, адмін-фільтр) — `BaseToolset.get_tools`;
  - системний промпт — `instruction` як функція;
  - маскування секретів, підрахунок токенів — `before/after_model_callback`;
  - зупинка на повторному виклику, прапорець «дія справді виконана»,
    дослівна відповідь (`PASSTHROUGH_TOOLS`) — `before/after_tool_callback`,
    `skip_summarization`;
  - ліміт кроків — `RunConfig(max_llm_calls=6)`;
  - пам'ять розмови (ADR-0034) — сесія ADK на користувача, нова після 60 хв простою;
    історія HA не використовується, лише останнє повідомлення.
- `unfounded_claim`, повідомлення про вичерпаний бюджет лишились у `main.py`.
- `eval_models.py`: `EVAL_ENGINE=adk`.
- Виправлено попутно (обидва рушії): `grocy_recipes` відбирав запити у
  `search_knowledge` (опис звужено); ISO-дата зі застарілим роком
  («2023-09-18») зводиться до останнього такого дня (`_parse_day`).

## Результат (gpt-4.1-mini, EVAL_RUNS=3)
ADK 29/30 → після виправлення дат energy 6/6 на обох; legacy 28/30 → 6/6.
Локальна модель через ADK працює. Токенів приблизно стільки ж.

## Наслідки і що далі
- Залежності: `google-adk`, `litellm` (великі, ~1800 рядків у lock).
- Не перенесено: `has_blocked_result` (маршрутизація «чутливих» інструментів
  лише на локальну модель; за замовчуванням список порожній).
- Сесії в RAM (`InMemorySessionService`); після перезапуску порожні.
- Далі: OpenTelemetry → Phoenix, agentgateway перед OpenAI, мультиагентність
  (агент прання), виведення `legacy`.
