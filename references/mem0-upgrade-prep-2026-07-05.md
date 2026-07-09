# mem0 Server Upgrade Prep — Session Transcript (2026-07-05)

Verbatim record of the first time we applied the upgrade-prep SOP. Future sessions
can compare new runs against this baseline to spot drift.

## Context

User asked: "看一下 docker 容器 mem0 与 github 项目是否一致，以本地为准" → "先备份吧"

## What we found

| Item | Local | Upstream `origin/main` |
|------|-------|------------------------|
| HEAD SHA | `8399b088a563505ca68d59c2c541386067f6884a` | `cd79fa8914b5b1cf66daacc957d826065df57df8` |
| pyproject version | `2.0.5` | `2.0.11` |
| Commits behind | — | 181 (`0\t181` from `git rev-list --left-right --count HEAD...origin/main`) |
| Working tree | 6 files modified (uncommitted) | clean |

## Files modified locally (uncommitted, 134+/42-)

```
server/dashboard/Dockerfile |  7 +-----
server/db.py                | 14 ++++++++++-
server/dev.Dockerfile       | 48 ++++++++++++++++++++++++------------
server/errors.py            | 16 ++++++++++--
server/main.py              | 60 +++++++++++++++++++++++++++++++++++++++------  ← EMBEDDER_API_KEY split
server/server_state.py      | 31 +++++++++++++++--------
```

The `server/main.py` change is the dual-provider persistence patch (LLM=api.uge.cc, embedder=siliconflow, separate env var). Everything else is downstream of that or build-time tweaks.

## Backups created

### Oracle-side (primary)

`/opt/1panel/docker/compose/mem0-official/backups/20260705_173820/`

```
mem0-local-patches.patch     13704 bytes   sha256: f31fb7022c45b424b8b20cf43cc86df5f791b2c743b91516535f5b2a2b4ec97d
orig/server/<6 files>                    sha256 in orig.sha256
modified/server/<6 files>                sha256 in modified.sha256
SNAPSHOT.txt                             environment snapshot (git status, HEAD, version, behind count)
```

### Local container-side (insurance)

`/home/agent/.hermes/cache/work/mem0-backups/20260705_173820/mem0-backup.tar.gz`
SHA256: `5aa08bfe8532ea4fc8029c38e0d5c909a7bd672d49162e4857f1b8b2716be58d`
(byte-for-byte identical to Oracle-side tar of same directory)

### Mem0 semantic memory

Entry written with `infer=False`, id prefix `eded8138`. Contains the
upgrade-prep metadata (paths, SHAs, lag count, 6 patched files list,
both backup locations).

## Dry-run verification (Step 2)

```
git clone https://github.com/mem0ai/mem0.git /tmp/patch-verify-$$
cd /tmp/patch-verify-$$
git checkout 8399b088a563505ca68d59c2c541386067f6884a
git apply --check <BACKUP_DIR>/mem0-local-patches.patch
# DRY_RUN_EXIT=0
git apply <BACKUP_DIR>/mem0-local-patches.patch
# APPLY_EXIT=0
git diff --stat
# 6 files changed, 134 insertions(+), 42 deletions(-)
```

Patch is portable. Safe to `git pull` next time without re-running dry-run
(unless upstream drastically changes the touched files — unlikely for these 6).

## Cross-host backup transfer (the base64 trick)

The Oracle minimal host has fragile shell quoting. The base64 pipe-decode
pattern worked first try:

```python
# Local: build tar.gz, base64 it, send via SSH, decode and save
import base64, hashlib
from hermes_tools import terminal

REMOTE_DIR = "<BACKUP_DIR>"
LOCAL_DIR = "/home/agent/.hermes/cache/work/mem0-backups/<TS>"

# Step 1: tar+base64 on Oracle, get back encoded bytes
res = terminal(command=f"ssh ... root@<MEM0_HOST_IP> 'cd {REMOTE_DIR} && tar czf - --exclude=SNAPSHOT.txt . | base64'", timeout=60)
b64 = res["output"].replace("\n", "").strip()

# Step 2: get remote SHA256 for verification
res = terminal(command=f"ssh ... 'sha256sum /tmp/mem0-backup.tar.gz'", timeout=10)
remote_sha = res["output"].split()[0]

# Step 3: decode and verify
tar_bytes = base64.b64decode(b64)
local_sha = hashlib.sha256(tar_bytes).hexdigest()
assert local_sha == remote_sha, f"CROSS-HOST CORRUPTION: {local_sha} != {remote_sha}"

# Step 4: extract locally for inspection
import subprocess
with open(f"{LOCAL_DIR}/mem0-backup.tar.gz", "wb") as f:
    f.write(tar_bytes)
subprocess.run(["tar", "xzf", f"{LOCAL_DIR}/mem0-backup.tar.gz", "-C", LOCAL_DIR], check=True)
```

## What we did NOT do (yet)

- No `git pull` (user chose "先备份吧" — backup only, upgrade decision pending)
- No cherry-pick (no CVE urgency; 181 commits are docs + bug fixes + features)
- No `docker compose build mem0-server` (would invalidate the running 30h-stable container)

## Upgrade decision matrix (for next session)

| Signal | Action |
|--------|--------|
| User asks "升级" / "更新" with no urgency | Present this backup SOP again, do not auto-upgrade |
| User asks "升级" with "fix X bug" | Cherry-pick specific SHA, dry-run, then full patch cycle |
| CVE announcement | Full sync path: backup → pull → 3-way merge patch → test → restart |
| Backfill lag >24h | Investigate first; upgrade is unrelated |

## Upstream changes worth cherry-picking (as of 2026-07-05)

- `ad7e09851c` (2026-07-01) `fix(memory): re-raise LLM extraction failures instead of returning []` — addresses the `results: []` symptom
- `152d1e66f7` (2026-07-01) `fix(embeddings): guard embed_batch count mismatch in OpenAI and Azure OpenAI`
- `59484f066f` (2026-07-02) `fix(elasticsearch): validate filter keys and values to prevent term injection` — not relevant (pgvector)
- `8a5c0729e5` (2026-07-02) `fix(server): do not forward empty-string entity ids as filters`

None are security CVEs. All can wait.