# ADR-0026: Model comparison; OpenAI `gpt-4.1-mini` as the working model

- **Status**: accepted
- **Date**: 2026-09-19

## Context

The local 3B model hit its ceiling (ADR-0018…0024: wrong tool choice,
misread tool output, failed multi-step/general questions). Keys for
OpenAI and Gemini were provided; guardrails (ADR-0025) were in place.
Before switching, the same 10-question, read-only set was run on each
backend with `api/eval_models.py`. Ground truth is computed live from
the real systems (door state, qBittorrent counts, AdGuard delta, washing
machine kWh, last TV-on time), so the checks don't go stale; a check
that only passed by luck was fixed (a hedged "відчинені… можуть бути й
закритими" no longer counts as correct).

Policy constraint honoured by the harness: the **free Gemini tier never
sees a tool result** (ADR-0025). On tool questions it is only asked to
*choose a tool* (first step, then the run stops); tool-free questions
(Tarantino recommendation, "who directed Pulp Fiction") are answered
in full.

## Results

| Backend | Score | Latency | Notes |
|---|---|---|---|
| Local Qwen2.5-3B | **7/10** | 3–80 s per question, 282 s total | used `get_live_state` for a history question (AdGuard), listed non-Tarantino films as Tarantino's, refused a general-knowledge question |
| OpenAI `gpt-4.1-mini` | **30/30** (3 runs × 10) | ~2 s per question | ~2.7k tokens/question (~80k for 30 runs) |
| Gemini `gemini-flash-latest` (free) | tool choice **10/10**, general **2/2** | 2–11 s | 4 of 10 requests in a burst were refused by the service (one 503, three 429) until spaced 20 s apart |

## What the evaluation found (beyond model quality)

Several failures were **system bugs the model merely exposed**, fixed
in this change:
- **Stale index for "коли востаннє".** `search_knowledge` reads the
  Qdrant `ha_history` snapshot, last ingested on 18.09, so a correct
  tool call still gave a stale answer (TV "last on 18:32" when it was
  on again at 11:27 next day). `get_sensor_history` now accepts
  `start_date="тиждень"` (last 7 days) and its description/system
  prompt name it as the live source for "коли востаннє"; `search_knowledge`
  is described as a possibly stale snapshot. Re-ingesting on a schedule
  would also help other uses — not done.
- **Model-guessed years.** Asked about "18.09" `gpt-4.1-mini` sent
  2023/2024 dates *despite today's date in the prompt* (1 of 3 correct).
  Fixed in code: `_parse_day` accepts `18.09`/`18.09.2026` and resolves a
  missing year to the latest non-future date; the tool description says
  not to invent a year. Afterwards 30/30.
- **Over-tooling.** Asked for a Tarantino recommendation the model
  searched Jellyfin for the director (not in the data) and gave up; the
  prompt now says general knowledge questions are answered without tools.

## Decision

- **Working model: OpenAI `gpt-4.1-mini`**, enabled with
  `CHAT_PROVIDER=openai` (reuses `OPENAI_API_KEY`, so the key is stored
  once) and `CHAT_MODEL=gpt-4.1-mini`; unset → local model as before.
  Verified live through HA's conversation API (TV last-on, door state,
  Tarantino suggestions; ~2 s each; audit shows `LLM hosted`).
- The daily budget (500 000 tokens) and local fallback from ADR-0025
  stay. The harness's own spend is counted in the same file, so a heavy
  evaluation day leaves less for live use (330 991 used that day).
- **Free Gemini is viable but not wired**: quality is enough for
  tool choice and general questions, but the free tier rate-limits a
  burst and Google's current model names change (`gemini-2.5-flash` was
  "no longer available to new users" with a 404; the `gemini-flash-latest`
  alias worked). It remains the planned tool-free general-question lane
  (ADR-0025) with the local model as the 429/503 fallback.

## Alternatives considered

- **Bigger OpenAI model first** — not needed: 30/30 on this set.
- **Stay local** — 7/10 with real errors on the questions the user
  actually asks; the guardrails make the hosted route acceptable.
- **Gemini free as the main model** — rate limits and the training-data
  policy make it a poor fit for a tool-using agent that handles home data.

## Consequences

- The 10-question set is small and read-only; it does not measure
  action tools (play, add torrent) — those are protected by code gates,
  not model judgment (ADR-0021/0023/0024) — nor long multi-step chains
  (Tarantino → check watched → Toloka → add), which the next tests
  should cover.
- Non-determinism is real (one question flipped 1/3 → 3/3 only after a
  code fix): keep using `EVAL_RUNS=3` when comparing.
- Ground truth for the washing-machine question is pinned to 18.09 and
  will expire when HA's ~10-day history rolls over.
- To revert to fully local: restart the server without `CHAT_PROVIDER`.

## Correction (same day)

The first version of this ADR said the local model got the door state
wrong. **That was my mistake**: the local run happened at 15:29–15:33
Kyiv time, after the door had been opened (15:21:55), so its answer
"відчинені" was correct and its original PASS stood; I re-scored the saved
answer assuming the door was closed. The local score is **7/10**, not
6/10. The door question was also unreliable for a second reason found the
same hour: HA has two sensors for one door and the stale one contradicted
the live one — see ADR-0027. After that fix the door question is 3/3
(OpenAI) and 2/2 (local) with the truth being "open".
