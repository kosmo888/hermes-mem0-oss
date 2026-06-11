# Hermes mem0-oss Memory Provider

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) memory provider plugin that connects to a self-hosted [mem0-official](https://github.com/mem0ai/mem0) server, enabling shared cross-instance semantic memory backed by pgvector.

## Features

- **Auto-mirroring**: Built-in `memory` tool writes are automatically synced to mem0
- **Semantic search**: `mem0_search` tool for cross-session semantic retrieval
- **Profile overview**: `mem0_profile` tool lists all stored memories
- **Circuit breaker**: Automatic cooldown on API failures
- **Cross-instance**: Multiple Hermes instances share one mem0 database
- **Cross-user mode**: Optional `MEM0_OSS_READ_ALL=true` to search across all user_ids
- **Zero dependencies**: Python stdlib only — no pip packages required

## Prerequisites

### 1. mem0-official server

mem0-official runs as three Docker containers on the host:

| Container | Port | Description |
|-----------|------|-------------|
| mem0-server | 8888 | REST API server |
| mem0-postgres | 8433 | pgvector database |
| mem0-dashboard | 3006 | Web management UI |

Install from: **[github.com/mem0ai/mem0](https://github.com/mem0ai/mem0)**

### 2. Hermes Agent

v0.16.0+ ([github.com/NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent))

### 3. Network access

Hermes must be able to reach the mem0-server container (typically `localhost:8888` on the same host, or `172.x.x.x:8888` for Docker-to-Docker).

## Quick Start

### 1. Install the plugin

```bash
# Create the plugin directory
mkdir -p ~/.hermes/plugins/memory/mem0_oss/

# Copy the plugin file
cp __init__.py ~/.hermes/plugins/memory/mem0_oss/__init__.py
```

> **Docker Hermes users**: the plugin goes in `~/.hermes/plugins/mem0_oss/` (NOT the `memory/` subdirectory).

### 2. Configure environment variables

Add to `~/.hermes/.env`:

```bash
MEM0_OSS_URL=http://localhost:8888          # mem0 server URL
MEM0_OSS_EMAIL=admin@mem0.local             # mem0 login email
MEM0_OSS_PASSWORD=your-password             # mem0 login password
MEM0_OSS_USER_ID=hermes                     # user identifier (unique per instance)
MEM0_OSS_AGENT_ID=hermes                    # agent identifier
```

### 3. Enable the plugin

```bash
hermes config set memory.provider mem0-oss
hermes config set plugins.enabled '["memory/mem0_oss"]'
```

> **Docker Hermes**: use `'["mem0_oss"]'` (without `memory/` prefix).

### 4. Restart

```bash
# Native Hermes
hermes gateway restart

# Docker Hermes
docker restart hermes-agent
```

Then send `/new` to start a fresh session that loads the new provider.

## Cross-Instance Sharing

To share memories across multiple Hermes instances that use **different `user_id`** values:

```bash
MEM0_OSS_READ_ALL=true
```

When set to `true`, `mem0_search` returns memories from ALL users on the server,
not just the current `user_id`. Each instance still writes with its own `user_id`
for attribution.

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `MEM0_OSS_URL` | `http://localhost:8888` | mem0-official server URL |
| `MEM0_OSS_EMAIL` | `admin@mem0.local` | Login email |
| `MEM0_OSS_PASSWORD` | *(required)* | Login password |
| `MEM0_OSS_USER_ID` | `hermes` | User identifier for this instance |
| `MEM0_OSS_AGENT_ID` | `hermes` | Agent identifier |
| `MEM0_OSS_READ_ALL` | `false` | If `true`, search returns all users' memories |

## Tools Provided

### `mem0_search`

Search memories by semantic meaning.

```
mem0_search(query="what do I know about the project")
```

### `mem0_profile`

Retrieve all stored memories for the current user.

```
mem0_profile()
```

## Architecture

```
┌──────────────────┐     ┌──────────────────┐
│  Native Hermes    │     │  Docker Hermes    │
│  user_id=alice    │     │  user_id=bob      │
└────────┬──────────┘     └────────┬──────────┘
         │   REST API              │   REST API
         └────────────┬────────────┘
                      │
         ┌────────────▼────────────┐
         │    mem0-server (8888)   │
         │    REST API + LLM       │
         └────────────┬────────────┘
                      │
         ┌────────────▼────────────┐
         │  mem0-postgres (8433)   │
         │  pgvector extension     │
         └─────────────────────────┘

         ┌─────────────────────────┐
         │  mem0-dashboard (3006)  │
         │  Web management UI      │
         └─────────────────────────┘
```

## Notes

- mem0-official uses server-side LLM fact extraction — raw memory text is rephrased into concise facts
- The built-in `memory` tool writes to local SQLite; the plugin mirrors these writes to mem0 via the `on_memory_write` hook
- Login tokens are cached for 23 hours, auto-refreshed on expiry
- Circuit breaker trips after 5 consecutive failures, cools down for 120s

## License

MIT
