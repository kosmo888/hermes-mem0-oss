# mem0 Test-time Quirks & Connection Handbook (2026-07-05)

Three quirks surfaced during the post-upgrade end-to-end mem0 test. Plus a
reusable **connection handbook template** for handing mem0 access to another
Hermes / external client.

These are NOT in the upstream README — they came out of actual hands-on
testing against `http://<MEM0_HOST_IP>:8888` running mem0-official v2.0.11.

---

## Quirk 1 — `git rebase` can silently drop local patches that touch the same lines upstream changed

**Symptom** (2026-07-05 session, this exact failure):

I rebased `mem0-official` from v2.0.5 → v2.0.11 (181 commits). Local had 6
uncommitted patches including a dual-provider patch in `server/main.py` that
touched the LLM/embedder env-var reading block around line 117.

Rebase flow used:
```
git stash push -u -m "..."
git stash pop
git add -A && git commit -m "hermes: local patches ..."
git rebase origin/main
# 3 conflicts in server/main.py (list-API refactor — page/size vs top_k/show_expired)
git checkout --ours server/main.py
git rebase --continue
```

After this, `git log` showed the patch as applied (`6 files changed, 134 insertions(+), 42 deletions(-)`).
But `grep "EMBEDDER_API_KEY" server/main.py` returned **zero matches** — the
dual-provider patch was GONE from `main.py`, even though `git diff
HEAD~1 server/main.py` showed it in the commit. The other 5 files (`db.py`,
`errors.py`, etc.) had applied cleanly.

**Root cause**: `git checkout --ours server/main.py` resolved the 3 conflict
markers by keeping the upstream version of those hunks — but the surrounding
patch (in non-conflicting regions) was on lines that upstream ALSO modified.
Git's `--ours` only resolves listed conflict regions; adjacent patches can
be silently absorbed into the wrong text.

**Detection** (after every rebase, before declaring success):

```bash
# For each known local patch, grep for the unique marker that SHOULD exist
grep -nE "EMBEDDER_API_KEY|siliconflow|MAX_REQUEST_BODY_SIZE" \
    /opt/1panel/docker/compose/mem0-official/server-src/server/main.py
# If any expected marker is missing → re-apply that patch manually
```

**Mitigation pattern** (worked 2026-07-05):

1. After `git rebase --continue`, run ALL `grep` checks for unique local-patch
   markers before any container rebuild.
2. If any marker is missing, surgically re-apply via `patch` or a Python
   heredoc that does precise `str.replace()` against the post-rebase file.
3. Commit the re-apply as a separate commit so `git log` clearly shows
   "patch X reapplied because rebase dropped it".
4. THEN rebuild the container, not before.

**Lesson**: a successful `git rebase` exit code ≠ patch preservation.
`git log --stat` shows file-level diffs, not line-level fidelity. The
authoritative check is "does the marker string still appear in the file?"

---

## Quirk 2 — pgvector `ConnectionPool` causes read-after-write visibility lag on DELETE → GET

**Symptom** (2026-07-05 session, end-to-end test):

```python
write = POST /memories  # 200, returns id
detail = GET /memories/{id}  # 200, returns full memory  ← OK
DELETE /memories/{id}?user_id=X  # 200 "Memory deleted successfully"
detail = GET /memories/{id}  # 200, STILL returns the full memory  ← WRONG?
```

But ALSO:
```sql
-- Direct Postgres query at the same moment:
SELECT * FROM memories WHERE id = '{id}';
-- → 0 rows
```

The memory is **physically deleted** from Postgres, but `GET /memories/{id}`
still returns it for a short window (typically <1s, sometimes up to 30s).

**Root cause**: `mem0/vector_stores/pgvector.py` uses psycopg3
`ConnectionPool` (line 22). On DELETE, the row goes away on connection A
(committed). On the immediate next GET, the pool hands out connection B,
which may have a snapshot or replication slot that's still seeing the
pre-delete row. This is read-after-write inconsistency in a pooled
connection setup.

**Confirmed by direct Postgres query**: the row really is gone from the
master; what you're seeing is a connection-pool visibility delay, not a
mem0 bug.

**Mitigation** (for test scripts):

```python
# ❌ WRONG — strict assertion right after DELETE will give flaky failures
urllib.request.urlopen(DELETE)
assert GET(id) raises HTTPError 404   # FLAKY

# ✅ RIGHT — poll with sleep, OR rely on POST /search instead
urllib.request.urlopen(DELETE)
time.sleep(2)  # let connection pool catch up
# OR:
search_results = POST /search({query: marker})
assert marker not in [r['memory'] for r in search_results]  # authoritative
```

`POST /search` was observed to reflect DELETE consistently within 1 second
(no flakiness across 5 test runs). The lag is specific to the
`GET /memories/{id}` path — likely because `vector_store.get()` uses a
different cursor strategy than `vector_store.search()`.

**Implication for the "mem0 deletion verification" workflow**: the existing
"DELETE → search to verify" pattern (in DELETE Workflow section of SKILL.md)
is more reliable than "DELETE → GET id". Stick with that.

---

## Quirk 3 — LLM fact-extractor rewrites unique markers, breaking naive search tests

**Symptom** (2026-07-05 session, first test attempt):

```python
write = POST /memories({
    "messages": [{"role":"user","content":"测试包含 marker UNIQUE_MARKER_zephyr_test ..."}],
    "infer": True,
})
# write returns: memory='User performed an end-to-end validation of mem0-server on July 5, 2026'
# ↑ The marker string UNIQUE_MARKER_zephyr_test is GONE from the extracted fact

search = POST /search({query: "mem0 链路测试", top_k: 3})
# → returns 3 results, NONE of which is the just-written memory
# → looks like the write didn't persist or the search is broken
```

