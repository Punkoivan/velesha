# ADR-0038: Jellyfin через MCP-сервер, власні інструменти вилучено

## Контекст
Наші Jellyfin-інструменти (`jellyfin_find`, `play_on_jellyfin_device`,
`control_jellyfin_playback`, `jellyfin_now_playing`, `jellyfin_mark_watched`)
дублювали готовий MCP-сервер. Рішення користувача: дублікатів не тримати.
Обрано [jaredtrent/jellyfin-mcp](https://github.com/jaredtrent/jellyfin-mcp)
(Go, MIT, найпопулярніший із знайдених; коміт 307e28e). Код переглянуто лише
вибірково: залежності мінімальні (офіційний MCP Go SDK, cobra), HTTP-клієнт ходить
лише на `JELLYFIN_URL`. Сервер піднімається дочірнім процесом через stdio.

## Рішення
- `adk_agent.py`: `McpToolset` із `tool_filter`. Читання (`search`, `browse`, `get_item`,
  `recommendations`, `people`, `tv_shows`, `analytics`, `sessions`) — завжди;
  `play`/`playback_control` — лише з командною фразою (`_COMMAND_RE`);
  `user_data` — лише з фразою «познач/відміть/проставити…». Групи сервера:
  `discovery,media,user,playback,analytics`; admin, content, livetv, music,
  system_info не вмикаються. Під час імпорту рецепта (ADR-0033) Jellyfin прихований.
- Другий замок — `_jellyfin_guard` перед кожним записом: сесія має бути Kodi
  (allowlist з ADR-0021), команди керування лише `Pause/Unpause/Stop/NextTrack/
  PreviousTrack/Seek/Mute/Unmute/ToggleMute/SetVolume`, дії `user_data` лише
  `mark_played/mark_unplayed/get_user_data`, напрямок відмітки визначають слова
  користувача, не модель. `SendMessage`, `GoHome`, `set_user_data`, рейтинги й
  обране заблоковано.
- Залишок часу до кінця рахується кодом (`_with_remaining`) і додається до
  відповіді `jellyfin_sessions`; модель його лише переказує.
- `JELLYFIN_USER_ID` передається явно (спільний профіль `tv`); без нього сервер
  сам бере адмін-користувача.
- **Несумісність з Jellyfin 12.1:** сервер автентифікується заголовком
  `X-MediaBrowser-Token`, який Jellyfin 12.1.0 відхиляє (401; `X-Emby-Token` так
  само). Працює `Authorization: MediaBrowser Token="…"`. Локальний патч —
  `api/jellyfin-mcp-auth-header.patch` (2 рядки). Бінарник збирається так:
  `git clone … && git checkout 307e28e && git apply <patch> && go build -o api/bin/jellyfin-mcp .`
  (`api/bin/` у .gitignore). Варто надіслати PR у upstream.
- MCP працює лише в ADK: `AGENT_ENGINE` за замовчуванням тепер `adk`; старий рушій
  лишився в коді без Jellyfin (далі його варто видалити).

## Що втрачено проти власного коду
- Очікування завантаження Kodi (до 60 с): якщо Kodi вимкнений, сесії немає, і
  агент так і каже.
- Автоматичний вибір «наступної серії» — тепер модель викликає `tv_shows` (next unplayed).
- Перевірка результату відмітки повторним читанням: тепер довіряємо відповіді сервера
  (`is_error`).

## Результат
eval (ADK, luna, 3 прогони): 30/30. Але схеми MCP-інструментів подвоїли токени
на запит (≈8.6k замість ≈4.5k на просте питання); при ціні luna це копійки.
Ворота перевірено юніт-тестами на живих сесіях; реальні play/pause на Kodi не
запускалися (це б перервало перегляд).
