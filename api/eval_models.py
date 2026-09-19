"""Compare chat backends on one fixed question set (ADR-0026).

Usage (from api/, secrets chained like the server):
    sops exec-env ../secrets.enc.env 'sops --config /dev/null exec-env secrets.enc.env \\
      "uv run python eval_models.py local|openai|gemini"'

Read-only questions only. Ground truth is computed live from the real
systems, so checks don't rot. Backend `gemini` is the FREE tier: by policy
(ADR-0025) it must never see a tool result, so on tool questions it is only
asked to pick a tool (first step, then stop); it answers tool-free ones fully.
"""

import datetime
import json
import os
import re
import sys
import time
import zoneinfo

BACKEND = sys.argv[1]
if BACKEND == "openai":
    os.environ.update(CHAT_API_KEY=os.environ["OPENAI_API_KEY"], CHAT_MODEL=os.environ.get("EVAL_MODEL", "gpt-4.1-mini"))
    os.environ.pop("CHAT_URL", None)
elif BACKEND == "gemini":
    os.environ.update(
        CHAT_API_KEY=os.environ["GEMINI_API_KEY"],
        CHAT_MODEL=os.environ.get("EVAL_MODEL", "gemini-flash-latest"),
        CHAT_URL="https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
    )
else:
    os.environ.pop("CHAT_API_KEY", None)
os.environ["DAILY_TOKEN_BUDGET"] = "10000000"  # eval is explicit spend, not the daily cap

import guardrails, ha_client, main, qbit_client, tools  # noqa: E402

TZ = zoneinfo.ZoneInfo("Europe/Kyiv")
CALLS, TOKENS = [], [0]


class Stop(Exception):
    pass


_real_call_tool = main.call_tool
_real_record = guardrails.record_usage


def fake_call_tool(name, args, user_text=""):
    CALLS.append(name)
    if BACKEND == "gemini" and SELECT_ONLY:
        raise Stop
    return _real_call_tool(name, args, user_text)


def rec(n):
    TOKENS[0] += n
    _real_record(n)


main.call_tool, guardrails.record_usage, main.guardrails.record_usage = fake_call_tool, rec, rec
if BACKEND != "local":  # a silent fallback would make the comparison meaningless
    def no_fallback(*a, **k):
        raise RuntimeError("FALLBACK-to-local")
    main._complete_local = no_fallback

# ---- ground truth, live -------------------------------------------------
door = next(  # by exact friendly name — entity ids get renamed in HA
    s["state"] for s in ha_client.get_states()
    if s["entity_id"].startswith("binary_sensor.") and s["attributes"].get("friendly_name") == "вхідні двері"
)
zero_ratio = sum(1 for t in qbit_client.torrents() if t["progress"] >= 1 and t["ratio"] == 0)
session_gb = qbit_client.server_state()["up_info_data"] / 1024**3
adguard = re.search(r"додалось (\d+)", tools.tool_get_sensor_history({"entity_name": "DNS запити", "start_date": "вчора"}))
adguard = adguard.group(1) if adguard else None
energy = re.search(r"приріст лічильника ([\d.]+)", tools.tool_get_sensor_history({"entity_name": "пралка електроенергія", "start_date": "2026-09-18"}))
energy = energy.group(1) if energy else None
end = datetime.datetime.now(datetime.timezone.utc)
ons = [h for h in ha_client.get_history("switch.tv", (end - datetime.timedelta(days=9)).isoformat(), end.isoformat()) if h["state"] == "on"]
last_on = datetime.datetime.fromisoformat(ons[-1]["last_changed"])
tv_times = {last_on.strftime("%H:%M"), last_on.astimezone(TZ).strftime("%H:%M")}
TARANTINO = r"(кримінальн|pulp|джанго|django|вбити білла|убити білла|kill bill|скажен|reservoir|джекі браун|jackie|безслав|inglourious|голлівуд|hollywood|мерзенн|hateful|доказ смерті|death proof)"

