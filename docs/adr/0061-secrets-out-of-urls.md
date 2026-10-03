# ADR-0061: Секрети — не в URL (вебхук-токен у заголовку, токен бота вирізається зі спанів)

- **Status**: accepted
- **Date**: 2026-10-03

## Контекст

Перша перевірка вебхука з HA (ADR-0060) показала: `WEBHOOK_TOKEN` у
`?token=` пишеться відкритим текстом у журнал доступу uvicorn, а з
ADR-0059 стандартний `logging` іде ще й в OTLP-колектор.

Те саме з вихідного боку: `RequestsInstrumentor` пише повний URL у
`http.url`, а Telegram Bot API тримає токен бота в шляху
(`/bot<token>/sendMessage`). Перевірено in-memory експортером — токен
потрапляв у спани з 2026-09-27 на кожне Telegram-сповіщення.

## Рішення

- `/webhook/{name}` приймає токен лише із заголовка `X-Webhook-Token`
  (`secrets.compare_digest`). Query-варіант прибрано без перехідного
  періоду — токен однаково перевипускається.
- `telemetry._redact_url` — `request_hook` для `RequestsInstrumentor`,
  замінює `/bot<щось>/` на `/botREDACTED/` у `http.url`/`url.full`.
- Обидва засвічені токени перевипущено: `WEBHOOK_TOKEN` (наш випадковий
  рядок) і токен бота (@BotFather → `/revoke`).

HA, `configuration.yaml`:
```yaml
rest_command:
  velesha_webhook:
    url: "http://192.168.88.13:8090/webhook/{{ name }}"
    method: POST
    headers:
      X-Webhook-Token: !secret velesha_webhook_token
    content_type: "application/json"
    payload: '{"message": {{ message | tojson }}}'
```
`secrets.yaml`: `velesha_webhook_token: "<WEBHOOK_TOKEN>"` (старий
`velesha_webhook_url` прибрати).

## Наслідки

- Старі спани в колекторі все ще містять старий токен бота — після
  `/revoke` він недійсний, чистити не обов'язково.
- Інші клієнти (HA, Grocy, Jellyfin) передають ключі в заголовках —
  перевірено, що в URL їх немає.
