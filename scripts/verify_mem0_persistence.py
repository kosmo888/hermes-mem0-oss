#!/usr/bin/env python3
"""
verify_mem0_persistence.py — ground-truth verification of mem0 writes.

The mem0 REST API has a 20-record cap on GET /memories and a broken
?search= parameter that returns arbitrary top-N by recency, not by
semantic similarity. As a result, "POST returned 200" is NOT proof
that a write persisted — and "GET didn't show the new entry" is NOT
proof that it didn't.

This script queries Postgres directly via the Oracle SSH host to get
the authoritative count and recent writes. Use it any time you need
to verify a batch of POST /memories calls actually committed.

Usage:
    python3 verify_mem0_persistence.py [--user USER_ID] [--limit N] [--since ISO_DATE]

Default: user_id=6228220870, limit=20, no time filter.
Output: total count, recent N writes with timestamps and previews.

Requires:
    - SSH key at /home/agent/.hermes/home/.ssh/id_ed25519 with access to root@<MEM0_HOST_IP> (in Docker, `~` resolves to /home/agent/.hermes/home)
    - mem0-postgres container reachable on the Oracle host

Exit codes:
    0 = healthy (writes visible)
    1 = error (SSH fail, query fail, parse fail)
"""
import argparse
import subprocess
import sys
from datetime import datetime, timezone

DEFAULT_USER = "6228220870"
SSH_KEY = "/home/agent/.hermes/home/.ssh/id_ed25519"
SSH_HOST = "root@<MEM0_HOST_IP>"
PG_USER = "postgres"
PG_DB = "postgres"


def run_psql(query: str) -> str:
    """Run a psql query via SSH using the file-based pattern.

    The single-line `ssh ... docker exec ... psql -c "..."` form is brittle
    when the query has multiple words — outer ssh shell consumes tokens and
    quoting gets nested. The robust pattern: write SQL to /tmp on Oracle via
    ssh, then docker cp into the postgres container, then psql -f.
    """
    import uuid
    tmp_id = uuid.uuid4().hex[:8]
    oracle_path = f"/tmp/verify_{tmp_id}.sql"
    container_path = f"/tmp/verify_{tmp_id}.sql"

    # 1. Write SQL to Oracle host
    # Use single-quoted heredoc to avoid nested-quote hell
    write_cmd = f"""bash -c 'cat > {oracle_path} << "SQL_EOF"
{query}
SQL_EOF'"""
    r = subprocess.run(
        ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10",
         "-i", SSH_KEY, SSH_HOST, write_cmd],
        capture_output=True, text=True, timeout=15
    )
    if r.returncode != 0:
        raise RuntimeError(f"ssh write_sql failed: {r.stderr.strip()}")

    # 2. docker cp into postgres container
    r = subprocess.run(
        ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10",
         "-i", SSH_KEY, SSH_HOST,
         "docker", "cp", oracle_path, f"mem0-postgres:{container_path}"],
        capture_output=True, text=True, timeout=30
    )
    if r.returncode != 0:
        raise RuntimeError(f"docker cp failed: {r.stderr.strip()}")

    # 3. psql -f inside container
    r = subprocess.run(
        ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10",
         "-i", SSH_KEY, SSH_HOST,
         "docker", "exec", "mem0-postgres",
         "psql", "-U", PG_USER, "-d", PG_DB, "-f", container_path],
        capture_output=True, text=True, timeout=30
    )
    if r.returncode != 0:
        raise RuntimeError(f"psql failed: {r.stderr.strip()}")
    return r.stdout


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--user", default=DEFAULT_USER, help="mem0 user_id (default: 6228220870)")
    p.add_argument("--limit", type=int, default=20, help="how many recent writes to show (default: 20)")
    p.add_argument("--since", default=None, help="ISO date filter (e.g. 2026-06-26)")
    args = p.parse_args()

    # 1. Total count for the user
    count_query = f"SELECT COUNT(*) FROM memories WHERE payload->>'user_id'='{args.user}';"
    out = run_psql(count_query)
    # psql output: count \n------- \n     4012 \n(1 row)
    total = int([ln.strip() for ln in out.splitlines() if ln.strip().isdigit()][0])
    print(f"[total] user {args.user} has {total} memories in Postgres")

    # 2. Recent N writes
    where = f"payload->>'user_id'='{args.user}'"
    if args.since:
        where += f" AND (payload->>'created_at')::date >= '{args.since}'"
    recent_query = (
        f"SELECT payload->>'created_at' as ts, "
        f"LEFT(payload->>'data', 120) as preview "
        f"FROM memories WHERE {where} "
        f"ORDER BY payload->>'created_at' DESC LIMIT {args.limit};"
    )
    out = run_psql(recent_query)
    print(f"\n[recent {args.limit} writes for {args.user}{f' (since {args.since})' if args.since else ''}]")
    print("-" * 80)
    for line in out.splitlines():
        # psql separates columns with |, so just print everything that's not a separator line
        if line.startswith("---") or line.startswith("(") or not line.strip():
            continue
        if "ts" in line and "preview" in line:
            continue  # header
        print(line)

    # 3. Sanity check: warn if total is way lower than expected
    if total == 0:
        print(f"\n⚠️  WARNING: total count is 0. mem0 may be empty or you may be querying the wrong user.")
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"FAIL: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
