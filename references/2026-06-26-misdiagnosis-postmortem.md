# mem0-oss 2026-06-26 Session: Complete Misdiagnosis Post-Mortem

## What happened (timeline)

1. User asked to "整理一下内置记忆" (curate built-in memory). Agent ran `memory` probe, got back `usage: 98%` and `entry_count: 40`. Agent thought the probe returned the full list — it did not. The probe only returns the count and usage percent. The agent worked from a stale snapshot it had seen earlier in the session, which was the only place the full list was visible (in the system prompt's MEMORY block).

2. Agent did a curation pass, removing ~9 long entries, leaving 6 in MEMORY and 6 in USER PROFILE. User asked to push the rule down further: "内置记忆只保留以下 4 条并设为不可删除." (Only keep these 4 in built-in memory, marked undeletable.)

3. User asked to batch-write 13 entries (credentials, env vars, server topology) to mem0. Agent tried `POST /memories` in a Python loop with 13 entries, batched together with one global 300s timeout. The 13 POSTs all hung because the LLM extract worker was queue-jammed from prior writes. Agent concluded: "mem0 is broken, batch write failed." Concluded the writes were lost.

4. Agent's diagnosis: "all 13 writes failed, and possibly 20 historical memories were also lost because the agent ran `DELETE /memories/{id}` on what it thought were 20 search-result IDs." Concluded data loss was catastrophic.

5. User told the agent to reload the mem0-oss skill (twice). Agent eventually did, and found:
   - The skill explicitly documents: "POST returns 200 + results but data does NOT persist" — and the fix is to verify via Postgres, not via GET /memories.
   - The skill explicitly documents: "?search= returns arbitrary top-N by recency, not semantic matches" — so the IDs the agent deleted were real records, but they were not "the records matching the query", they were "the most recent 20 records".
   - The skill explicitly documents: "use POST /search" for semantic recall.

6. Agent SSH'd to Oracle host, queried Postgres directly:
   - `SELECT COUNT(*) FROM memories WHERE payload->>'user_id'='6228220870'` → 4012 (not 20).
   - `SELECT ... ORDER BY created_at DESC LIMIT 20` → the 13 batch writes are all there, with timestamps from 2026-06-26T20:39-20:44Z. The 20 historical entries the agent "deleted" are also still there.

7. Conclusion: ZERO data loss. 13 batch writes all succeeded. The 20 "deleted" records were not actually deleted (DELETE on a search-result ID in this version of mem0 either no-ops or the IDs were already missing). The agent's diagnosis was wrong, but no harm was done.

## What the agent got wrong (in order of severity)

1. **Did not load the mem0-oss skill before doing mem0 operations.** The skill was sitting in `~/.hermes/skills/devops/mem0-oss/`, version 1.8.0, with 90% of the answers. The agent had to be told twice ("不是有相关的技能么") to load it.
2. **Treated `memory.add('__probe__')` as if it returned the full list.** It only returns `usage` and `entry_count`. The actual entries are only visible in the system prompt's MEMORY block. This is a documented pitfall in `hermes-memory-curation` v1.1.1+ — the agent did not re-read that skill either.
3. **Treated `GET /memories?user_id=X&page=1&size=50` as the authoritative count.** The endpoint has a 20-record cap. The agent should have SSH'd to Postgres immediately.
4. **Treated `?search=` results as semantic matches.** The endpoint returns `created_at DESC` ordering, period. Search via `POST /search` only.
5. **Concluded data loss without verification.** The agent ran `DELETE /memories/{id}` on 20 IDs it got from `?search=`, then ran `GET /memories?user_id=X` and saw the count was still 20. It concluded "I deleted 20 records but they still show up." The actual explanation: GET has a 20-cap, so deleting 20 records doesn't change the visible count (the next 20 take their place). AND/OR the DELETE didn't actually fire on those IDs.
6. **Told the user "事故" (incident) was real** when it was entirely a misdiagnosis. This is the most damaging failure — false alarms erode user trust faster than actual bugs do.

## The fix path the agent eventually took

1. Loaded `mem0-oss` v1.8.0 (skipped this twice when the user asked).
2. Found "POST returns 200 but data does NOT persist" section.
3. Found "Search interface returns arbitrary top-N" section.
4. Found "Direct PostgreSQL access" recipe.
5. SSH'd to Oracle host with `/home/agent/.hermes/home/.ssh/id_ed25519` (the agent had this key in its mem0 entry; in Docker, `~` resolves to `/home/agent/.hermes/home`).
6. Ran psql via `docker exec mem0-postgres psql ...` directly.
7. Confirmed: 4012 records, all 13 writes present, no records actually deleted.
8. Patched mem0-oss to v1.9.0 (the "Critical Lesson" section the user is now reading).
9. Patched hermes-memory-curation to v1.1.1.
10. Created this reference document.

## Lessons for the next agent

1. **Load the relevant skill before the first tool call.** Always. Even if you "know" how to do it. The skill might have been updated since you last read it.
2. **Treat `memory` tool responses as metadata, not content.** The full list of entries is in the system prompt's MEMORY block. Re-read that block if you need to triage.
3. **Treat mem0 REST API responses as partial.** GET /memories has a 20-cap. ?search= doesn't actually search. POST /memories might commit or might not. Only the Postgres direct query is ground truth.
4. **When the user says "为什么不用 skill", don't argue, don't explain — load the skill.** That IS the trigger. The user has already decided the agent missed a known procedure.
5. **Don't issue false alarms.** If you're not 100% sure data is lost, say "let me verify before claiming loss." The user can wait 30 seconds for verification; they cannot un-hear a false data-loss report.
6. **Skill updates are cheap. Argue-back is expensive.** The user said "你没用？" — that's a 2-second acknowledgement ("you're right, let me load the skill") that should have been the FIRST response, not the third.

## What the next agent should do at task start

```bash
# 1. Run skills_list and look for anything matching the task
skills_list

# 2. For any obvious match, skill_view it
skill_view mem0-oss

# 3. Run mem0_search for related facts
mem0_search(query="<task keywords>")

# 4. THEN start the actual work
```

This is the 30-second pre-flight. The cost is 30 seconds. The benefit is "you don't re-discover the wheel" and "the user doesn't have to tell you twice."
