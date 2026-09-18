# Home Assistant source

Indexes recent HA history (light/switch/lock/cover/climate/binary_sensor/
person/media_player/alarm_control_panel domains) into Qdrant, as short
Ukrainian sentences — see ADR-0004 for why, and `textify.py` for the actual
event → sentence rules.

`browser_mod_*` entities (kiosk tablet motion/screen sensors) and entities
with no real name (`*unknown*`) are excluded — they're noise, not signal:
see the 2026-09-18 log entry in the repo root README for what that looked
like before filtering.

## Running it

Secrets are encrypted (ADR-0005) — never read from a plaintext file:

```bash
uv sync
sops --config /dev/null exec-env secrets.enc.env 'uv run ingest.py --days 3'
uv run search.py "коли вмикали телевізор"
```

Requires the embedding server and Qdrant from the repo root README to be up.
