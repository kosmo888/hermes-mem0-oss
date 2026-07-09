# JWT 过期永久化 + 凭证固化 (2026-07-06)

> 接续 `mem0-dual-provider-config-and-jwt-401-2026-07-04.md` 的坑 2。本轮把 `users` 表位置/列名/密码重置配方/JWT 过期硬编码点全部查证并永久化。

## 痛点：mem0 自动签发的 JWT 默认过期很快

源码 `/app/auth.py` 第 17-18 行硬编码了 token 过期（不是环境变量）：

```python
ACCESS_TOKEN_EXPIRE_MINUTES = 30   # 30 分钟
REFRESH_TOKEN_EXPIRE_DAYS = 30     # 30 天（用户原印象 "7 天" 是别处残留）
```

每次 `docker exec mem0-server env` 都不会暴露这两个常量——**改 .env / compose environment 段都没用**，必须改源码。用户反馈：每次 token 过期就要重新登录才能调 `/memories` / `/search`，运维负担很重。

## users 表真正的位置（坑 2 升级版）

`mem0-dual-provider-config-and-jwt-401-2026-07-04.md` 里写的 `psql -U postgres -d postgres -c '\dt'` 会报 `relation "users" does not exist`——**因为表在另一个 db 里**：

```sql
-- 错的：在 postgres db 里找
docker exec mem0-postgres psql -U postgres -d postgres -c '\dt'   -- 没有 users
-- 正确：用 mem0_app db
docker exec mem0-postgres psql -U postgres -d mem0_app -c '\dt'
-- 6 张表：alembic_version / api_keys / refresh_token_jtis
-- / request_logs / settings / users
```

列名是 **`password_hash`** 不是 `password`：

```
ERROR: column "password" of relation "users" does not exist
-- \d users  显示真实列名是 password_hash
```

users 表结构关键列：`id (uuid) | name | email | password_hash (text, bcrypt) | role | created_at | last_login_at`

唯一一行数据：`admin@hermesagent.com` / role=admin。

## 密码重置配方（无需重启容器）

```bash
ssh -i ~/.hermes/home/.ssh/id_ed25519 root@<MEM0_HOST_IP> << 'EOF'
# Step 1：在 mem0-server 容器里用 bcrypt 生成新 hash
NEW_PW='hermes-mem0-2024'  # ← 任意改
HASH=$(docker exec mem0-server python3 -c "import bcrypt;print(bcrypt.hashpw(b'${NEW_PW}',bcrypt.gensalt()).decode())")

# Step 2：写进 postgres 的 mem0_app.users 表
docker exec mem0-postgres psql -U postgres -d mem0_app -c \
  "UPDATE users SET password_hash='$HASH' WHERE email='admin@hermesagent.com' RETURNING id,email;"
EOF
```

立即可用——不用 `docker restart`，bcrypt 是无状态的。

## JWT 过期永久化配方（关键贡献）

**根因**：`/app/auth.py` 第 17-18 行 Python 模块级常量，**不是 env var**，改 .env / environment 都无效。

**永久化思路**：容器内源码 → 宿主文件 → `:ro` 挂载回去 → compose 容忍重建。

