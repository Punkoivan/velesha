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
export REMINDER_SPEAK_S21_VOICE=mobile_app_s21_voic4  # reminders asked by voice on s21 are also said aloud there (ADR-0085)
export HA_CONTROL_ALLOW=switch.tv,light.zala_1,light.zala_2,light.zala_3,light.korydor_1,light.korydor_2,switch.nidzya,switch.posudomyika  # devices ha_switch may control: TV, hall and corridor lamps, Ninja and dishwasher plugs
export JELLYFIN_PLAY_ALLOW=kodi,firefox,tv5  # playback targets: Kodi, laptop Jellyfin Web (ADR-0052), Jellyfin Android TV app on the iNeXT box (device "TV5")
export TELEGRAM_CHAT_ID_DEFAULT=333751480  # Telegram notification channel alongside HA notify (ADR-0054)
export TELEGRAM_ALLOWED_USERS="333751480:punka"  # who may talk to the bot (Telegram user id:person), ADR-0070
export TELEGRAM_FAMILY_CHATS="-4024131871"  # «Кіно і не тільки» — family group with home control (ADR-0077)
export VELESHA_DEFAULT_USER=punka  # requests without a device marker (ADR-0069)
export VELESHA_ADMIN_DEVICES=s21-voice,telegram-group,telegram-private  # shared device trusted with admin tools: torrents, Toloka (ADR-0072)
export VELESHA_ADMINS=punka  # Toloka/qBittorrent (ADR-0035); without it "punka" lost admin when ADR-0069 renamed "default"
export VELESHA_NAMES="punka:Іван,marina:Марина"  # how memory names a person in auto-learned facts
export VELESHA_DEVICE_USERS="punka-mobile:punka,punka-ha:punka,mac:punka"  # HA user -> person; other devices are shared
export PET_FEED_PRODUCT_ID=169  # Grocy: dog food, consumed automatically per feeding (ADR-0062)
export PET_FEED_SCHEDULE="08:00=75,20:00=75"  # feeding time=grams
export PET_FEED_WARN_DAYS=7
export PET_FEED_PACK_G=18000  # added to Grocy shopping list when running low
export OTEL_EXPORTER_OTLP_ENDPOINT=http://192.168.88.7:4318  # OpenTelemetry traces (ADR-0059)

exec sops exec-env "$ROOT/secrets.enc.env" \
  "sops --config /dev/null exec-env $ROOT/api/secrets.enc.env 'exec uv run uvicorn main:app --host 0.0.0.0 --port 8090'"