def door_ok(a: str) -> bool:
    """Says the true state and does not also claim the opposite (a hedged
    'відчинені … можуть бути й закритими' must not pass)."""
    closed = bool(re.search("закрит|зачин", a, re.I))
    opened = bool(re.search(r"(?<!не )(відчин|відкрит)", a, re.I))
    return (closed and not opened) if door == "off" else (opened and not closed)


# id, question, first tool(s) accepted, answer check, tool-free?
QUESTIONS = [
    ("door", "чи відчинені вхідні двері?", {"get_live_state"}, lambda a: door_ok(a)),
    ("ratio0", "скільки роздач з рейтингом 0?", {"qbittorrent_list", "qbittorrent_status"}, lambda a: str(zero_ratio) in a),
    ("session", "скільки віддано в qBittorrent за сесію?", {"qbittorrent_status"}, lambda a: any(abs(float(x.replace(",", ".")) - session_gb) < 1.5 for x in re.findall(r"\d+[.,]?\d*", a))),
    ("adguard", "скільки запитів пройшло через AdGuard вчора?", {"get_sensor_history"}, lambda a: adguard is not None and adguard in a.replace(" ", "")),
    ("energy", "скільки 18.09 числа пралка використала електроенергії?", {"get_sensor_history"}, lambda a: energy is not None and energy in a),
    ("tv", "коли востаннє було вімкнено телевізор?", {"get_sensor_history"}, lambda a: any(t in a for t in tv_times)),
    ("recipe", "порадь щось із куркою на вечерю", {"search_knowledge"}, lambda a: "курк" in a.lower()),
    ("toloka", "знайди Mandy 2018 на толоці", {"toloka_search"}, lambda a: "Толоці" in a and "Mandy" in a or "Менді" in a),
    ("tarantino", "хочу подивитись якийсь фільм Тарантіно, запропонуй", None, lambda a: bool(re.search(TARANTINO, a, re.I)) and "Темпл" not in a and "Інтерстеллар" not in a),
    ("general", "хто зняв фільм Кримінальне чтиво і в якому році він вийшов?", None, lambda a: "тарантіно" in a.lower() and "1994" in a),
]

RUNS = int(os.environ.get("EVAL_RUNS", "1"))  # models are non-deterministic: repeat for a rate
rows = []
ONLY = set(filter(None, os.environ.get("EVAL_ONLY", "").split(",")))
PAUSE = float(os.environ.get("EVAL_PAUSE", "0"))  # free tiers rate-limit; spread the requests
for qid, text, tool_ok, check in [q for q in QUESTIONS if not ONLY or q[0] in ONLY for _ in range(RUNS)]:
    time.sleep(PAUSE)
    SELECT_ONLY = tool_ok is not None
    CALLS.clear(); TOKENS[0] = 0
    off = main.tools_for(text)
    msgs = [{"role": "system", "content": main.system_prompt({t["function"]["name"] for t in off})}, {"role": "user", "content": text}]
    t0 = time.time()
    try:
        answer = main.run_agent(msgs, off, text)
    except Stop:
        answer = ""
    except Exception as e:  # noqa: BLE001
        answer = f"ERROR {type(e).__name__}: {e}"
    dt = time.time() - t0
    first = CALLS[0] if CALLS else None
    if BACKEND == "gemini" and SELECT_ONLY:
        ok, how = first in tool_ok, "tool-choice"
    elif tool_ok is None:
        ok, how = check(answer), "answer"
    else:
        ok, how = check(answer) and first in tool_ok, "answer+tool"
    rows.append(dict(id=qid, ok=bool(ok), how=how, first_tool=first, calls=len(CALLS), secs=round(dt), tokens=TOKENS[0], answer=answer[:160]))
    print(f"{'PASS' if ok else 'FAIL'} {qid:9} {how:12} tool={first!s:22} {dt:5.0f}s tok={TOKENS[0]:6}  {answer[:90]!r}", flush=True)

n = sum(r["ok"] for r in rows)
print(f"\n{BACKEND}: {n}/{len(rows)} passed, {sum(r['secs'] for r in rows)}s total, {sum(r['tokens'] for r in rows)} tokens")
json.dump(rows, open(f"/tmp/eval_{BACKEND}.json", "w"), ensure_ascii=False, indent=1)
