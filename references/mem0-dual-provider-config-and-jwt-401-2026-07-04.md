# mem0 双提供商配置持久化 & JWT 401 (2026-07-04, 续轮)

接续 `mem0-dedup-and-configure-outage-2026-07-04.md`。用户硬编码了双提供商（LLM=api.uge.cc, Embedder=siliconflow）配置，改了宿主机 `main.py` 和 `.env`，然后重启。重启后出现 JWT 401 错误——一个新坑。

## 持久化的双提供商配置（用户硬编码）

上一轮失败后，用户手动修改了三处宿主机文件以持久化正确的双提供商配置（确保重启保留配置）：

| 层级 | 文件 | 改动 |
|-------|------|--------|
| 容器服务端代码 | `/opt/1panel/docker/compose/mem0-official/server-src/server/main.py` (第 ~117 行) | 添加 `EMBEDDER_API_KEY = os.environ.get("EMBEDDER_API_KEY") or O[...]` 逻辑，使 `embedder` 的 `api_key` 从独立环境变量加载，而非复用 `OPENAI_API_KEY` |
| 容器环境配置 | `/opt/1panel/docker/compose/mem0-official/.env` (mem0-official `compose` 目录) | 将 `LLM` 和 `embedder` 拆分为不同环境变量，并补全 `siliconflow` 的 key |
| Hermes 侧 | `~/.hermes/.env` (代理侧） | 更新 `MEM0_OPENAI_API_KEY` 和 `MEM0_OPENAI_BASE_URL` 指向 `api.uge.cc`；保留 `api.uge.cc` 的 key；`MEM0_BACKEND_URL` 保持 `http://<MEM0_HOST_IP>:8888` 不变 |

### push 到 mem0 的最终配置

```
LLM:  qwen/qwen3-next-80b-a3b-instruct  @ https://api.uge.cc/v1
      api_key: sk-<LEGACY_LLM_API_KEY_REDACTED>
Embedder: BAAI/bge-m3 (大写!)  @ https://api.siliconflow.cn/v1
          api_key: sk-<EMBEDDER_API_KEY_REDACTED>
```

**是否持久化的关键检查**：执行 `docker restart mem0-server`，然后 `GET /configure` —— 结果显示已加载的模型和提供商。如果 `runtime POST /configure` 的更改重启后还原回了默认值，说明 `.env`/`main.py` 补丁未应用。重启后应始终仅依赖环境变量值。

### `main.py` 补丁的意思

在修补 `main.py` 之前：`OPENAI_API_KEY` 同时用于 `LLM` 和 `embedder`（单一环境变量，mem0 默认）——这是导致在一个更新却破坏另一个的根本原因。补丁后：`EMBEDDER_API_KEY` 是一个专用变量，因此更新 `OPENAI_API_KEY`（`LLM` 侧）无法触及 `embedder` 侧。**后续 mem0 运行时若同样遇到 key 挂载问题，可采用同样的补丁。**

## 重启后的已知坑（本轮新增）

重启后 `postgres` 和 `mem0-server` 都启动了，但认证失败：

### 坑 1：`mem0_search` 返回 401 但服务在运行

诊断序列应是：
```
curl http://<MEM0_HOST_IP>:8888/health                           -> {"status":"ok"}  ✅
curl http://<MEM0_HOST_IP>:8888/memories -H "Authorization: Bearer sk-..." 
                                                             -> 401 "Invalid or expired token."
curl http://<MEM0_HOST_IP>:8888/memories -H "X-API-Key: POb06Jd-..." 
                                                             -> 401 "Invalid API key."
```

**重要**：mem0 REST API 有 **两个独立的 auth surfaces**（认证接口）：
1. **JWT Bearer token**（通过 `POST /auth/login` 获取 `email`+`password`，返回 `access_token`）——这是 `/memories`、`/search` 等的标准认证方式
2. **X-API-Key header** ——通过 `POST /configure` 配置的 admin API key `POb06Jd-...`。**直接撞 X-API-Key 会 401**，因为 admin key 已作为 JWT 分发，不再作为直接的 API-key auth surface。

之前那种“POST `/memories` 不需要 JWT”（v2.3.0 确认）的说法是基于 `user_id` 绑定的 auto-injection —— 在最近重启或配置变更后，未加 JWT 的写操作可能也会开始失败。如果遇到 401，请先用 `/auth/login` 获取 JWT。

### 坑 2：`MEM0_ADMIN_PASSWORD` 与数据库中的密码不一致

`.env` 中 `MEM0_ADMIN_PASSWORD=hermes-mem0-2024`，但 `POST /auth/login` 用该密码返回 `"Invalid email or password."`。原因：postgres 重启（伴随存储卷重建或 `compose` 配置变更）可能丢弃了用户表，或者 mem0 启动时使用了不同密码走了 setup。

诊断：
```bash
# 看 mem0-server 容器启动日志中是否走了 setup 页面
docker logs mem0-server 2>&1 | grep -iE "pass|admin|setup|initial|seed"

# 或者直接进 postgres 看用户表
ssh -o StrictHostKeyChecking=accept-new -i /home/agent/.hermes/home/.ssh/id_ed25519 root@<MEM0_HOST_IP> \
  "docker exec mem0-postgres psql -U postgres -d postgres -c '\\dt'"
# 如果 users 表不存在 → setup 没完成
# 如果 users 表有 admin@hermesagent.com 但密码 hash 不同 → 密码对不上
```

恢复路径：如果 setup 还没完成，`/auth/setup-status` 返回 `{"needsSetup": true}`，则用 `POST /auth/register` 注册 admin，然后执行后续。如果提示 "Registration is closed, An admin account already exists" 但密码对不上，需要直接进 postgres 重置密码 hash，或重建 mem0-server 容器并强制 setup。

**同一个 admin 用户对应两个独立的密码系统**：
1. mem0 REST API 管理用户（POSTGRES 中 `users` 表）—— 用 `admin@hermesagent.com` / `hermes-mem0-2024` 登录，返回 JWT
2. mem0-server 内部 `ADMIN_API_KEY`（即 `<POSTGRES_PASSWORD>`）—— 这是 server 端发放给我们的一个固定 token，不是 JWT，也不通过 `/auth/login` 走

当 JWT 登录失败时，不要假设 `ADMIN_API_KEY` 能作为 Bearer 使用 —— 它走的是不同的 auth surface。

### 坑 3：`/auth/setup-status` 是零成本便宜探针

当 auth 状态不明时，先打这个端点：
- `{"needsSetup": false}` —— 已 setup，admin 账户存在，密码应该在 postgres 里
- `{"needsSetup": true}` —— 全新部署，menu 可以注册第一个 admin

比 `/auth/login` 试错好，因为不会触发 rate-limit，也不会让你误判密码。

## 双提供商直链健康分诊 (post-restart)

```python
# 1. postgres 
ssh root@<MEM0_HOST_IP> "docker exec mem0-postgres pg_isready"  
# 健康 → "/var/run/postgresql:5432 - accepting connections"

# 2. mem0-server /health (不碰 DB / embedder)
requests.get("http://<MEM0_HOST_IP>:8888/health")
# → {"status":"ok"}  ≠ 数据/搜索可用，只说明 HTTP 服务起得来

# 3. LLM 提取器：POST /generate-instructions (只走 LLM)
requests.post("http://<MEM0_HOST_IP>:8888/generate-instructions",
              json={"messages":[{"role":"user","content":"hello"}]})
# → 200 + 正文 → LLM 提取器活着

# 4. Embedder：POST /search (LLM + embedder，一次走通)
requests.post("http://<MEM0_HOST_IP>:8888/search",
              json={"query":"test","user_id":"6228220870","agent_id":"hermes","top_k":3})
# → 200 + results → embedder 活着；502/timeout → embedder 挂了或是网络问题

# 5. /configure 看 LLM 和 embedder 配置 (key 会 redacted)
requests.get("http://<MEM0_HOST_IP>:8888/configure")
# → llm.provider="openai", llm.config.model 是什么, embedder.provider 是什么
```

顺序很关键：按 1→2→3→4 走，哪一刀断了就见血，不要跳级。这种 "isolation probing" 上次错误诊断 embedder-down 30 分钟就是因为跳级了。
