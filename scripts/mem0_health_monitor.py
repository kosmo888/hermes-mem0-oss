#!/usr/bin/env python3
"""
mem0 Health Monitor + Conversation Backfill

1. Check mem0 health (health endpoint, search test)
2. If unhealthy, attempt fixes (model switch between fallback models)
3. After recovery, extract conversations from state.db between
   last_healthy_time and now, write them to mem0 via POST /memories
4. Update last_healthy timestamp

Cron: every 30 minutes (job: 2d3a7370f5a4)

Auth: JWT via POST /auth/login (email+password).  The server has
AUTH_DISABLED=false, so all endpoints require a Bearer JWT.  Login
is cached for 7 days (matching the server's JWT lifetime).  Do NOT
rely on MEM0_OSS_API_KEY — that env var is unset on this deploy.
"""

import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

# ── Config ──────────────────────────────────────────────────────────
MEM0_URL = os.environ.get("MEM0_OSS_URL", "http://<MEM0_HOST_IP>:8888")
# Hardcode paths — $HOME in Docker resolves to /home/agent/.hermes/home (wrong)
_HERMES_BASE = "/home/agent/.hermes"
STATE_DB = os.path.join(_HERMES_BASE, "state.db")
LAST_HEALTHY_FILE = os.path.join(_HERMES_BASE, "mem0_last_healthy.json")
LOG_FILE = os.path.join(_HERMES_BASE, "logs", "mem0_monitor.log")

MEM0_USER_ID = "6228220870"
MEM0_AGENT_ID = "hermes"

# Fallback models to try if current model fails (priority order)
# 主用: qwen-next-80b (1s) — 备选: nemotron-mini-4b (1s) — 兜底: step-3.7-flash (1.4s)
FALLBACK_MODELS = [
    "nvidia/nemotron-mini-4b-instruct",
    "stepfun-ai/step-3.7-flash",
    "qwen/qwen3.5-122b-a10b",
]
DEFAULT_MODEL = "qwen/qwen3-next-80b-a3b-instruct"

API_BASE = "https://api.uge.cc/v1"
API_KEY = os.environ.get("MEM0_OSS_API_KEY", "")

# Auth: mem0 server requires JWT (AUTH_DISABLED=false).
# Login with email+password to get a Bearer token.
MEM0_EMAIL = os.environ.get("MEM0_OSS_EMAIL", "admin@hermesagent.com")
MEM0_PASSWORD = os.environ.get("MEM0_OSS_PASSWORD", "")
_TOKEN_CACHE = {"token": None, "expiry": 0.0}


# ── Logging ──────────────────────────────────────────────────────────
# log() writes to file always; stdout only on alert (anomaly/failure).
# This keeps cron delivery quiet when everything is normal.

_alert_msgs = []  # collect alert lines for stdout


