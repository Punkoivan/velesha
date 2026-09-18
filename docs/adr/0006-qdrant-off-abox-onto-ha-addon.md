# ADR-0006: Move Qdrant off `abox`, onto the persistent `ha-addon-qdrant` instance

- **Status**: accepted
- **Date**: 2026-09-18

## Context

ADR-0003 pointed Velesha at the Qdrant instance already running inside the
`abox` KinD cluster, flagged explicitly as a temporary coupling: "Velesha
currently has a soft runtime dependency on the `abox` cluster being up and
port-forwarded. This is a known temporary coupling, not a design goal."

That dependency broke for real: `abox` got torn down and rebuilt from a
different branch (course exercise — see `harness-course` log,
2026-09-18) to test a much larger stack (llm-d, neo4j, qdrant-mcp). The
cluster's Qdrant is ephemeral by nature — every `tofu destroy` /
`tofu apply` cycle is a fresh instance with empty storage. Both
`obsidian_recipes` and `ha_history` collections were lost outright.

`ha-addon-qdrant` (the user's own Home Assistant add-on, wrapping Qdrant)
was sitting right there the whole time as a durable alternative: it runs
as part of the HA instance, its data is included in HA's own backups
automatically (per that add-on's README), and it isn't tied to a
course-cluster experiment that gets rebuilt on a whim.

## Decision

Point Velesha's Qdrant at the `ha-addon-qdrant` instance instead of
`abox`: `https://ha.punka.space:6333` (the HA host's domain, not its raw
IP — see gotcha below), authenticated with the add-on's `api_key`.

TLS needs no special handling: the cert (`qdrant-fullchain.pem` on the HA
host) is a real Let's Encrypt cert for `*.punka.space`, already trusted by
any standard CA bundle. The `.punka.space` project also has its own local
CA (`Punka Local CA`, for other self-signed local services) — that one is
**not** relevant here and was a dead end (see gotcha).

Connection details move to a **shared** secret file at the repo root,
`secrets.enc.env` (not per-source like `sources/home_assistant/`'s — every
source needs Qdrant, only one source currently needs the HA API too):
`QDRANT_URL`, `QDRANT_API_KEY`. Same SOPS/age mechanism as ADR-0005.

**Gotcha hit for real**: `HA_URL` in this project points at the host's raw
IP (`192.168.88.80`), which was the first thing tried for `QDRANT_URL` too
— `curl` failed with `SSL: no alternative certificate subject name
matches target IPv4 address`, because the cert's SAN is `*.punka.space`,
not the IP. Switching to the domain (`ha.punka.space`, which resolves
locally to the same IP) fixed the SAN mismatch, but then hit `unable to
get local issuer certificate` — the instinctive fix (pass the project's
`Punka Local CA` as a custom trust root) was wrong: `openssl verify`
against that CA failed too, because the leaf cert's real issuer is
Let's Encrypt, not the local CA. Dropping the custom CA entirely and
trusting the system's default bundle is what actually worked.

## Alternatives considered

- **Keep using `abox`'s Qdrant, just remember to re-index after every
  cluster rebuild** — rejected outright; that's not a design, that's
  accepting data loss as a recurring event and hoping to remember.
- **A dedicated Velesha-only Qdrant** (docker-compose, its own storage) —
  still on the table for later (this was ADR-0003's other alternative),
  but `ha-addon-qdrant` already exists, is already backed up, and needs
  zero new infrastructure. Revisit only if Velesha's needs outgrow
  sharing storage with HA's own vector workloads, if any.

## Consequences

- Velesha no longer needs `abox` running or port-forwarded at all for
  Phase 1 sources — the `abox` KinD cluster is now purely a course
  artifact, not a Velesha runtime dependency. This closes out `PLAN.md`'s
  "Phase 0 — Decouple from `abox`" item.
- `obsidian_recipes` and `ha_history` need re-indexing from scratch on the
  new Qdrant (empty collections there) — one-time cost of this migration.
- Qdrant now shares infrastructure with the Home Assistant instance
  itself. If HA goes down or the add-on is stopped, so does Velesha's
  search — an explicit tradeoff for durability or address changes if the
  add-on's connection details change is minor since they're centralized
  as of this ADR.
