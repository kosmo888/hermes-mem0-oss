# mem0-server upgrade transcript — v2.0.5 → v2.0.11 (2026-07-05)

End-to-end record of the upgrade: backup → rebase → build → restart → diagnose the residual 401 → discover the `config_overrides` override mechanism. **Read this before attempting another mem0 upgrade.**

## TL;DR

- **Upgraded**: `mem0ai` Python SDK from `2.0.5` (`8399b08`) to `2.0.11` (`cd79fa8`, 181 commits).
- **Lost on rebase**: 0 of 6 patches were truly lost, but **dual-provider patch on `main.py` got auto-merged away** when `git checkout --ours` resolved the list-API conflicts; had to re-apply manually.
- **Post-upgrade 401 was fixed** (see Step 9-B): After restart, `POST /memories` returned 401 even though compose `OPENAI_API_KEY=sk-leql9Cl...` was valid and direct `Memory.from_config(...).add(...)` worked. Root cause: PostgreSQL table `mem0_app.settings.config_overrides` contained a JSONB blob with the **stale legacy key `sk-n7ineS6V...`** from an old `POST /configure` call. `initialize_state()` merges this on top of env vars, so the runtime config wins with the stale key while env vars look correct. **Fix applied**: `UPDATE mem0_app.settings SET value = jsonb_set(value::jsonb, '{llm,config,api_key}', '"sk-leql9Cl..."', false) WHERE key='config_overrides'` + `docker restart mem0-server` → POST /memories now returns 200 with 5.5s latency.
- **Breaking change in v2.0.11**: `/health` endpoint removed; use `/docs` for liveness probes.
- **Stale skill entries**: previous skill versions named `sk-n7ineS6V...` as the "server-side key". This key now returns 401 on api.uge.cc. The current valid key (used by both client and server) is `sk-<CURRENT_LLM_API_KEY_REDACTED>`.

## Step 0 — Inventory before any change

```bash
ssh root@<MEM0_HOST_IP> 'cd /opt/1panel/docker/compose/mem0-official/server-src && \
  grep ^version pyproject.toml && \
  git log -1 --oneline && \
  git rev-list --left-right --count HEAD...origin/main'
# → version = "2.0.5"
# → 8399b08 chore: release Python SDK v2.0.5 and TypeScript SDK v3.0.7 (#5470)
# → 0\t181   (181 commits behind main)

ssh root@<MEM0_HOST_IP> 'cd .../server-src && git status -s'
# M server/dashboard/Dockerfile
# M server/db.py
# M server/dev.Dockerfile
# M server/errors.py
# M server/main.py                ← dual-provider patch (critical)
# M server/server_state.py
```

## Step 1 — Dual-host backup

Two backups made (Oracle + local-container), SHA256 cross-verified. Full recipe in main SKILL.md §"Server Upgrade Preparation".

- Oracle-side: `/opt/1panel/docker/compose/mem0-official/backups/20260705_173820/`
  - `mem0-local-patches.patch` (337 lines, SHA256 f31fb702…)
  - `orig/` (HEAD-clean versions, per-file SHA256 manifest)
  - `modified/` (current with-patch versions, per-file SHA256 manifest)
  - `SNAPSHOT.txt` (env snapshot for forensics)
  - `mem0-server.log.before-upgrade` (50-line tail of container logs)
- Local-side: `/home/agent/.hermes/cache/work/mem0-backups/20260705_173820/mem0-backup.tar.gz`
  - SHA256 = `5aa08bfe8532ea4fc8029c38e0d5c909a7bd672d49162e4857f1b8b2716be58d`

## Step 2 — Patch portability dry-run

Critical pre-flight: confirm the patch survives a fresh clone at the exact local HEAD. If `git apply --check` fails, the patch has drift (line endings, whitespace, or upstream moved past it) and must be regenerated before any upgrade.

```bash
TMP=/tmp/patch-verify-$$
git clone --quiet https://github.com/mem0ai/mem0.git $TMP
cd $TMP
git checkout --quiet 8399b088a563505ca68d59c2c541386067f6884a
git apply --check <BACKUP_DIR>/mem0-local-patches.patch   # → exit 0
git apply <BACKUP_DIR>/mem0-local-patches.patch
git diff --stat
# → 6 files changed, 134 insertions(+), 42 deletions(-)
rm -rf $TMP
```

## Step 3 — Commit the local patch as a single commit (makes rebase tractable)

