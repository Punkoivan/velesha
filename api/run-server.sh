#!/usr/bin/env bash
# Launches the Velesha API under systemd (ADR-0045) — foreground, no nohup/disown,
# systemd owns the process and captures stdout/stderr into the journal.
set -euo pipefail
ROOT="$HOME/projects/velesha"
cd "$ROOT/api"

export CHAT_PROVIDER=openai
export CHAT_MODEL=gpt-5.6-luna
export CHAT_REASONING=none   # gpt-5.6-luna refuses function tools without this (ADR-0037)
export LOG_CALLER_PROMPT=1
export REMINDER_NOTIFY_DEFAULT=notify.mobile_app_punkas26  # server-side reminder push, not the HA calendar automation (ADR-0049)
export JELLYFIN_PLAY_ALLOW=kodi,firefox  # playback targets allowed besides Kodi — laptop's Jellyfin Web session (ADR-0052)
export TELEGRAM_CHAT_ID_DEFAULT=333751480  # Telegram notification channel alongside HA notify (ADR-0054)
export VELESHA_DEFAULT_USER=punka  # requests without a device marker (ADR-0069)
export VELESHA_NAMES="punka:Іван"  # how memory names a person in auto-learned facts
export VELESHA_DEVICE_USERS="punka-mobile:punka,punka-ha:punka,mac:punka"  # HA user -> person; other devices are shared
export PET_FEED_PRODUCT_ID=169  # Grocy: dog food, consumed automatically per feeding (ADR-0062)
export PET_FEED_SCHEDULE="08:00=75,20:00=75"  # feeding time=grams
export PET_FEED_WARN_DAYS=7
export PET_FEED_PACK_G=18000  # added to Grocy shopping list when running low
export OTEL_EXPORTER_OTLP_ENDPOINT=http://192.168.88.7:4318  # OpenTelemetry traces (ADR-0059)

exec sops exec-env "$ROOT/secrets.enc.env" \
  "sops --config /dev/null exec-env $ROOT/api/secrets.enc.env 'exec uv run uvicorn main:app --host 0.0.0.0 --port 8090'"
