# ADR-0037: Робоча модель — gpt-5.6-luna (reasoning_effort=none)

## Контекст
Робочою була `gpt-4.1-mini` (ADR-0026). Користувач запропонував `gpt-5.6-luna`;
вона є у ключі (`GET /v1/models`, поруч `-sol`, `-terra`). Ціна за відкритими
джерелами: $0.20 вхід / $1.20 вихід за 1M токенів (після зниження 2026-07-30).

## Що виявили
- `gpt-5.6-luna` на `/v1/chat/completions` відхиляє виклик інструментів, поки
  `reasoning_effort` не заданий явно: «Function tools with reasoning_effort are not
  supported… set reasoning_effort to 'none'». Старий рушій (сирий `requests`)
  через це падав на локальну модель; ADK через LiteLLM працював.
- Додано `CHAT_REASONING` (для `main.py` і ADK): передається як `reasoning_effort`.

## Результат (eval, по 3 прогони, `EVAL_MODEL=gpt-5.6-luna`)
- ADK, `none`: 30/30, ≈2.3 с на питання, ≈4.7k токенів (як у 4.1-mini).
- Legacy, `none`: 29/30 (один прогін AdGuard узяв не ту метрику).
- ADK без `reasoning_effort`: 30/30, але 3–9 с на питання.
- eval `toloka` тепер приймає `jellyfin_find` як перший крок (правило промпта, ADR-0029).

## Рішення
Робоча модель — `gpt-5.6-luna` з `CHAT_REASONING=none`. `gpt-4.1-mini`
лишається можливою (`CHAT_MODEL`), обидві налаштовуються змінними середовища.

## Наслідки
- Ціна за токен нижча за 4.1-mini, кількість токенів та швидкість такі самі;
  реальні витрати варто звірити за тиждень з `api/data/usage.json` і кабінетом OpenAI.
- `max_completion_tokens=600` при `none` достатньо; з міркуванням би не вистачило.