Without this, rebase cannot preserve the patch — it operates on commit boundaries.

```bash
cd .../server-src
git add -A
git commit -m "hermes: local patches (dual-provider embedder, db, errors, server_state, dockerfiles)"
# → febf34a
```

## Step 4 — Rebase onto upstream

```bash
git fetch origin main
git rebase origin/main
# → Rebasing (1/1)
# → Auto-merging server/main.py
# → CONFLICT (content): Merge conflict in server/main.py
```

Three conflict regions in `server/main.py` — all in the list-API section that upstream refactored from `page`/`size` to `top_k` + `show_expired`:

| Conflict location | Upstream (HEAD) | Local (febf34a) | Decision |
|---|---|---|---|
| `_RESERVED_PAYLOAD_KEYS` | includes `expiration_date` | doesn't | **Take upstream** (new feature) |
| `/memories` query params | `top_k` + `show_expired` | `page` + `size` | **Take upstream** (new API surface) |
| admin listing body | `top_k`-based | `page`/`size`-based | **Take upstream** (matches new params) |

```bash
# In rebase context: --ours = LOCAL patch, --theirs = UPSTREAM commit.
# For "upstream rewrote API, local didn't touch it" conflicts, the correct choice is --theirs.
# The session used --ours and that wiped the surrounding dual-provider edits.
# See "Cross-cutting lessons" #2 below.
git checkout --theirs server/main.py   # recommended; session used --ours by mistake
GIT_EDITOR=true git rebase --continue
# → [detached HEAD b792007] hermes: local patches ...
# → 5 files changed, 82 insertions(+), 34 deletions(-)
# → Successfully rebased and updated refs/heads/main.
```

## Step 5 — Verify dual-provider patch actually survived

```bash
grep -nE "EMBEDDER_API_KEY|siliconflow|EMBEDDER_BASE_URL" server/main.py
# → (empty!)
```

The rebase dropped the patch from `main.py`. **Fix**: re-apply via targeted `src.replace(...)` (Python heredoc). Five replacements, all anchored on unique pre/post strings present in upstream v2.0.11:

1. `OPENAI_BASE_URL` env var (after `OPENAI_API_KEY`)
2. `llm.config` gets `openai_base_url`
3. `embedder.config` uses `EMBEDDER_API_KEY` + `EMBEDDER_BASE_URL`
4. `MAX_REQUEST_BODY_SIZE` env var (after `DASHBOARD_URL`)
5. `limit_request_size` middleware (inserted before `app.include_router(auth_router.router)`)

Commit as a follow-up patch:
```bash
git add server/main.py
git commit -m "hermes: re-apply dual-provider patch after upstream rebase"
# → afe7073
```

Final HEAD = `afe7073` = upstream `cd79fa8` + rebase `b792007` + re-apply `afe7073`.

## Step 6 — Build + restart

```bash
cd /opt/1panel/docker/compose/mem0-official
docker compose build mem0-server   # ~13s
docker compose stop mem0-server
docker compose up -d mem0-server
# → Started, Application startup complete, uvicorn on 0.0.0.0:8000
```

## Step 7 — Functional verification (PARTIAL at the time — POST /memories 401, fixed in Step 9-B)

What worked:
- `wget -qO- http://<MEM0_HOST_IP>:8888/health` → **404** (v2.0.11 removed it)
- `POST /auth/login` → 200 + JWT
- `GET /configure` → 200, shows LLM on `https://api.uge.cc/v1`, embedder on `https://api.siliconflow.cn/v1`, both with redacted keys
- `POST /search` → 200, 3 results
- Container env vars verified: `OPENAI_API_KEY=sk-leql9Cl...`, `EMBEDDER_API_KEY=sk-ynbwdwna...`

What broke:
- `POST /memories` → **502 Bad Gateway**, server log: `LLM extraction failed: 401 Invalid token`
- Direct container debug (`Memory.from_config(env_dict).add(...)`) → **200 OK**

This **mismatch is the diagnostic signature** of the `config_overrides` issue (next section). **Resolution**: see Step 9-B for the actual fix that restored POST /memories to 200 OK.

## Step 8 — Diagnosis of the 401 (config_overrides override)

Reading `server/main.py:117` showed `OPENAI_BASE_URL` was being read correctly. So the env var path was fine. The question was why HTTP `/memories` 401'd while direct in-process `Memory.add()` 200'd.