```bash
ssh -i ~/.hermes/home/.ssh/id_ed25519 root@<MEM0_HOST_IP> << 'EOF'
cd /opt/1panel/docker/compose/mem0-official

# Step 1：备份容器内原文件
docker exec mem0-server cp /app/auth.py /app/auth.py.bak.$(date +%Y%m%d-%H%M%S)

# Step 2：sed 改两个常量（设成 10 年）
docker exec mem0-server sh -c "
  sed -i 's|^ACCESS_TOKEN_EXPIRE_MINUTES = 30|ACCESS_TOKEN_EXPIRE_MINUTES = 5256000  # 10 years (永久化)|;
          s|^REFRESH_TOKEN_EXPIRE_DAYS = 30|REFRESH_TOKEN_EXPIRE_DAYS = 3650  # 10 years (永久化)|' \
  /app/auth.py && \
  grep -E 'ACCESS_TOKEN_EXPIRE|REFRESH_TOKEN_EXPIRE' /app/auth.py"

# Step 3：把改后的文件 cp 到宿主，作为挂载源
docker cp mem0-server:/app/auth.py ./auth.py.host

# Step 4：在 docker-compose.yml 的 mem0-server 段 environment 之后、networks 之前插入 volumes
sed -i '/^      - EMBEDDER_BASE_URL=https:\/\/api.siliconflow.cn\/v1$/a\
    volumes:\
      - ./auth.py.host:/app/auth.py:ro  # 永久化 JWT 过期时间' \
  docker-compose.yml

# Step 5：重建容器让挂载生效
docker compose up -d mem0-server
sleep 10
docker exec mem0-server grep -E 'ACCESS_TOKEN_EXPIRE|REFRESH_TOKEN_EXPIRE' /app/auth.py
# 应输出 10 年那两行
EOF
```

**`ro` 模式意义**：容器内无法修改此文件，防止 alembic upgrade / 自动化脚本回滚改动。
**重建免疫**：`docker compose up -d` 会从 `docker-compose.yml` 读取 volumes，重新挂载 `auth.py.host`——image 升级 / 容器销毁都丢不了这个改动。

## 验证 access_token 真的 10 年有效

```python
import base64, json, time, datetime, urllib.request

# 登录拿 access_token
login_data = json.dumps({"email":"admin@hermesagent.com","password":"hermes-mem0-2024"}).encode()
req = urllib.request.Request(
    "http://<MEM0_HOST_IP>:8888/auth/login",
    data=login_data, headers={"Content-Type":"application/json"}, method="POST")
jwt = json.loads(urllib.request.urlopen(req, timeout=15).read())["access_token"]

# 解码 JWT 看 exp 字段
p = jwt.split('.')[1] + '=' * (-len(jwt.split('.')[1]) % 4)
payload = json.loads(base64.urlsafe_b64decode(p))
exp_dt = datetime.datetime.utcfromtimestamp(payload["exp"])
days = (payload["exp"] - payload.get("iat", int(time.time()))) / 86400
print(f"exp: {exp_dt}  |  有效期 {days:.0f} 天 ≈ {days/365:.1f} 年")
```

输出：`exp: 2036-07-03 10:21:23  |  有效期 3650 天 ≈ 10.0 年`

## docker-compose `environment:` 段 > `.env` 文件（铁律）

本次排查发现：`.env` 里写 `JWT_SECRET=<JWT_SIGNING_SECRET_REDACTED>_SiRGhKSRK1QgvQYXRiEp8yTWctSvco7`，但 `docker exec mem0-server env | grep JWT_SECRET` 返回的是 `mem0-default-jwt-secret-change-me-in-production`（默认值）——**因为 compose 的 `environment:` 段显式写死了那个默认值**，优先级高于 `.env`。

修复：在 `/opt/1panel/docker/compose/mem0-official/docker-compose.yml` 的 `mem0-server` 服务 environment 段把 `JWT_SECRET=...` 改成固定值，**不能只改 .env**。

同样适用 `AUTH_DISABLED` 等其它敏感变量——遇到"改了 .env 但容器值没变"，先 `grep -E "^[[:space:]]*-[[:space:]]*VAR_NAME" docker-compose.yml` 看是不是 compose 段写死了。

## 与 `mem0-dual-provider-config-and-jwt-401-2026-07-04.md` 的关系

那篇覆盖的是：双提供商持久化、JWT 401 诊断顺序、setup-status 探针、`ADMIN_API_KEY` vs JWT 两个 auth surface。
本篇覆盖：JWT 过期永久化（用户明示要求）+ users 表真实位置修正 + 密码重置配方 + compose 优先级铁律。
两者结合是 mem0-server 运维的完整凭证栈指南。
