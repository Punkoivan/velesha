# ADR-0005: Secrets via SOPS + age, no plaintext .env in the repo

- **Status**: accepted
- **Date**: 2026-09-18

## Context

`sources/home_assistant/` needs `HA_URL` and a long-lived access token to
call the HA REST API. The obvious move — a `.env` file, gitignored — works,
but a blanket `.env` gitignore rule across the whole repo is easy to trip
over later: a future non-secret `.env.example`, or a source that legitimately
wants to version its config, gets swept up by the same pattern, and it's
never obvious at a glance which `.env` is intentionally untracked vs.
accidentally excluded.

## Decision

Encrypt secrets with [SOPS](https://github.com/getsops/sops) using an
[age](https://github.com/FiloSottile/age) key pair, and commit the
**encrypted** file. No plaintext secret file exists in the repo at any
point — nothing to gitignore, nothing to forget to gitignore.

- Key: age keypair, private half at `~/.config/sops/age/keys.txt` (outside
  the repo, backed up in the user's password manager), public half
  registered in `.sops.yaml` at the repo root.
- Format: one `secrets.enc.env` per source that needs secrets
  (`sops --input-type dotenv --output-type dotenv`), so decrypted output is
  a normal `.env` — keys stay readable in the encrypted file (`HA_TOKEN=...`
  as a key name), only values are encrypted.
- Usage: scripts never read a plaintext file. Run them through
  `sops exec-env <source>/secrets.enc.env '<command>'`, which decrypts into
  the subprocess's environment only, in memory, for that one invocation.

Example for the HA source:

```bash
sops exec-env sources/home_assistant/secrets.enc.env 'uv run ingest.py'
```

To edit secrets: `sops sources/home_assistant/secrets.enc.env` (opens
decrypted in `$EDITOR`, re-encrypts on save — never touches disk
unencrypted outside the editor's temp file, which SOPS shreds after).

## Alternatives considered

- **Plaintext `.env` + gitignore** — simplest, but exactly the "which .env
  is safe to commit" confusion this ADR exists to avoid, and secrets aren't
  versioned (rotate a token, the old one isn't recoverable from git history
  — arguably a feature, but not one we chose deliberately).
- **A secrets manager / vault service** (Vault, Doppler, 1Password CLI,
  etc.) — real overkill for a single-user local project; adds a network
  dependency for something that should work fully offline.
- **`direnv` + local-only `.envrc`** — avoids committing secrets at all,
  but then secrets live nowhere durable; re-cloning the repo on another
  machine means re-entering every token by hand. SOPS gives us that for
  free since the encrypted file travels with the repo.

## Consequences

- Every source that needs secrets gets a `secrets.enc.env` next to its
  code, committed normally.
- Losing `~/.config/sops/age/keys.txt` with no backup means losing access
  to every secret ever encrypted with it — this is why it goes in the
  password manager, not just on disk.
- Onboarding a second machine (or restoring after a wipe) means: import
  the age private key into `~/.config/sops/age/keys.txt`, everything else
  (`git clone`, `sops exec-env`) just works.
- CI, if this project ever gets any, would need the age private key as a
  secret itself — not a concern yet with zero automation, worth revisiting
  if that changes.