def log(msg: str, alert: bool = False):
    """Write to log file. If alert=True, also queue for stdout delivery."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    if alert:
        _alert_msgs.append(line)
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _get_jwt_token() -> str:
    """Login to mem0 /auth/login and cache JWT (7-day TTL)."""
    if _TOKEN_CACHE["token"] and time.time() < _TOKEN_CACHE["expiry"] - 3600:
        return _TOKEN_CACHE["token"]
    url = f"{MEM0_URL}/auth/login"
    data = json.dumps({"email": MEM0_EMAIL, "password": MEM0_PASSWORD}).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = json.loads(resp.read())
    _TOKEN_CACHE["token"] = body["access_token"]
    _TOKEN_CACHE["expiry"] = time.time() + 7 * 24 * 3600
    log(f"Logged in as {MEM0_EMAIL}")
    return _TOKEN_CACHE["token"]


def _auth_header() -> str:
    """Return Authorization header value. Prefers JWT over API_KEY."""
    if MEM0_PASSWORD:
        return f"Bearer {_get_jwt_token()}"
    if API_KEY:
        return f"Bearer {API_KEY}"
    return ""


def api_get(path: str, timeout: int = 15) -> dict:
    """GET request to mem0 API."""
    url = f"{MEM0_URL}{path}"
    req = urllib.request.Request(url)
    auth = _auth_header()
    if auth:
        req.add_header("Authorization", auth)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def api_post(path: str, body: dict, timeout: int = 120) -> dict:
    """POST request to mem0 API."""
    url = f"{MEM0_URL}{path}"
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    auth = _auth_header()
    if auth:
        req.add_header("Authorization", auth)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


# ── Health Checks ───────────────────────────────────────────────────

def check_health() -> bool:
    """Check /health endpoint."""
    try:
        resp = api_get("/health", timeout=10)
        return resp.get("status") == "ok"
    except Exception as e:
        log(f"Health check failed: {e}", alert=True)
        return False


def check_search(retries: int = 2) -> bool:
    """Test search endpoint with a simple query. Retries on transient timeout."""
    for attempt in range(retries):
        try:
            resp = api_post("/search", {
                "query": "health check test",
                "user_id": MEM0_USER_ID,
                "top_k": 1,
            }, timeout=15)
            if "results" in resp:
                return True
            log(f"Search check: no 'results' key (attempt {attempt+1}/{retries})")
        except Exception as e:
            msg = str(e)[:100]
            log(f"Search check failed (attempt {attempt+1}/{retries}): {msg}")
            if attempt < retries - 1:
                time.sleep(5)
    log("Search check FAILED after retries", alert=True)
    return False


def check_write() -> bool:
    """Test write endpoint with a simple fact, then delete it."""
    try:
        resp = api_post("/memories", {
            "user_id": MEM0_USER_ID,
            "agent_id": MEM0_AGENT_ID,
            "messages": [
                {"role": "user", "content": "mem0 health check: ping"}
            ],
        }, timeout=30)
        results = resp.get("results", [])
        if not results:
            log("Write check: no results returned (LLM may be slow)")
            return False

        # Clean up test memory
        for r in results:
            mem_id = r.get("id")
            if mem_id:
                try:
                    url = f"{MEM0_URL}/memories/{mem_id}"
                    req = urllib.request.Request(url, method="DELETE")
                    auth = _auth_header()
                    if auth:
                        req.add_header("Authorization", auth)
                    urllib.request.urlopen(req, timeout=10)
                except Exception:
                    pass
        log("Write check: OK")
        return True
    except Exception as e:
        log(f"Write check failed: {e}", alert=True)
        return False


def get_current_model() -> str:
    """Get current LLM model from mem0 config."""
    try:
        cfg = api_get("/configure", timeout=10)
        return cfg.get("llm", {}).get("config", {}).get("model", "unknown")
    except Exception:
        return "unknown"


def switch_model(model: str) -> bool:
    """Switch mem0 LLM model via POST /configure."""
    try:
        resp = api_post("/configure", {
            "llm": {
                "provider": "openai",
                "config": {
                    "api_key": API_KEY,
                    "openai_base_url": API_BASE,
                    "temperature": 0.3,
                    "model": model,
                }
            }
        }, timeout=15)
        ok = resp.get("message") == "Configuration set successfully"
        if ok:
            log(f"Model switched to: {model}")
        return ok
    except Exception as e:
        log(f"Model switch failed: {e}", alert=True)
        return False


def attempt_repair() -> bool:
    """Try to fix mem0. Returns True if repaired."""
    log("Attempting repair...")

    # Step 1: Check if server is reachable at all
    if not check_health():
        log("mem0 server unreachable — cannot repair automatically", alert=True)
        return False

    # Step 2: Check search
    if not check_search():
        log("Search endpoint failing — trying model switch")
        current = get_current_model()
        log(f"Current model: {current}")

        for model in FALLBACK_MODELS:
            if model == current:
                continue
            log(f"Trying model: {model}")
            if switch_model(model):
                time.sleep(5)  # Let config propagate
                if check_search():
                    log(f"Repaired with model: {model}")
                    return True

        log("All model fallbacks exhausted", alert=True)
        return False

    # Step 3: Check write
    if not check_write():
        log("Write endpoint failing — trying model switch")
        current = get_current_model()
        for model in FALLBACK_MODELS:
            if model == current:
                continue
            if switch_model(model):
                time.sleep(5)
                if check_write():
                    log(f"Repaired with model: {model}")
                    return True

    # If we got here, search works but write might not
    # Try write again after potential model switch
    return check_write()


# ── Conversation Backfill ────────────────────────────────────────────

def get_last_healthy_time() -> float:
    """Read last healthy timestamp from file. Returns 0 if never recorded."""
    try:
        with open(LAST_HEALTHY_FILE) as f:
            data = json.load(f)
            return float(data.get("timestamp", 0))
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return 0.0


def set_last_healthy_time(ts: float):
    """Write last healthy timestamp to file."""
    os.makedirs(os.path.dirname(LAST_HEALTHY_FILE), exist_ok=True)
    with open(LAST_HEALTHY_FILE, "w") as f:
        json.dump({
            "timestamp": ts,
            "datetime": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
            "model": get_current_model(),
        }, f)


def extract_conversations(from_ts: float, to_ts: float) -> list[dict]:
    """
    Extract user+assistant conversation turns from state.db
    between from_ts and to_ts (Unix timestamps).

    Returns list of {session_id, messages: [{role, content}, ...]}
    """
    conn = sqlite3.connect(STATE_DB)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # Find sessions that overlap with [from_ts, to_ts]
    c.execute("""
        SELECT id, source, started_at, ended_at, title
        FROM sessions
        WHERE source = 'telegram'
          AND started_at <= ?
          AND (ended_at >= ? OR ended_at IS NULL)
        ORDER BY started_at ASC
    """, (to_ts, from_ts))

    sessions = c.fetchall()
    log(f"Found {len(sessions)} sessions in time range")

    conversations = []
    for sess in sessions:
        # Get user+assistant messages for this session in time range
        c.execute("""
            SELECT role, content, timestamp
            FROM messages
            WHERE session_id = ?
              AND role IN ('user', 'assistant')
              AND timestamp >= ?
              AND timestamp <= ?
              AND content IS NOT NULL
              AND content != ''
            ORDER BY timestamp ASC
        """, (sess["id"], from_ts, to_ts))

        msgs = c.fetchall()
        if not msgs:
            continue

        # Group into user→assistant turns
        turns = []
        current_user = None
        current_assistant = None

        for msg in msgs:
            if msg["role"] == "user":
                # If we have a complete turn, save it
                if current_user and current_assistant:
                    turns.append({
                        "user": current_user,
                        "assistant": current_assistant,
                    })
                current_user = msg["content"]
                current_assistant = None
            elif msg["role"] == "assistant" and current_user:
                current_assistant = msg["content"]

        # Don't forget the last turn
        if current_user and current_assistant:
            turns.append({
                "user": current_user,
                "assistant": current_assistant,
            })

        if turns:
            conversations.append({
                "session_id": sess["id"],
                "title": sess["title"],
                "turns": turns,
            })

    conn.close()
    return conversations


def backfill_to_mem0(conversations: list[dict], max_turns: int = 3) -> int:
    """
    Write extracted conversations to mem0 via POST /memories.
    Limits turns per cycle to avoid timeout (each write takes 60-90s).
    Returns number of turns successfully written.
    """
    written = 0
    for conv in conversations:
        if written >= max_turns:
            log(f"  Reached max_turns limit ({max_turns}), remaining will be backfilled next cycle")
            break
        for turn in conv["turns"]:
            if written >= max_turns:
                break
            try:
                # Truncate very long messages to avoid timeout
                user_text = turn["user"]
                assistant_text = turn["assistant"]
                if len(user_text) > 8000:
                    user_text = user_text[:8000] + "..."
                if len(assistant_text) > 8000:
                    assistant_text = assistant_text[:8000] + "..."

                api_post("/memories", {
                    "user_id": MEM0_USER_ID,
                    "agent_id": MEM0_AGENT_ID,
                    "messages": [
                        {"role": "user", "content": user_text},
                        {"role": "assistant", "content": assistant_text},
                    ],
                }, timeout=30)
                written += 1
                log(f"  Backfilled turn {written}: {user_text[:60]}...")
            except Exception as e:
                log(f"  Backfill failed for turn: {e}")
                # Continue with next turn — don't abort entire batch

    return written


# ── Main ─────────────────────────────────────────────────────────────

def main():
    log("=" * 50)
    log("mem0 Health Monitor starting")

    now = time.time()
    last_healthy = get_last_healthy_time()

    # ── Phase 1: Health Check (fast: health + search only) ──
    healthy = check_health() and check_search()

    # Ensure primary model is configured; only switch if not already on DEFAULT_MODEL
    current = get_current_model()
    if current != DEFAULT_MODEL:
        log(f"Current model {current} != primary {DEFAULT_MODEL}, restoring")
        switch_model(DEFAULT_MODEL)
        time.sleep(2)

    if not healthy:
        log("mem0 is UNHEALTHY — attempting repair", alert=True)
        repaired = attempt_repair()
        if not repaired:
            log("Repair FAILED — will retry next cycle", alert=True)
            sys.exit(1)
        log("Repair SUCCESSFUL")
        # After repair, verify write works too
        healthy = check_health() and check_search()
        if healthy:
            # Quick write verification (shorter timeout)
            if not check_write():
                log("Write still failing after repair — will monitor", alert=True)
                healthy = False

    if not healthy:
        log("Still unhealthy after repair — will retry next cycle", alert=True)
        # Don't exit 1 — transient timeout is not a hard failure
        print("⚠️ mem0 search timeout (transient) — will retry next cycle", flush=True)
        sys.exit(0)

    # ── Phase 2: Backfill ──
    # Backfill batch sized to fit under 30-min cron interval.
    # Each POST /memories can take up to 120s on slow New API channels.
    # 3 turns × 120s = 360s worst case, plus health check + retries, single
    # cycle stays under ~6 min. Incomplete batches auto-continue next cycle.
    backfill_incomplete = False
    if last_healthy > 0:
        gap_seconds = now - last_healthy
        if gap_seconds > 60:
            conversations = extract_conversations(last_healthy, now)
            total_turns = sum(len(c["turns"]) for c in conversations) if conversations else 0
            if total_turns > 0:
                # Batch 3 keeps single-cycle runtime under ~6 min even on slow LLM channels.
                batch = min(total_turns, 3)
                log(f"Backfill: {total_turns} turns pending, writing batch of {batch}")
                written = backfill_to_mem0(conversations, max_turns=3)
                log(f"Backfill: wrote {written}/{batch} turns")
                if written < batch:
                    backfill_incomplete = True
            else:
                log("No conversations to backfill")
        else:
            log(f"Gap too small ({gap_seconds:.0f}s), skipping backfill")
    else:
        log("First run — no previous healthy timestamp, skipping backfill")

    # ── Phase 3: Update timestamp ──
    # Only advance timestamp if all backfill is complete
    if not backfill_incomplete:
        set_last_healthy_time(now)
        log(f"Last healthy timestamp updated: {datetime.fromtimestamp(now, tz=timezone.utc).isoformat()}")
    else:
        log("Skipping timestamp update — backfill will continue next cycle")
    log("Monitor cycle complete")

    # ── Send alerts to stdout (cron delivery) ──
    # Only output if something went wrong; normal runs stay silent.
    if _alert_msgs:
        print("⚠️ mem0 健康监控异常：", flush=True)
        for msg in _alert_msgs:
            print(msg, flush=True)
    # Normal → no stdout → cron silently succeeds → no delivery to user


if __name__ == "__main__":
    main()