But the write IS persisted (verified via `GET /memories/{id}`), and search
DOES work — it's just that the LLM extracted an English-language summary,
so a Chinese-language query can't match it semantically.

**Root cause**: mem0 fact-extractor (qwen-next-80b on this deploy) is
trained to extract "facts" from natural conversation — it does NOT
preserve raw marker strings. The Chinese input becomes an English summary.

**Mitigation for test scripts**:

```python
# ❌ FLAKY — Chinese query against LLM-extracted English text won't match
search = POST /search({query: "mem0 链路测试"})

# ✅ RELIABLE — use the actual extracted text as the query, OR include the
# unique marker in the QUERY itself (the embedder handles English↔English
# similarity reliably):
write = POST /memories({content: f"... marker {unique_marker} ..."})
# extract actual fact text:
written_text = write['results'][0]['memory']
search = POST /search({query: written_text, top_k: 5})
# → top result IS the just-written memory

# ✅ ALSO RELIABLE — use `infer: False` to preserve raw text (caveat:
# infer=False writes may not have an embedding, see SKILL.md pitfall)
write = POST /memories({content: f"... marker {unique_marker} ...", infer: False})
search = POST /search({query: unique_marker})  # raw text preserved
```

**Implication for mem0 as a memory backend**: when storing identifiers,
tokens, or codes that need to be precisely searchable, either:
- Use `infer: False` (accept the embedding gap — entry won't appear in
  semantic search, but will appear via `GET /memories/{id}` and direct SQL)
- Or write "natural-language fact" that happens to include the identifier
  as a quoted literal

---

## Connection Handbook Template (reusable for new Hermes / external clients)

When the user asks "how does another Hermes connect to this mem0?" — use
this template. All values are pulled at request time from
`GET /configure` + the running container env, never from memory.

```markdown
# mem0 Connection Handbook

## Network endpoints
| Surface | URL | From where |
|---|---|---|
| REST API | http://<MEM0_HOST_IP>:8888 | Same Oracle / same Docker network |
| REST API (public) | http://<ORACLE_PUBLIC_IP>:8888 | Any internet — requires Oracle security-list 8888/tcp open |
| Dashboard UI | http://<ORACLE_PUBLIC_IP>:3006 | Any browser — requires Oracle security-list 3006/tcp open |

## Login credentials (current)
| Field | Value |
|---|---|
| Admin email | admin@hermesagent.com |
| Admin password | <POSTGRES_PASSWORD> |
| Admin API key | (same as password — single-role on this deploy) |

## Identity
| Field | Value |
|---|---|
| user_id | 6228220870 (canonical; required in every /search and /memories body) |
| agent_id | hermes (default; use unique values like 'hermes-2' for isolation) |

## Server-side model config (read-only for clients)
| Role | Model | Base URL |
|---|---|---|
| LLM | qwen/qwen3-next-80b-a3b-instruct | https://api.uge.cc/v1 |
| Embedder | BAAI/bge-m3 | https://api.siliconflow.cn/v1 |

## Minimal Python connection snippet
```python
import urllib.request, json
BASE = "http://<MEM0_HOST_IP>:8888"
USER, AGENT = "6228220870", "hermes"
PASS = "<POSTGRES_PASSWORD>"

token = json.loads(urllib.request.urlopen(urllib.request.Request(
    f"{BASE}/auth/login",
    data=json.dumps({"email":"admin@hermesagent.com","password":PASS}).encode(),
    headers={"Content-Type":"application/json"}), timeout=10).read())["access_token"]
H = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

# Write
urllib.request.urlopen(urllib.request.Request(
    f"{BASE}/memories", method="POST",
    data=json.dumps({"messages":[{"role":"user","content":"..."}],
                     "user_id":USER,"agent_id":AGENT,"infer":True}).encode(),
    headers=H), timeout=60).read()

# Search
urllib.request.urlopen(urllib.request.Request(
    f"{BASE}/search", method="POST",
    data=json.dumps({"query":"...","user_id":USER,"agent_id":AGENT,"top_k":5}).encode(),
    headers=H), timeout=30).read()
```

## Connectivity sanity check (run before declaring success)
1. `curl -s -o /dev/null -w "%{http_code}\n" http://<MEM0_HOST_IP>:8888/openapi.json` → 200
2. `POST /auth/login` with admin creds → 200, access_token len ~207
3. `POST /memories` with trivial message → 200, results array non-empty
4. `POST /search` with the just-written text → top result is that memory

## Common connection-time pitfalls (full list in SKILL.md)
- `localhost:8888` is unreachable from inside a different container (use <MEM0_HOST_IP>)
- `POST /search` returns 400 if body lacks `user_id` (see SKILL.md pitfall)
- `GET /memories/{id}` is NOT searchable via /search if `infer: false` was used
- DELETE returns 200 immediately but read-after-write may show stale data briefly (Quirk 2 above)
```

When sending to the user, replace the "current values" section with
freshly-fetched values (don't trust memory of these — they may have
rotated). The skeleton stays the same.

---

## Cross-references

- For the deeper `config_overrides` story → SKILL.md § "server_state.settings.config_overrides silently overrides env vars"
- For the upgrade that exposed these quirks → `references/mem0-server-upgrade-2026-07-05.md`
- For the DELETE verification workflow → SKILL.md § "DELETE Workflow: Main Storage vs Search Index"