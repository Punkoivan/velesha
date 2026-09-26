# ADR-0053: jellyfin-mcp патч — start_position_ticks для точного переносу сесії

- **Status**: accepted
- **Date**: 2026-09-26

## Контекст

«Перенеси серіал з Kodi на ноутбук» після виправлення allowlist'у
(ADR-0052) стартував на новому пристрої з нуля/наступної серії, а не з
точної миті, де зупинилось відтворення. Сам Jellyfin API (`POST
/Sessions/{id}/Playing`) підтримує `StartPositionTicks`, і `jellyfin_sessions`
уже повертає `PlayState.PositionTicks` для сесії-джерела — дані є,
але `jellyfin-mcp` (`internal/jellyfin/types.go: PlayInput`) не мав поля
для цього параметра й не передавав його у виклик.

## Рішення

Другий локальний патч на `jellyfin-mcp` (перший — auth-header,
ADR-0038): `api/jellyfin-mcp-start-position.patch` додає
`StartPositionTicks *int64` у `PlayInput` і `params.Set("StartPositionTicks", …)`
у обробнику `jellyfin_play`, коли поле задане. Системний промпт
(`_CONTROL_LINES["jellyfin_play"]`) тепер явно каже: для переносу сесії —
взяти `item_id` і `PlayState.PositionTicks` із `jellyfin_sessions` для
пристрою-джерела, передати їх у `jellyfin_play` разом із
`start_position_ticks` на пристрій-ціль.

## Наслідки

- Обидва патчі (auth-header, start-position) застосовуються послідовно
  при перезбірці бінарника — команда в ADR-0038 оновлена.
- Варто надіслати обидва як PR у upstream `jaredtrent/jellyfin-mcp`.