Reading `server/server_state.py:114`:
```python
def initialize_state(default_config):
    overrides = _load_overrides()                       # ← reads from PostgreSQL
    merged = deepcopy(default_config)
    if overrides:
        merged = _merge_config(merged, overrides)
    _memory_instance = Memory.from_config(merged)
```

So the **actual** Memory instance used by `get_memory_instance()` is built from `merged`, not `default_config`. Any prior `POST /configure` that set a value (including API keys) would be persisted in PostgreSQL and re-merged on startup.

Querying the database:
```bash
docker exec mem0-postgres psql -U postgres -d mem0_app -c "SELECT key, value FROM settings;"
```
(Confirmed: there's a SECOND PostgreSQL database — `mem0_app` for auth/users/settings, separate from `postgres` for the `memories` table. Both live in the same `mem0-postgres` container.)

```json
{
  "config_overrides": "{\"llm\": {\"config\": {\"api_key\": \"sk-<LEGACY_LLM_API_KEY_REDACTED>\", ...}}, ...}"
}
```

The stale `sk-n7ineS6V...` was winning on every restart because `initialize_state()` loads this BEFORE `get_memory_instance()` returns the actual instance.

Direct verification:
```bash
# Test the two keys directly against api.uge.cc
curl -X POST https://api.uge.cc/v1/chat/completions -H "Authorization: Bearer sk-leql9Cl..."  → 200
curl -X POST https://api.uge.cc/v1/chat/completions -H "Authorization: Bearer sk-n7ineS6V..." → 401 Invalid token
```

Confirmed: the legacy key is genuinely dead. The override row is poison.

## Step 9 — B-option applied: jsonb_set replaces stale api_key (2026-07-05, executed)

After user chose **Option B (preserve DB override, only replace the stale key)** — rationale: respects upstream's design that settings table is the runtime truth source, no need to wipe history.

### 9.1 Safety net before any change

```bash
# Dump settings table as rollback safety
docker exec mem0-postgres pg_dump -U postgres -d mem0_app -t settings --data-only > /tmp/settings.backup.sql
# Also save current config_overrides JSONB value
docker exec mem0-postgres psql -U postgres -d mem0_app -At -c \
  "SELECT value FROM settings WHERE key='config_overrides'" \
  > /tmp/config_overrides.before-fix.sql  # 681 bytes
```

### 9.2 Dry-run the SQL (read-only verification)

```sql
SELECT
  value::jsonb->'llm'->'config'->>'api_key' AS old_llm_key,
  jsonb_set(value::jsonb, '{llm,config,api_key}',
            '"sk-<CURRENT_LLM_API_KEY_REDACTED>"', false)::jsonb
            ->'llm'->'config'->>'api_key' AS new_llm_key,
  jsonb_set(value::jsonb, '{llm,config,api_key}',
            '"sk-<CURRENT_LLM_API_KEY_REDACTED>"', false)::jsonb
            ->'embedder'->'config'->>'api_key' AS embedder_key_unchanged
FROM settings WHERE key='config_overrides';
```

Dry-run result: old=`sk-n7ineS6V...` → new=`sk-leql9Cl...`, embedder key unchanged. **Safe to proceed.**

### 9.3 Actual UPDATE

```sql
UPDATE settings
SET value = jsonb_set(value::jsonb, '{llm,config,api_key}',
                      '"sk-<CURRENT_LLM_API_KEY_REDACTED>"', false)::text,
    updated_at = NOW()
WHERE key='config_overrides'
RETURNING key, value::jsonb->'llm'->'config'->>'api_key' AS new_llm_key, updated_at;
-- → UPDATE 1; new_llm_key = sk-<CURRENT_LLM_API_KEY_REDACTED>
--   updated_at = 2026-07-05 10:06:07.480713+00
```

### 9.4 Restart and verify

```bash
docker compose restart mem0-server   # Ready in ~6 seconds
```

Post-restart end-to-end verification (47-test run, see Step 11):

| Test | Result |
|---|---|
| POST /auth/login | ✅ 200, JWT 207 chars |
| POST /memories (LLM extraction) | ✅ 10.9s, 1 result |
| POST /search (embedder vectorization) | ✅ 0.8s, 3 results, freshly-written fact found at score 0.697 |
| GET /memories/{id} | ✅ 200, has all fields |
| DELETE /memories/{id} | ✅ 200, Postgres row truly deleted (verified via direct SQL) |
| 413 middleware (11MB body) | ✅ HTTP 413 "Maximum: 10485760 bytes" |

**Both halves of the dual-provider config confirmed independently working**: LLM call to `api.uge.cc/v1/chat/completions` returned 200 (no more 401), embedder call to `api.siliconflow.cn/v1/embeddings` returned 200.

## Step 10 — Health monitor fix (2026-07-05, executed)

`mem0_health_monitor.py` was still calling `GET /health` which v2.0.11 removed. Replaced with `GET /openapi.json` probe — 200 + JSON with `openapi` key = healthy, no auth required.

**Change** in `check_health()` (line 126-133 of `/home/agent/.hermes/scripts/mem0_health_monitor.py`):

```python
def check_health() -> bool:
    """Check mem0 server is alive.

    v2.0.11 removed the /health endpoint (404). Fall back to /openapi.json
    (200 OK, no auth required, fastapi reflection of all routes -- confirms
    the server process is up and the FastAPI app initialised without error).
    """
    try:
        resp = api_get("/openapi.json", timeout=10)
        return isinstance(resp, dict) and "openapi" in resp
    except Exception as e:
        log(f"Health check failed: {e}", alert=True)
        return False
```

End-to-end monitor cycle verified at 2026-07-05 18:10: login → health check → search → **backfill 3 turns of this session** ("你好" / "看一下docker..." / "先备份吧") → timestamp update → RC=0, 47.98s. SKILL.md bumped v3.1.0 → v3.2.0 to record this fix.

## Step 11 — v2.0.11 behavior testing (47 tests across 20 OpenAPI paths)

Full-path testing surfaced **6 behavior changes** vs v2.0.5 documentation. All recorded as pitfalls in SKILL.md (v3.3.0/v3.4.0):

1. **`POST /search` error message is now actionable**: returns `"filters must contain at least one of: user_id, agent_id, run_id. Example: filters={'user_id': 'u1'}"` instead of the misleading `"Provider rejected the request as malformed"`. The "two distinct causes" pitfall is now obsolete — Cause 1 (missing user_id) is no longer a 30-minute false diagnosis.

2. **`top_k` hard cap is NOT 50**: v2.0.5 docs said max 50, but `top_k=100` returns 100 results. Either the cap was raised in v2.0.11 or it was never enforced on this path. Verified once; if it changes, update this note.

3. **`DELETE /memories/{id}` is NO LONGER idempotent** ⚠️ BREAKING: 1st call = 200, 2nd call = 404 (was 200 in v2.0.5 per v2.3.0 changelog). All cleanup scripts must handle 404 as "already deleted, OK to continue".

4. **Fact-extractor DUPLICATES on health-monitor `check_write` ping**: A single `POST /memories` with content `"mem0 health check: ping"` produces 1-6 rewrites of the same fact, each with a different UUID. The cleanup loop at the end of `check_write()` deletes by returned IDs, but **read-after-write inconsistency** (see #5) can cause some variants to leak past cleanup. Detection SQL + cleanup recipe in SKILL.md.

5. **pgvector `ConnectionPool` causes DELETE → GET read-after-write lag**: After DELETE returns 200, calling `GET /memories/{id}` within 0-2 seconds may STILL return the deleted memory (HTTP 200 with full payload). Postgres main table is correctly empty (verified via direct SQL `SELECT COUNT(*) FROM memories WHERE id=...` → 0). It's only the FastAPI HTTP path that has the inconsistency. Workaround: `time.sleep(2)` between DELETE and GET in scripts, OR use direct PostgreSQL DELETE for bulk cleanup.

6. **PUT `/memories/{id}` still does NOT refresh embeddings** (unchanged from v2.0.5): PUT updates `data` field, GET reflects new text, but `POST /search` with the new keyword misses it. Workaround unchanged: DELETE + POST.

### Database state after the test run

```sql
SELECT COUNT(*) AS total,
       COUNT(DISTINCT payload->>'user_id') AS users,
       COUNT(DISTINCT payload->>'agent_id') AS agents,
       MAX(payload->>'created_at') AS newest,
       MIN(payload->>'created_at') AS oldest
FROM memories;
-- → 9085 memories, 5 users, 4 agents
--   newest 2026-07-05T11:57:28Z
--   oldest 2026-06-11T02:28:04Z
--   100% vector coverage (9091 with_vector, 0 without — pre-cleanup snapshot)
```

6 leftover test markers from this session's testing were cleaned up via direct PostgreSQL DELETE (not REST — see pitfall #5 about read-after-write inconsistency).


## Cross-cutting lessons (carry to future mem0 ops)

1. **Verify BOTH the env var AND the database override row** when 401 appears. Either alone can lie. The signature: HTTP path 401 + direct in-process `Memory.add()` 200 → suspect `config_overrides` override.
2. **`git checkout --ours` in a rebase keeps LOCAL**, not upstream. For "upstream rewrote an API, local didn't touch it" conflicts, use `--theirs`. The rebase conflict markers say "HEAD" for the upstream side and the commit-hash for the local side; mentally map "HEAD = upstream" then `--theirs` = upstream, `--ours` = local.
3. **The 181-commit sync diff is mostly bug fixes + docs + new vector stores**. None are security CVE-grade. Future upgrades are low-risk IF dual-provider patch survives.
4. **Oracle minimal host shell quoting**: every `ssh ... 'bash -c "..."'` with inner heredocs/quotes reliably breaks. The base64 pipe-decode pattern is the only safe multi-line transport (`echo '<b64>' | ssh root@... 'base64 -d > /tmp/x.sh && bash /tmp/x.sh'`).
5. **`mem0_search` MCP plugin v1.0.1 does not depend on server version** — plugin kept working throughout the entire upgrade and 401 debugging. The plugin-level circuit breaker (5 failures → 120s cooldown) is independent of any of this.
6. **Stale credential tables in skills are worse than no entry**. The `sk-n7ineS6V...` key was logged as "server-side" across multiple skill entries, `~/.hermes/.env`, AND mem0 semantic memory. All three needed updating when the key rotated. Use `curl -X POST ... -H "Authorization: Bearer <key>"` as the one-liner validation before declaring any key authoritative.

## Files referenced / changed

- `/opt/1panel/docker/compose/mem0-official/server-src/server/main.py` (rebase + re-apply)
- `/opt/1panel/docker/compose/mem0-official/server-src/server/server_state.py` (already patched pre-upgrade)
- `/opt/1panel/docker/compose/mem0-official/backups/20260705_173820/` (full backup)
- `/home/agent/.hermes/cache/work/mem0-backups/20260705_173820/mem0-backup.tar.gz` (cross-host mirror)

## Resolved follow-ups (all from previous "Open" list, now done)

1. ✅ **Apply B-option SQL fix** — `jsonb_set` replaced stale `sk-n7ineS6V...` with `sk-leql9Cl...` in `mem0_app.settings.config_overrides`. POST /memories now returns 200. See Step 9.
2. ✅ **Update `mem0_health_monitor.py`** — `check_health()` now probes `/openapi.json` instead of removed `/health`. Verified end-to-end at 2026-07-05 18:10 (47.98s, 3 turns backfilled). See Step 10.
3. ✅ **Mark `sk-n7ineS6V...` as DEPRECATED in SKILL.md** — Key Constants section updated, the legacy key is now crossed-out (`~~strikethrough~~`) with explicit "Do NOT use" warning. mem0 fact-extractor also updated semantic memory entries accordingly.
4. ⏸ **Re-run model benchmark on v2.0.11** — not strictly necessary: spot-check of `mem0/llms/openai.py` between v2.0.5 and v2.0.11 showed identical `chat.completions.create()` call pattern (no behavioral change). Existing benchmark (qwen-next-80b at 1s, nemotron-mini-4b at 1s, step-3.7-flash at 1.4s, qwen-122b at 16s) still applies.
5. ⏸ **Make dual-provider patch rebase-resilient** — not done. The rebase did auto-drop the patch but manual re-apply was tractable (~10 min). Consider if future upgrades happen frequently; for now, the post-rebase re-apply is documented in Step 4-5 of this transcript.

## New follow-ups (post-test, 2026-07-05)

6. **Consider patching `check_write` to use `infer=false`**: would reduce fact-extractor variant leakage. Currently `check_write` writes 1 ping fact, LLM may produce 1-6 variants, DELETE cleanup misses some due to read-after-write lag → 6 leftover entries per test session.
7. **Re-evaluate `top_k` documentation in skill**: the "max 50" claim was wrong as of v2.0.11. Update or remove the constraint.
8. **Migrate health-monitor dependency to direct SQL cleanup**: the REST API has DELETE/GET consistency issues; for >10 entry cleanup use direct PostgreSQL (single transaction, no pool issue).
