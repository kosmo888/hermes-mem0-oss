# mem0 LLM Model Benchmark — 2026-06-27

## Method

Tested on the actual mem0 workload: a typical fact-extraction prompt in Chinese, simulating "我养了一只橘猫，叫小黑，3岁".

```python
prompt = """请从以下对话中提取出对用户有长期价值的偏好/事实/规则信息。
以简洁的陈述句输出，每条不超过30字。如果对话中无有价值信息，回复"无"。

对话:
用户: 我养了一只橘猫，叫小黑，3岁。
助手: 好的，记下来了。
"""
body = {
    "model": mid,
    "messages": [{"role":"user","content": prompt}],
    "max_tokens": 200,
    "temperature": 0.3,
}
```

Endpoint: `https://api.uge.cc/v1/chat/completions` (New API gateway on Cloudflare → <US_SERVER_IP>)

## Results (sorted by latency)

| # | Model | Time | In/Out | Output preview | Verdict |
|---|-------|------|--------|----------------|---------|
| 1 | `nvidia/nemotron-mini-4b-instruct` | 1.09s | 98/20 | 记录: 用户养了一只3岁橘猫，名字叫小黑。 | ⭐ Selected as fallback 1 |
| 2 | `qwen/qwen3-next-80b-a3b-instruct` | 1.03s | 88/14 | 用户养了一只3岁的橘猫，名叫小黑。 | ⭐ Selected as primary |
| 3 | `meta/llama-3.1-8b-instruct` | 0.85s | 120/2 | 无 | ❌ Rejected — Chinese instruction-following weak |
| 4 | `google/gemma-3-4b-it` | — | — | — | ❌ 404 (not deployed) |
| 5 | `nvidia/nemotron-nano-3-30b-a3b` | — | — | — | ❌ 404 (not deployed) |
| 6 | `openai/gpt-oss-20b` | 2.12s | 145/189 | 用户养橘猫   猫名叫小黑   猫3岁 | Rejected — splits into separate facts |
| 7 | `qwen/qwen3.5-122b-a10b` | 16.25s | 83/12 | 用户养了一只3岁的橘猫，名字叫小黑。 | Emergency fallback only (16× slower) |
| 8 | `deepseek-ai/deepseek-v4-flash` | 5.19s | 72/12 | 用户养了一只3岁的橘猫，名叫小黑。 | Acceptable, not selected |
| 9 | `nvidia/llama-3.3-nemotron-super-49b-v1.5` | timeout | — | NoneType.strip() error | ❌ REMOVED — 60-120s + fact extraction bug |
| 10 | `minimaxai/minimax-m3` | 11.56s | 227/12 | 用户养了一只3岁的橘猫，名叫小黑。 | Acceptable, but expensive |
| 11 | `minimaxai/minimax-m2.7` | 2.38s | 102/100 | 用户养了一只橘猫，叫小黑，3岁。 | Acceptable but DEGRADED risk |
| 12 | `moonshotai/kimi-k2.6` | 7.07s | 75/15 | — 不对，不对，不对，不对… | ❌ Rejected — incoherent output |
| 13 | `z-ai/glm-5.1` | 1.82s | 76/13 | 用户养了一只3岁橘猫，名叫小黑。 | Acceptable |
| 14 | `stepfun-ai/step-3.7-flash` | 1.42s | 83/120 | 用户养有一只3岁的橘猫，名为小黑。 | ⭐ Selected as fallback 2 |
| 15 | `mistralai/mistral-small-4-119b-2603` | 1.09s | 115/31 | - 用户养了一只名为"小黑"的橘猫。 - 小黑的年龄为3岁。 | Rejected — bullet format |
| 16 | `google/gemma-3-12b-it` | — | — | — | ❌ 404 (not deployed) |

## Selection Criteria

1. **Latency under 2s** for primary/fallback — mem0 POST `/memories` adds 7-9s of embedder + pgvector + RTT on top, so the LLM should be fast.
2. **Single concise output line** — mem0 expects one fact per call, not bullets or splits.
3. **Chinese instruction-following** — user messages are predominantly Chinese.
4. **Stable** — must not return empty bodies or hallucinate attributes.

## Why qwen-next-80b over alternatives

- **80B total but A3B (3B active)** — MoE keeps inference cost low while knowledge is broad.
- **Consistent latency ~1s** across multiple test runs.
- **Single-line output, no bullet splitting** (unlike gpt-oss-20b).
- **No DEGRADED state risk** (unlike minimax-m2.7 which has known empty-response failures).

## Why nemotron-mini-4b as fallback 1

- **1.09s latency** — same as primary.
- **NVIDIA NIM channel** — separate from qwen route, so if qwen has an outage, nemotron still works.
- **4B params** — extremely cheap, fits emergency use.

## Why step-3.7-flash as fallback 2

- **1.42s** — slightly slower but still under 2s.
- **Independent vendor** (stepfun) — third route if qwen AND nemotron both fail.

## Why qwen3.5-122b as emergency only

- **16.25s** — too slow for cron-backed writes.
- But it works when all faster options fail.
- Kept in fallback chain as last resort.

## Mem0 write latency (with primary)

| Stage | Time |
|-------|------|
| LLM fact extraction | 1s |
| Embedder (baai/bge-m3) | 1-2s |
| pgvector HNSW insert | 1-2s |
| Network RTT × 4 (Oracle ↔ US) | 2.4s |
| Misc (auth, parsing) | 0.5s |
| **Total** | **8-10s** |

Cold first call after restart: 14-15s (model warmup + connection pool init).
Warm steady-state: 8-10s.

## What's NOT in this benchmark

- Models with 404 (not actually deployed on the channel) — would need a different test
- Long-context performance (>32K tokens) — not relevant to mem0
- Tool calling / function use — mem0 doesn't use it
- Vision models — mem0 is text-only

## How to re-run

```python
import urllib.request, json, time

SK = "your-newapi-token"  # sk-leql9... or sk-n7ineS...
candidates = [
    "qwen/qwen3-next-80b-a3b-instruct",
    "nvidia/nemotron-mini-4b-instruct",
    "stepfun-ai/step-3.7-flash",
    # add more...
]

prompt = """请从以下对话中提取出对用户有长期价值的偏好/事实/规则信息。..."""

for mid in candidates:
    body = {"model": mid, "messages":[{"role":"user","content":prompt}],
            "max_tokens":200, "temperature":0.3}
    t0 = time.time()
    try:
        r = json.loads(urllib.request.urlopen(urllib.request.Request(
            "https://api.uge.cc/v1/chat/completions",
            data=json.dumps(body).encode(),
            headers={"Authorization":f"Bearer {SK}","Content-Type":"application/json"}),
            timeout=30).read())
        dt = time.time()-t0
        text = r["choices"][0]["message"]["content"].strip()
        print(f"{mid:<48} {dt:>5.2f}s | {text[:80]}")
    except Exception as e:
        print(f"{mid:<48} ❌ {str(e)[:60]}")
```
