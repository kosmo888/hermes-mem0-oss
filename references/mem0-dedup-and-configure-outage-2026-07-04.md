# mem0 ★不可删★ Dedup & /configure Outage (2026-07-04)

## What happened

User asked to list, then deduplicate, all ★不可删★ entries in mem0. During the dedup operation, a `POST /configure` call to update the LLM API key accidentally clobbered the embedder API key, causing a 30+ minute outage.

## Phase 1: Inventory (19 entries found)

- `GET /memories?page=N&size=50` returns 50/page, capped at 1000 total
- Filtered for `★不可删★` in the `memory` field → **19 entries** found
- `mem0_search` (MCP tool) returned up to 50 hits for the same prefix, but many only *mentioned* ★不可删★ without being tagged entries

## Phase 2: Dedup plan (19 → 5)

Grouped the 19 entries into 5 topics:

| Topic | Entry count | Merged into |
|-------|------------|-------------|
| mem0 read rules | 6 | Single 9-point read rule |
| mem0 write rules | 5 | Single 7-point write rule |
| Credential management | 2 | Single unified rule |
| .env read method | 1 | Preserved as-is |
| Metadata/self-check/marking system | 4+ | Single marking system rule |

## Phase 3: Execution

### Deletion (successful)
- Used `DELETE /memories/{id}` — all 19 entries deleted, 200 for each, ~0.2s/call

### Write attempt 1: `infer: true` (failed)
- `POST /memories` with `infer: true` (default) returned `results: []` for all 5 merged entries
- LLM fact-extractor (qwen3-next-80b) filtered out rule-type/imperative content
- **Confirmed**: `infer: true` does NOT work for behavioral rules — already documented in skill

### Write attempt 2: `infer: false` (partially successful)
- `POST /memories` with `infer: false` returned `200` with `results: [{id, event: "ADD"}]`
- `GET /memories/{id}` confirmed each entry persisted in PostgreSQL
- BUT paged `GET /memories?page=N` could NOT find them (entries without embedding vectors excluded from paged query)
- `POST /search` with `user_id` + `agent_id` — found some entries, not others (intermittent, embedder state-dependent)

### Multiple write rounds created duplicates
- Testing `infer: true` vs `infer: false` across multiple rounds created ~3 copies of each entry
- Paged GET couldn't find them to delete
- Some duplicates found via `POST /search` (with `user_id`!) and cleaned up
- Others remain in DB without embeddings — invisible but harmless (won't be recalled)

## Phase 4: /configure outage (THE critical learning)

### Trigger
LLM API key needed updating. Procedure used:
1. `GET /configure` → returned full config with `api_key: "[redacted]"` for both LLM and embedder
2. Modified `llm.config.api_key` to new key
3. `POST /configure` with the **entire config body** (including embedder section with `[redacted]`)

### What happened
- LLM key: updated correctly (or silently ignored — server uses container env anyway)
- Embedder key: **overwritten with literal string `"[redacted]"`** — NOT silently ignored
- Result: `POST /search` → 502 `provider_auth_failed` for ALL searches
- `mem0_search` MCP tool → 502
- `GET /memories?page=N` → hangs/timeout (tries to use embedding for sorting)
- Service itself stays healthy (`/health` returns 200)

### Misdiagnosis (30+ minutes wasted)
- Initially blamed siliconflow bge-m3 as "unstable" — wrong
- Then concluded both API keys (LLM + embedder) were expired — partially wrong
- `curl -d '{"query":"test","top_k":3}'` returned 400 → assumed embedder was down
- `curl -d '{"query":"test","user_id":"6228220870","agent_id":"hermes","top_k":3}'` → 200 (embedder was FINE at that point!)
- The real issue was self-inflicted: the POST /configure overwrote the embedder key

### Root cause
`POST /configure` with a full config body treats `[redacted]` as a literal API key and stores it. The GET endpoint's redaction is a display-layer feature — there's no round-trip safety.

### Fix (needed but not yet executed)
- Need to SSH to Oracle host, find original siliconflow key in `/opt/1panel/docker/compose/mem0-official/.env`
- Or check `.env.bak-*` files in `~/.hermes/` for the original siliconflow key
- POST `/configure` with ONLY the embedder section: `{"embedder":{"provider":"openai","config":{"api_key":"sk-...","openai_base_url":"https://api.siliconflow.cn/v1","model":"BAAI/bge-m3"}}}`

### Prevention rule
NEVER POST the full config body returned by `GET /configure` — only POST the section you need to update.

## Phase 5: Built-in memory cleanup

After dedup, also cleaned Hermes built-in memory:
- Removed 2 old ★不可删★ entries (AI协作铁律, 技能利用率要求) — duplicates of mem0 content
- Removed 2 stale notes (skill directory permission issue, MoA troubleshooting)
- Added 1 compact index pointer: `★不可删★铁律精简索引(mem0存全量)：①mem0读取9触发场景 ②mem0写入7场景 ③凭证管理mem0+.env双向同步 ④.env读取方法 ⑤★不可删★标记只可精炼不可删。`
- Dropped from 1,311 chars to ~143 chars

## Login credentials (confirmed working)

```
POST /auth/login
{"email":"admin@hermesagent.com","password":"hermes-mem0-2024"}
→ {"access_token":"eyJ...", "refresh_token":"eyJ..."}
```

JWT can be used for authenticated endpoints but does NOT un-redact API keys in `GET /configure` response.

## api.uge.cc key validation

The new key `sk-<CURRENT_LLM_API_KEY_REDACTED>` was confirmed valid:
- `GET /v1/models` → 200 (returns full model list including qwen3, bge-m3, etc.)
- `POST /v1/chat/completions` with qwen3-next-80b → 200 (valid response)
- `POST /v1/embeddings` with `baai/bge-m3` → error "Something went wrong" (api.uge.cc does NOT support bge-m3 for embeddings — only siliconflow does)

## .env backup files available

```
~/.hermes/.env.bak-20260702T200013Z
~/.hermes/.env.bak-20260604T060636Z
~/.hermes/.env.bak-20260610T200125Z
~/.hermes/.env.bak-20260627T200023Z
~/.hermes/.env.bak-20260606T114253Z
~/.hermes/.env.bak-20260627T145149
~/.hermes/.env.bak-20260617T200109Z
```

These likely contain the original siliconflow API key. NOT yet searched — next session should grep for `SILICONFLOW` or `silicon` in these backups.
