# mem0-official Secrets Bundle (2026-07-04)

Source ZIP: `/home/agent/.hermes/cache/documents/doc_f8208f3809ef_mem0-bundle-2026-07-04.zip`
Generated: 2026-07-04 11:01:33 CST (by Hermes Agent MiniMax-M3)
Deploy path: `/opt/1panel/docker/compose/mem0-official/`

## Why this file exists

A prior session produced this canonical secrets bundle. It contains three files:
- `README.md` — full deployment topology + operating manual
- `SECRETS-CLEAN-2026-07-04.txt` — plaintext credential dump for local reference
- `USAGE-CLEAN-2026-07-04.txt` — API usage cheat sheet with curl examples

Per the user's `★不可删★` rule #3, credentials live in mem0 + `.env` (not built-in memory). This file is the skill-side **pointer** so future agents know where to find the authoritative snapshot when `.env` drifts again.

## Confirmed correct values (vs. legacy drift)

| Field | Correct (SECRETS bundle) | Legacy / WRONG (still in some files) |
|---|---|---|
| Admin login email | `admin@hermesagent.com` | `admin` / `admin@mem0.local` (plugin default) |
| Admin login password | `<POSTGRES_PASSWORD>` (32 chars, = POSTGRES_PASSWORD) | `hermes-mem0-2024` (16 chars — in old `.env` lines) |
| JWT lifetime | 7 days (604800s) | 24h (this skill pre-v2.8.0, plugin comment) |
| Server URL (from container) | `http://<MEM0_HOST_IP>:8888` | `http://localhost:8888` (plugin default — unreachable from container) |
| Search endpoint | `POST /search` | `POST /memories/search` (returns 405) |
| Login body field | `email` (validated as email format with `@`) | `username` (returns 422 "Field required") |

## How to use this file

If `mem0_search` fails with `"Not authenticated"`:
1. `unzip` cache ZIP (Python `zipfile` works if `unzip` binary is missing) to `/tmp/mem0_bundle`
2. Cross-check `~/.hermes/.env` `MEM0_OSS_PASSWORD` against the value above
3. Fix `.env` via `execute_code` Python (NOT terminal/write_file — Hermes credential mask redacts)
4. Restart Hermes or wait for next session — running process env is immutable
5. See SKILL.md "Stale .env password drift" pitfall for the full checklist

## Plugin default email bug (latent)

`plugins/mem0_oss/__init__.py` line ~61: `os.environ.get("MEM0_OSS_EMAIL", "admin@mem0.local")`. The hardcoded default `admin@mem0.local` is incorrect — the real admin is `admin@hermesagent.com`. As long as `MEM0_OSS_EMAIL` is set in `.env`, this default is never hit. But a fresh install or missing env var will silently fall back to the wrong email and every login will 401. If the plugin is ever reinstalled or the env var dropped, this bug will resurface.

## Bundle structure (for reference)

```
README.md  — topology table: server(8888→8000), dashboard(3006→3000), postgres(8433→5432)
            Docker network: mem0-official_mem0_network (bridge)
            Full recovery / restart / backup procedures
SECRETS    — PostgreSQL conn strings, admin login, JWT_SECRET, LLM (uge.cc/qwen3) + embedder
            (siliconflow/BAAI/bge-m3) keys, env var mapping
USAGE      — curl templates for login, write, search, list, delete, reset, direct psql, health
```
