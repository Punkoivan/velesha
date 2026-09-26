# ADR-0054: Telegram як другий канал сповіщень (лише вихідні)

- **Status**: accepted
- **Date**: 2026-09-27

## Контекст

Хотіли ще один канал для push, окрім HA Companion App/`notify.*`.
Обмежились навмисно: лише вихідні сповіщення, без вхідних команд і без
веб-хука — не окремий інтерфейс до Velesha, а ще один спосіб доставки
того, що вже й так шле `reminder_poll_loop` (ADR-0049).

## Рішення

- `api/telegram_client.py` — мінімальний клієнт Bot API,
  `send_message(chat_id, text)` через `TELEGRAM_BOT_TOKEN`
  (`api/secrets.enc.env`, справжній секрет).
- `TELEGRAM_CHAT_ID_<USER>` (uppercase, той самий патерн, що
  `REMINDER_NOTIFY_<USER>`) — chat_id, не секрет, у `run-server.sh`.
  Для `acer`: `TELEGRAM_CHAT_ID_DEFAULT=333751480`.
- `poll_reminders_once` тепер шле в **обидва** налаштовані канали
  паралельно (HA notify, якщо є `REMINDER_NOTIFY_*`, і Telegram, якщо
  є `TELEGRAM_CHAT_ID_*`) — не або/або, кожен незалежно.
- Бот створено через @BotFather (`@velesha_ha_bot`); chat_id знайдено
  через `getUpdates` після того, як користувач написав боту перше
  повідомлення (бот не може писати першим).

## Наслідки

- Жодної обробки вхідних повідомлень боту — писати йому команди поки
  що марно, Velesha їх не читає. Якщо колись знадобиться повноцінний
  Telegram-інтерфейс (вхідні команди) — окрема робота (webhook або
  long-polling на updates, авторизація відправника проти
  `TELEGRAM_CHAT_ID_*`).
