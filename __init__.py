"""
Mem0 OSS MemoryProvider — connects to self-hosted mem0-official server.

A Hermes Agent memory provider plugin that syncs built-in memory writes
to a self-hosted mem0-official server (REST API), enabling shared,
cross-instance semantic memory backed by pgvector.

Shared memory backend for multiple Hermes instances. Each instance uses
a distinct user_id for attribution; all memories live in the same pgvector
database and are cross-searchable.

Features:
  - Auto-mirrors built-in memory writes to mem0 server
  - Semantic search across all instances (mem0_search)
  - Full profile retrieval (mem0_profile)
  - Circuit breaker for API resilience
  - Optional cross-user read mode (MEM0_OSS_READ_ALL=true)

Config via environment variables:
  MEM0_OSS_URL          — mem0-official server URL (default: http://localhost:8888)
  MEM0_OSS_EMAIL        — Login email (default: admin@mem0.local)
  MEM0_OSS_PASSWORD     — Login password (required)
  MEM0_OSS_USER_ID      — User identifier for this instance (default: hermes)
  MEM0_OSS_AGENT_ID     — Agent identifier (default: hermes)
  MEM0_OSS_READ_ALL     — If "true", search returns memories from ALL users,
                           not just the current user_id. Useful when multiple
                           Hermes instances share the same mem0 server but use
                           different user_ids.

Deployment:
  1. Copy __init__.py to ~/.hermes/plugins/memory/mem0_oss/__init__.py
     (for Docker Hermes: ~/.hermes/plugins/mem0_oss/__init__.py)
  2. Set env vars in ~/.hermes/.env
  3. hermes config set memory.provider mem0-oss
  4. hermes config set plugins.enabled '["memory/mem0_oss"]'
     (Docker: hermes config set plugins.enabled '["mem0_oss"]')
  5. Restart Hermes gateway

Prerequisites:
  - A running mem0-official server (https://github.com/mem0ai/mem0)
  - Network access from Hermes to the mem0 server

Dependencies:
  - Python stdlib only (urllib, threading, json, os, logging)
  - No pip packages required
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider
from tools.registry import tool_error

logger = logging.getLogger(__name__)

BREAKER_THRESHOLD = 5
BREAKER_COOLDOWN_SECS = 120

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _load_config() -> dict:
    return {
        "url": os.environ.get("MEM0_OSS_URL", "http://localhost:8888"),
        "email": os.environ.get("MEM0_OSS_EMAIL", "admin@mem0.local"),
        "password": os.environ.get("MEM0_OSS_PASSWORD", ""),
        "user_id": os.environ.get("MEM0_OSS_USER_ID", "hermes"),
        "agent_id": os.environ.get("MEM0_OSS_AGENT_ID", "hermes"),
    }


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

PROFILE_SCHEMA = {
    "name": "mem0_profile",
    "description": "Retrieve all stored memories about the user — preferences, facts, project context.",
    "parameters": {"type": "object", "properties": {}, "required": []},
}

SEARCH_SCHEMA = {
    "name": "mem0_search",
    "description": "Search memories by meaning. Returns relevant facts ranked by similarity.",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to search for."},
            "top_k": {"type": "integer", "description": "Max results (default: 10, max: 50)."},
        },
        "required": ["query"],
    },
}


# ---------------------------------------------------------------------------
# MemoryProvider implementation
# ---------------------------------------------------------------------------

class Mem0OssMemoryProvider(MemoryProvider):
    """Memory provider backed by self-hosted mem0-official REST API."""

    def __init__(self):
        self._url = "http://localhost:8888"
        self._user_id = "hermes"
        self._agent_id = "hermes"
        self._token: Optional[str] = None
        self._token_lock = threading.Lock()
        self._token_expiry: float = 0.0
        self._prefetch_result = ""
        self._prefetch_lock = threading.Lock()
        self._prefetch_thread: Optional[threading.Thread] = None
        self._sync_thread: Optional[threading.Thread] = None
        # Circuit breaker
        self._consecutive_failures = 0
        self._breaker_open_until = 0.0

    @property
    def name(self) -> str:
        return "mem0-oss"

    def is_available(self) -> bool:
        cfg = _load_config()
        return bool(cfg.get("password"))

    def get_config_schema(self):
        return [
            {
                "key": "url", "description": "mem0-official server URL",
                "default": "http://localhost:8888", "env_var": "MEM0_OSS_URL",
            },
            {
                "key": "email", "description": "Login email",
                "default": "admin@mem0.local", "env_var": "MEM0_OSS_EMAIL",
            },
            {
                "key": "password", "description": "Login password",
                "secret": True, "required": True, "env_var": "MEM0_OSS_PASSWORD",
            },
            {
                "key": "user_id", "description": "User identifier for this instance",
                "default": "hermes", "env_var": "MEM0_OSS_USER_ID",
            },
            {
                "key": "agent_id", "description": "Agent identifier",
                "default": "hermes", "env_var": "MEM0_OSS_AGENT_ID",
            },
        ]

    def initialize(self, session_id: str, **kwargs) -> None:
        cfg = _load_config()
        self._url = cfg["url"].rstrip("/")
        self._user_id = kwargs.get("user_id") or cfg["user_id"]
        self._agent_id = cfg["agent_id"]
        # Pre-load token
        self._login()

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def _login(self) -> bool:
        """Log into mem0-official, store JWT token. Thread-safe."""
        with self._token_lock:
            # Check if existing token is still fresh (tokens are 24h, refresh after 23h)
            if self._token and time.time() < self._token_expiry - 3600:
                return True

            cfg = _load_config()
            email = cfg["email"]
            password = cfg["password"]
            if not password:
                logger.warning("mem0-oss: MEM0_OSS_PASSWORD not set, cannot login")
                return False

            data = json.dumps({"email": email, "password": password}).encode()
            try:
                req = urllib.request.Request(
                    f"{self._url}/auth/login",
                    data=data,
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    body = json.loads(resp.read())
                self._token = body["access_token"]
                self._token_expiry = time.time() + 24 * 3600  # 24h
                logger.info("mem0-oss: logged in as %s", email)
                return True
            except Exception as e:
                logger.warning("mem0-oss: login failed: %s", e)
                return False

    def _api(self, method: str, path: str, body: Optional[dict] = None) -> dict:
        """Make an authenticated API call. Auto-refreshes token if needed."""
        if self._is_breaker_open():
            raise RuntimeError("Circuit breaker open")

        # Ensure we have a token
        if not self._login():
            raise RuntimeError("Not authenticated")

        url = f"{self._url}{path}"
        data = json.dumps(body).encode() if body else None

        req = urllib.request.Request(
            url, data=data, method=method,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            },
        )

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read())
            self._record_success()
            return result
        except urllib.error.HTTPError as e:
            body_text = e.read().decode(errors="replace")
            logger.warning("mem0-oss: HTTP %d on %s %s: %s", e.code, method, path, body_text)
            # 401 = token expired, retry after refresh
            if e.code == 401:
                self._token = None
                if self._login():
                    # Retry once
                    req = urllib.request.Request(
                        url, data=data, method=method,
                        headers={
                            "Authorization": f"Bearer {self._token}",
                            "Content-Type": "application/json",
                        },
                    )
                    try:
                        with urllib.request.urlopen(req, timeout=30) as resp2:
                            result = json.loads(resp2.read())
                        self._record_success()
                        return result
                    except Exception:
                        pass
            self._record_failure()
            raise RuntimeError(f"API error {e.code}: {body_text}")
        except Exception as e:
            self._record_failure()
            raise RuntimeError(f"API call failed: {e}")

    # ------------------------------------------------------------------
    # Circuit breaker
    # ------------------------------------------------------------------

    def _is_breaker_open(self) -> bool:
        if self._consecutive_failures < BREAKER_THRESHOLD:
            return False
        if time.monotonic() >= self._breaker_open_until:
            self._consecutive_failures = 0
            return False
        return True

    def _record_success(self):
        self._consecutive_failures = 0

    def _record_failure(self):
        self._consecutive_failures += 1
        if self._consecutive_failures >= BREAKER_THRESHOLD:
            self._breaker_open_until = time.monotonic() + BREAKER_COOLDOWN_SECS
            logger.warning(
                "mem0-oss: breaker tripped after %d failures. Cooldown %ds.",
                self._consecutive_failures, BREAKER_COOLDOWN_SECS,
            )

    # ------------------------------------------------------------------
    # Read filters (used for search/get)
    # ------------------------------------------------------------------

    def _read_filters(self) -> Dict[str, Any]:
        if os.environ.get("MEM0_OSS_READ_ALL", "").lower() == "true":
            return {}
        return {"user_id": self._user_id}

    def _write_filters(self) -> Dict[str, Any]:
        return {"user_id": self._user_id, "agent_id": self._agent_id}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def system_prompt_block(self) -> str:
        return (
            "# Mem0 OSS Memory\n"
            f"Active. Connected to {self._url}. User: {self._user_id}.\n"
            "Use mem0_search to find memories, mem0_profile for a full overview. "
            "The built-in memory tool auto-mirrors to mem0."
        )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if self._prefetch_thread and self._prefetch_thread.is_alive():
            self._prefetch_thread.join(timeout=3.0)
        with self._prefetch_lock:
            result = self._prefetch_result
            self._prefetch_result = ""
        if not result:
            return ""
        return f"## Mem0 Memory\n{result}"

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        if self._is_breaker_open():
            return

        def _run():
            try:
                resp = self._api(
                    "POST", "/search",
                    {"query": query, "filters": self._read_filters(), "top_k": 5},
                )
                results = resp.get("results", []) if isinstance(resp, dict) else resp
                if results:
                    lines = []
                    for r in results:
                        memory = r.get("memory", "")
                        if memory:
                            score = r.get("score", 0)
                            lines.append(f"- {memory} (score: {score:.2f})" if score else f"- {memory}")
                    with self._prefetch_lock:
                        self._prefetch_result = "\n".join(lines)
            except Exception as e:
                logger.debug("mem0-oss: prefetch failed: %s", e)

        self._prefetch_thread = threading.Thread(target=_run, daemon=True, name="mem0oss-prefetch")
        self._prefetch_thread.start()

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """Send the turn to mem0-official for server-side fact extraction."""
        if self._is_breaker_open():
            return

        def _sync():
            try:
                self._api(
                    "POST", "/memories",
                    {
                        "user_id": self._user_id,
                        "agent_id": self._agent_id,
                        "messages": [
                            {"role": "user", "content": user_content},
                            {"role": "assistant", "content": assistant_content},
                        ],
                    },
                )
                logger.debug("mem0-oss: sync_turn OK")
            except Exception as e:
                logger.warning("mem0-oss: sync_turn failed: %s", e)

        if self._sync_thread and self._sync_thread.is_alive():
            self._sync_thread.join(timeout=5.0)

        self._sync_thread = threading.Thread(target=_sync, daemon=True, name="mem0oss-sync")
        self._sync_thread.start()

    # ------------------------------------------------------------------
    # Optional hooks
    # ------------------------------------------------------------------

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Mirror built-in memory writes to mem0-official."""
        if self._is_breaker_open():
            return
        if action not in ("add", "replace"):
            return  # skip removes

        def _mirror():
            try:
                self._api(
                    "POST", "/memories",
                    {
                        "user_id": self._user_id,
                        "agent_id": self._agent_id,
                        "messages": [
                            {"role": "user", "content": f"[{target}] {content}"},
                        ],
                    },
                )
                logger.debug("mem0-oss: memory mirror OK (%s:%s)", target, action)
            except Exception as e:
                logger.debug("mem0-oss: memory mirror failed: %s", e)

        threading.Thread(target=_mirror, daemon=True, name="mem0oss-mirror").start()

    # ------------------------------------------------------------------
    # Tools
    # ------------------------------------------------------------------

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [PROFILE_SCHEMA, SEARCH_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: dict, **kwargs) -> str:
        if self._is_breaker_open():
            return json.dumps({
                "error": "Mem0 API temporarily unavailable (circuit breaker open). Will retry automatically."
            })

        if tool_name == "mem0_profile":
            try:
                resp = self._api(
                    "GET", f"/memories?user_id={self._user_id}&page=1&size=50"
                )
                if isinstance(resp, list):
                    memories = resp
                elif isinstance(resp, dict):
                    memories = resp.get("items", []) or resp.get("results", []) or []
                else:
                    memories = []
                if not memories:
                    return json.dumps({"result": "No memories stored yet."})
                lines = [m.get("memory", "") for m in memories if m.get("memory")]
                return json.dumps({"result": "\n".join(lines), "count": len(lines)})
            except Exception as e:
                return tool_error(f"mem0_profile failed: {e}")

        elif tool_name == "mem0_search":
            query = args.get("query", "")
            if not query:
                return tool_error("Missing required parameter: query")
            top_k = min(int(args.get("top_k", 10)), 50)
            try:
                resp = self._api(
                    "POST", "/search",
                    {
                        "query": query,
                        "filters": self._read_filters(),
                        "top_k": top_k,
                    },
                )
                results = resp.get("results", []) if isinstance(resp, dict) else []
                if not results:
                    return json.dumps({"result": "No relevant memories found."})
                items = [{"memory": r.get("memory", ""), "score": r.get("score", 0)} for r in results]
                return json.dumps({"results": items, "count": len(items)})
            except Exception as e:
                return tool_error(f"mem0_search failed: {e}")

        return tool_error(f"Unknown tool: {tool_name}")

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        for t in (self._prefetch_thread, self._sync_thread):
            if t and t.is_alive():
                t.join(timeout=5.0)
        self._token = None


def register(ctx) -> None:
    """Register mem0-oss as a memory provider plugin."""
    ctx.register_memory_provider(Mem0OssMemoryProvider())
