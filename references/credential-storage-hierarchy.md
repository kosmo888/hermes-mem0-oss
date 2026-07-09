# Credential Storage Hierarchy for mem0 (2026-06-27)

## The Rule

Per ★不可删★ rules #3 and #6:

> **Credentials NEVER go in built-in memory.**

The 5000-character built-in memory cap is reserved for **behavioral rules only**. Credentials, tokens, and connection details must be stored in a 3-layer hierarchy:

| Layer | Where | Purpose | Lifetime |
|-------|-------|---------|----------|
| 1. Container env | `docker-compose.yml` `environment:` + `.env` file | Deployment persistence — survives restarts | Permanent (until rotated) |
| 2. mem0 | REST API `POST /memories` with explicit attribution | Semantic search by agent during sessions | Indefinite (until cleanup) |
| 3. .env backup | `~/.hermes/home/.env` (or `~/.env`) | Ops-level recovery, source-controlled backup | Permanent |

## mem0 credentials inventory

| Name | Value | Used for |
|------|-------|----------|
| Backend URL | `http://<MEM0_HOST_IP>:8888` | Connecting to mem0 REST API |
| Admin email | `admin@hermesagent.com` | Login |
| Admin password | `<POSTGRES_PASSWORD>` (same as POSTGRES_PASSWORD/ADMIN_API_KEY, per SECRETS-2026-07-04.txt — NOT `hermes-mem0-2024`) | Login |
| `ADMIN_API_KEY` | `<POSTGRES_PASSWORD>` | Service-to-service API auth (= POSTGRES_PASSWORD) |
| `OPENAI_API_KEY` (server-side) | `sk-<LEGACY_LLM_API_KEY_REDACTED>` | mem0-server → New API (LLM calls) |
| `OPENAI_BASE_URL` | `https://api.uge.cc/v1` | New API endpoint |
| Client New API token | `sk-<CURRENT_LLM_API_KEY_REDACTED>` | Hermes/agent → New API (used by user) |

**Two distinct API keys** — never confuse them. The server-side key is fixed and lives in the mem0 container env. The client-side key is what the agent uses to talk directly to New API.

## Why no credentials in built-in memory

1. **Capacity** — 5000 char cap is for behavioral rules. Adding 6 credentials + their context = 500-1000 chars, eating into rule space.

2. **Security** — built-in memory is auto-injected into every LLM prompt as system context. Any model that processes a request sees the keys. mem0's REST API requires explicit GET to retrieve facts, so the LLM doesn't see them by default.

3. **Search semantics** — built-in memory is a flat list, not searchable. mem0 supports semantic search: the agent can ask "what's the admin password?" and get the right one back.

4. **★不可删★ rule #6** explicitly says "内置记忆只承载 ★不可删★ 规则本身，其他一律 mem0".

## How to add a new credential

1. **Add to container env** (if it needs to be in the container):
   - Edit `/opt/1panel/docker/compose/mem0-official/.env`
   - Add `KEY=value` line
   - Add to `docker-compose.yml` `environment:` block if not already in `env_file: - .env`
   - Restart container: `ssh ... "docker restart mem0-server"`

2. **Write to mem0** with explicit attribution:
   ```python
   import urllib.request, json
   login = json.loads(urllib.request.urlopen(urllib.request.Request(
       "http://<MEM0_HOST_IP>:8888/auth/login",
       data=json.dumps({"email":"admin@hermesagent.com","password":"<POSTGRES_PASSWORD>"}).encode(),
       headers={"Content-Type":"application/json"}), timeout=10).read())
   tok = login["access_token"]
   
   fact = """Configuration note: mem0 server admin API key for service-to-service auth is 
   ADMIN_API_KEY=<POSTGRES_PASSWORD>. Source: mem0 server docker-compose env. 
   NOT a user preference; documented for ops reference."""
   
   req = urllib.request.Request(
       "http://<MEM0_HOST_IP>:8888/memories",
       data=json.dumps({"user_id":"6228220870","agent_id":"hermes",
                        "messages":[{"role":"user","content":fact}],
                        "infer":True}).encode(),
       headers={"Content-Type":"application/json","Authorization":f"Bearer {tok}"},
       method="POST")
   urllib.request.urlopen(req, timeout=30).read()
   ```

3. **Backup to .env**:
   ```bash
   cat >> ~/.hermes/home/.env << 'EOF'

   # ===== mem0 backend credentials (added YYYY-MM-DD) =====
   MEM0_BACKEND_URL=http://<MEM0_HOST_IP>:8888
   MEM0_ADMIN_EMAIL=admin@hermesagent.com
   MEM0_ADMIN_PASSWORD=<POSTGRES_PASSWORD>
   MEM0_OPENAI_API_KEY=sk-<LEGACY_LLM_API_KEY_REDACTED>
   MEM0_ADMIN_API_KEY=<POSTGRES_PASSWORD>
   MEM0_OPENAI_BASE_URL=https://api.uge.cc/v1
   EOF
   chmod 600 ~/.hermes/home/.env
   ```

## When credentials change

| Change type | Action |
|-------------|--------|
| Rotate admin password | 1) Update `.env`, 2) `docker restart mem0-server`, 3) Test login, 4) Update mem0 fact, 5) Update ~/.hermes/home/.env backup |
| Rotate API key | Same as above, but be aware mem0 may not pick up the new key without container restart since `/configure` strips it |
| Change New API token | 1) Get new token, 2) Update mem0 fact, 3) Update ~/.hermes/home/.env, 4) Verify by making a test call |
| Change mem0 URL | Update everywhere: docker-compose port mapping, mem0 fact, ~/.hermes/home/.env, all scripts that hardcode <MEM0_HOST_IP> |

## Anti-pattern: Don't write a single composite fact

If you write `"Credentials: admin=admin@hermesagent.com, pass=POb06Jd-..., key=POb06Jd-..."` as one fact, the LLM will:
1. Split it into multiple facts with attribution hallucination
2. Create "User established" / "User confirmed" entries for each fragment
3. Pollute search with attribution noise

**Better**: One fact per credential, with explicit "Configuration note" prefix and explicit "Source" attribution:

```
Configuration note: mem0 server admin API key is ADMIN_API_KEY=<POSTGRES_PASSWORD>. 
Source: /opt/1panel/docker/compose/mem0-official/.env. NOT a user preference.
```

The LLM will still say "User said" in extraction, but the original text is preserved for cross-reference.
