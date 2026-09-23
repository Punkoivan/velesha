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

exec sops exec-env "$ROOT/secrets.enc.env" \
  "sops --config /dev/null exec-env $ROOT/api/secrets.enc.env 'exec uv run uvicorn main:app --host 0.0.0.0 --port 8090'"
