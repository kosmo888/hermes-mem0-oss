# mem0-oss skill (sanitized documentation)

> ⚠️ **This is the SANITIZED public version** of the mem0-oss skill. Sensitive values (passwords, API keys, JWT secrets, IPs) have been replaced with `<PLACEHOLDER>` tokens.
>
> The full internal version with real credentials lives on Hermes' private skills directory and should NOT be republished.
>
> See `SKILL.md` § SANITIZED VERSION banner for the redaction map.

A documentation bundle that pairs with the [kosmo888/hermes-mem0-oss](https://github.com/kosmo888/hermes-mem0-oss) **Python plugin** (`__init__.py`) — itself a Memory Provider for [Hermes Agent](https://github.com/NousResearch/hermes-agent) that connects to self-hosted [mem0-official](https://github.com/mem0ai/mem0).

## What is this

```
mem0-oss/                      ← THIS repo (sanitized docs + scripts)
├── SKILL.md                   (Hermes-side SOP and pitfalls)
├── references/*.md            (10 reference docs)
└── scripts/*.py               (3 utility scripts)
```

The Python plugin that actually wires Hermes to mem0 lives in the same GitHub repo under `__init__.py` at the repo root.

## Install

1. Install the Python plugin: copy `__init__.py` to `~/.hermes/plugins/memory/mem0_oss/__init__.py` and set the env vars in `~/.hermes/.env`.
2. Drop this doc bundle into `~/.hermes/skills/devops/mem0-oss/` so Hermes can read the SOP and reference material.

## License

See upstream components for licensing.
