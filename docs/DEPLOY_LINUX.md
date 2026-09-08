# 后端 Linux 部署操作手册

面向运维/交付人员，覆盖三种部署方式：

| 方式 | 适用场景 | 章节 |
|------|----------|------|
| A. 源码部署（venv + systemd） | 已有 PostgreSQL / Redis，需精细控制进程与调优 | [§3](#3-方式-a源码部署venv--systemd) |
| B. Docker Compose（服务器上构建） | 服务器能联网拉基础镜像，一条命令带起全套依赖 | [§4](#4-方式-bdocker-compose服务器上构建) |
| C. 预打包镜像 + Docker 部署 | 生产/内网离线，构建与运行分离，可回滚到指定镜像 tag | [§5](#5-方式-c预打包镜像--docker-部署) |

环境变量的**逐项含义**见 [DEPLOYMENT.md §2](./DEPLOYMENT.md#2-后端环境变量)，本文只给部署动作。

---

## 1. 架构与必须启动的进程

后端是 **FastAPI（ASGI）**，ASGI 对象为 `app.main:app`，默认监听 8000。除 API 外还有**一个必须常驻的后台进程**：

| 进程 | 作用 | 缺失后果 |
|------|------|----------|
| `uvicorn app.main:app` | HTTP API（`/v1`、`/admin`、`/auth`、`/kb` …） | 服务不可用 |
| `python -m app.tasks.kb_worker --loop` | 知识库解析/分块/向量化 | 上传的文档永远停在 `uploaded`，永不 `ready` |
| cron 任务（5 个，见 [§7](#7-定时任务)） | 对账、日聚合、赠金过期 | 资金与用量数据逐渐失真 |

外部依赖：**PostgreSQL 14+（必须装 pgvector 扩展）**、**Redis 6+**（限流依赖，缺失会降级为进程内限流，多 worker 下不准）。

日志走 stdout（structlog JSON），不写文件——由 systemd journal 或 `docker logs` 收集。

需要持久化的只有两处：**数据库**和 **`KB_STORAGE_ROOT`（默认 `backend/data/kb_files`，知识库原文件）**。

---

## 2. 通用前置：数据库、Redis、`.env`

### 2.1 PostgreSQL + pgvector

pgvector 的二进制**必须先装到 PostgreSQL 所在机器**，迁移里的 `CREATE EXTENSION` 才能成功（方式 B/C 用官方 `pgvector/pgvector:pg16` 镜像，已内置，可跳过本节）。

```bash
# Debian / Ubuntu，数字换成实际 PG 大版本
sudo apt update && sudo apt install -y postgresql-16-pgvector

# RHEL / Rocky（PGDG 源）
sudo dnf install -y pgvector_16
```

建库并验证扩展可用：

```bash
sudo -u postgres psql -c "CREATE USER tokenhub WITH PASSWORD '强密码';"
sudo -u postgres psql -c "CREATE DATABASE tokenhub OWNER tokenhub;"
sudo -u postgres psql -d tokenhub -c "SELECT name, default_version FROM pg_available_extensions WHERE name='vector';"
```

最后一条必须返回一行；返回空说明扩展没装好，别继续往下走。

### 2.2 Redis

```bash
sudo apt install -y redis-server
sudo sed -i 's/^# *requirepass .*/requirepass 强密码/' /etc/redis/redis.conf
sudo systemctl enable --now redis-server
redis-cli -a '强密码' ping        # 期望 PONG
```

### 2.3 `backend/.env`

三种方式共用同一份 `.env`。数据库/Redis 既可给完整 URL，也可给分片字段（未设 `DATABASE_URL` / `REDIS_URL` 时才用分片字段拼装）。

```env
# ---- 数据库 / Redis（源码部署填真实地址；Docker 部署由 compose 覆盖为服务名）----
DATABASE_URL=postgresql+asyncpg://tokenhub:强密码@127.0.0.1:5432/tokenhub
REDIS_URL=redis://:强密码@127.0.0.1:6379/0

# ---- 密钥：以下四项生产必须替换，否则等于没有鉴权 ----
API_KEY_PEPPER=<openssl rand -hex 32>
CHANNEL_KEY_ENCRYPTION_KEY=<恰好 32 字节字符串>
JWT_SECRET=<openssl rand -hex 32>
ADMIN_PASSWORD=<强密码>
ADMIN_USERNAME=admin

# ---- 对外地址 ----
CORS_ORIGINS=https://tokenhub.example.com
PAYMENT_PUBLIC_BASE_URL=https://api.tokenhub.example.com

# ---- 业务 ----
BUSINESS_TIMEZONE=Asia/Shanghai
KB_STORAGE_ROOT=./data/kb_files
KB_DEFAULT_EMBEDDING_MODEL=text-embedding-3-small
```

生成密钥：

```bash
openssl rand -hex 32                    # API_KEY_PEPPER / JWT_SECRET
openssl rand -base64 24 | cut -c1-32    # CHANNEL_KEY_ENCRYPTION_KEY（须 32 字节）
```

> `CHANNEL_KEY_ENCRYPTION_KEY` 一旦投产**不可更改**，它用于解密库里已存的上游渠道 Key；改了等于所有渠道 Key 失效。
>
> `.env` 权限收紧：`chmod 600 backend/.env`。仓库里的 `.env` 是本地开发值，**不要直接带到生产**。

---

## 3. 方式 A：源码部署（venv + systemd）

### 3.1 取码与安装依赖

```bash
sudo apt install -y python3.11 python3.11-venv git
sudo useradd -r -m -d /opt/llm-tokenhub -s /usr/sbin/nologin tokenhub

sudo -u tokenhub git clone <仓库地址> /opt/llm-tokenhub
cd /opt/llm-tokenhub/backend

sudo -u tokenhub python3.11 -m venv .venv
sudo -u tokenhub .venv/bin/pip install --upgrade pip
sudo -u tokenhub .venv/bin/pip install .      # 生产不要装 [dev]
```

依赖来自 `pyproject.toml`，要求 **Python ≥ 3.11**。

### 3.2 配置与迁移

```bash
sudo -u tokenhub cp .env.example .env
sudo -u tokenhub vi .env                       # 按 §2.3 填写
sudo chmod 600 .env

sudo -u tokenhub mkdir -p data/kb_files
sudo -u tokenhub .venv/bin/alembic upgrade head
```

`alembic upgrade head` 应推进到 `013_gift_credit`。确认：

```bash
sudo -u tokenhub .venv/bin/alembic current
```

### 3.3 API 服务单元

`/etc/systemd/system/tokenhub-api.service`：

```ini
[Unit]
Description=LLM TokenHub Backend API
After=network-online.target postgresql.service redis-server.service
Wants=network-online.target

[Service]
Type=exec
User=tokenhub
WorkingDirectory=/opt/llm-tokenhub/backend
Environment=PYTHONUNBUFFERED=1
ExecStart=/opt/llm-tokenhub/backend/.venv/bin/uvicorn app.main:app \
    --host 127.0.0.1 --port 8000 --workers 4 \
    --proxy-headers --forwarded-allow-ips=127.0.0.1
Restart=always
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

只监听 `127.0.0.1`，由 Nginx 对外；若不放 Nginx 就改成 `0.0.0.0` 并自行处理 TLS。

### 3.4 kb_worker 服务单元

`/etc/systemd/system/tokenhub-kb-worker.service`：

```ini
[Unit]
Description=LLM TokenHub KB Ingest Worker
After=network-online.target tokenhub-api.service
Wants=network-online.target

[Service]
Type=exec
User=tokenhub
WorkingDirectory=/opt/llm-tokenhub/backend
Environment=PYTHONUNBUFFERED=1
ExecStart=/opt/llm-tokenhub/backend/.venv/bin/python -m app.tasks.kb_worker --loop
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

多实例并行安全（领 job 用 `FOR UPDATE SKIP LOCKED`），积压多时可直接起多个副本。

### 3.5 启动

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now tokenhub-api tokenhub-kb-worker
sudo systemctl status tokenhub-api --no-pager
curl -s http://127.0.0.1:8000/health          # {"status":"ok"}
sudo journalctl -u tokenhub-api -f            # 看日志
```

### 3.6 升级

```bash
cd /opt/llm-tokenhub && sudo -u tokenhub git pull
cd backend
sudo -u tokenhub .venv/bin/pip install .
sudo -u tokenhub .venv/bin/alembic upgrade head
sudo systemctl restart tokenhub-api tokenhub-kb-worker
```

---

## 4. 方式 B：Docker Compose（服务器上构建）

`backend/docker-compose.yml` 一次带起 **postgres(pgvector) + redis + api + kb_worker**，无需在宿主机装 Python / PG / Redis。

### 4.1 安装 Docker

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo systemctl enable --now docker
docker compose version
```

### 4.2 配置

`.env` 除了 §2.3 的应用配置，还要补 compose 用的四项（容器内的 PG/Redis 凭据）。**`DATABASE_URL` / `REDIS_URL` 会被 compose 覆盖成服务名地址，`.env` 里写什么都不影响容器**：

```env
POSTGRES_USER=tokenhub
POSTGRES_PASSWORD=<强密码>
POSTGRES_DB=tokenhub
REDIS_PASSWORD=<强密码>

# 可选
API_WORKERS=4
API_BIND=127.0.0.1:8000     # 只给本机 Nginx；要直接对外则写 0.0.0.0:8000
```

### 4.3 启动

```bash
cd /opt/llm-tokenhub/backend
mkdir -p data/kb_files
docker compose up -d --build
```

`api` 容器启动时会自己执行 `alembic upgrade head`，然后拉起 uvicorn；`kb_worker` 等 `api` 健康检查通过后才启动，因此不会并发迁移。

### 4.4 验证

```bash
docker compose ps                     # api 应为 healthy
curl -s http://127.0.0.1:8000/health
docker compose logs -f api kb_worker
```

### 4.5 常用运维

```bash
docker compose restart api                       # 重启
docker compose down                              # 停止（保留数据卷）
docker compose up -d --build                     # 改码后重建
docker compose run --rm api alembic current      # 查迁移版本
docker compose exec postgres psql -U tokenhub -d tokenhub   # 进库
```

数据在 `pgdata` / `redisdata` 命名卷和宿主 `./data/kb_files` 里，`down` 不会删；**只有 `down -v` 会删库**。

---

## 5. 方式 C：预打包镜像 + Docker 部署

构建与运行分离：在开发机 / CI 打好镜像，再把镜像送到生产机。适合生产机不装编译链、不能联网 pip 的场景，且可按 tag 精确回滚。

### 5.1 构建镜像（开发机 / CI）

```bash
cd backend
docker build -t llm-tokenhub-backend:1.0.0 .
docker tag llm-tokenhub-backend:1.0.0 llm-tokenhub-backend:latest
```

用 git 短 hash 做 tag 更利于追溯：

```bash
docker build -t llm-tokenhub-backend:$(git rev-parse --short HEAD) .
```

镜像内容：Python 3.11-slim + 项目依赖 + `app`/`alembic`，工作目录 `/app`，以非 root 用户 `appuser` 运行，默认命令即 uvicorn。API 与 kb_worker **共用这一个镜像**，只是启动命令不同。

### 5.2 分发镜像（二选一）

**私有 registry（推荐，有网）**

```bash
# 开发机
docker tag llm-tokenhub-backend:1.0.0 registry.example.com/llm-tokenhub-backend:1.0.0
docker push registry.example.com/llm-tokenhub-backend:1.0.0

# 生产机
docker login registry.example.com
docker pull registry.example.com/llm-tokenhub-backend:1.0.0
```

**离线 tar 包（内网无 registry）**

```bash
# 开发机：导出并压缩
docker save llm-tokenhub-backend:1.0.0 | gzip > tokenhub-backend-1.0.0.tar.gz
scp tokenhub-backend-1.0.0.tar.gz ops@生产机:/tmp/

# 生产机：导入
gunzip -c /tmp/tokenhub-backend-1.0.0.tar.gz | docker load
docker images | grep llm-tokenhub-backend
```

生产机若连不了 Docker Hub，基础镜像也要一并离线带过去：

```bash
docker pull pgvector/pgvector:pg16 && docker pull redis:7-alpine
docker save pgvector/pgvector:pg16 redis:7-alpine | gzip > deps-images.tar.gz
```

### 5.3 生产机部署

只需 `docker-compose.yml` + `.env`（不需要源码），把 `BACKEND_IMAGE` 指向已导入的镜像，并**注释掉 compose 里 `api` / `kb_worker` 的 `build: .`**（否则 compose 仍会尝试构建）：

```bash
mkdir -p /opt/llm-tokenhub/backend/data/kb_files
cd /opt/llm-tokenhub/backend
# 放入 docker-compose.yml 与 .env
echo 'BACKEND_IMAGE=llm-tokenhub-backend:1.0.0' >> .env
docker compose up -d
```

`.env` 内容同 [§4.2](#42-配置)。

### 5.4 升级与回滚

```bash
# 升级：换 tag 重建容器
sed -i 's/^BACKEND_IMAGE=.*/BACKEND_IMAGE=llm-tokenhub-backend:1.1.0/' .env
docker compose up -d

# 回滚：改回旧 tag（旧镜像还在本机）
sed -i 's/^BACKEND_IMAGE=.*/BACKEND_IMAGE=llm-tokenhub-backend:1.0.0/' .env
docker compose up -d
```

> **数据库迁移不可逆**：Alembic 只保证向前。回滚镜像前先确认新版没引入不兼容的表结构变更；跨版本回滚要连数据库一起回到备份点。

---

## 6. Nginx 反向代理（三种方式通用）

后端只监听本机，Nginx 负责 TLS 与前端静态托管。

```nginx
server {
    listen 443 ssl http2;
    server_name api.tokenhub.example.com;

    ssl_certificate     /etc/letsencrypt/live/api.tokenhub.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/api.tokenhub.example.com/privkey.pem;

    client_max_body_size 50m;      # 知识库文件上传，需 ≥ KB_DEFAULT_MAX_FILE_SIZE_BYTES

    location ~ ^/(v1|admin|auth|keys|wallets|usage|orgs|payments|chat|kb|health|metrics) {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # 流式对话（SSE）必须关缓冲，否则 token 会被攒住一次性吐出
        proxy_buffering off;
        proxy_read_timeout 300s;
    }
}
```

`/metrics` 无鉴权，公网暴露时请在 Nginx 上加 IP 白名单或 basic auth。

前端（`frontend/dist`）由另一个 server 块或同域 `location /` 托管，并把 `CORS_ORIGINS` 设为前端域名。

---

## 7. 定时任务

五个 CLI 任务，无任务队列，用宿主机 cron 调度。频率与语义见 [DEPLOYMENT.md §7](./DEPLOYMENT.md#7-定时任务cron)。

**源码部署（方式 A）**，`crontab -u tokenhub -e`：

```cron
*/10 * * * * cd /opt/llm-tokenhub/backend && .venv/bin/python -m app.tasks.reconcile_holds
*/5  * * * * cd /opt/llm-tokenhub/backend && .venv/bin/python -m app.tasks.reconcile_payments
0    * * * * cd /opt/llm-tokenhub/backend && .venv/bin/python -m app.tasks.expire_gifts
30   0 * * * cd /opt/llm-tokenhub/backend && .venv/bin/python -m app.tasks.aggregate_usage_daily --yesterday
15   2 * * * cd /opt/llm-tokenhub/backend && .venv/bin/python -m app.tasks.reconcile_gifts
```

**Docker 部署（方式 B / C）**，用 `run --rm` 起一次性容器：

```cron
*/10 * * * * cd /opt/llm-tokenhub/backend && docker compose run --rm --no-deps kb_worker python -m app.tasks.reconcile_holds
*/5  * * * * cd /opt/llm-tokenhub/backend && docker compose run --rm --no-deps kb_worker python -m app.tasks.reconcile_payments
0    * * * * cd /opt/llm-tokenhub/backend && docker compose run --rm --no-deps kb_worker python -m app.tasks.expire_gifts
30   0 * * * cd /opt/llm-tokenhub/backend && docker compose run --rm --no-deps kb_worker python -m app.tasks.aggregate_usage_daily --yesterday
15   2 * * * cd /opt/llm-tokenhub/backend && docker compose run --rm --no-deps kb_worker python -m app.tasks.reconcile_gifts
```

`reconcile_gifts` 发现账本不一致时**退出码 1 且只告警不改数**，请把它接进告警，人工核对流水后再动余额。上线后可先跑 `expire_gifts --dry-run` 摸底。

---

## 8. 上线验收清单

```bash
# 1. 健康检查
curl -s http://127.0.0.1:8000/health                    # {"status":"ok"}

# 2. 迁移到位（应为 013_gift_credit）
alembic current                                          # 或 docker compose run --rm api alembic current

# 3. pgvector 已启用（installed_version 不为空）
psql -d tokenhub -c "SELECT extname FROM pg_extension WHERE extname='vector';"

# 4. 管理端登录（默认密码必须已改）
curl -s -X POST http://127.0.0.1:8000/admin/auth/login \
     -H 'Content-Type: application/json' \
     -d '{"username":"admin","password":"你的密码"}'

# 5. kb_worker 在跑
systemctl is-active tokenhub-kb-worker   # 或 docker compose ps kb_worker
```

逐项确认：

- [ ] `.env` 里已无 `change-me-*`，`ADMIN_PASSWORD` 已改，文件权限 600
- [ ] `CORS_ORIGINS` 指向真实前端域名（写错会导致前端所有请求被浏览器拦）
- [ ] `KB_STORAGE_ROOT` 目录存在且进程可写
- [ ] Redis 已连通（否则限流降级为进程内，多 worker 下失准）
- [ ] 生产环境不再暴露 Mock 支付渠道
- [ ] 数据库 + `KB_STORAGE_ROOT` 已纳入备份（删库会级联删原文件，钱包流水恢复不了原文）
- [ ] cron 五个任务已挂上，`reconcile_gifts` 非零退出已接告警

---

## 9. 常见问题

| 现象 | 原因与处理 |
|------|-----------|
| 启动即报 `password authentication failed` / `could not connect` | `DATABASE_URL` 错。注意驱动前缀必须是 `postgresql+asyncpg://`，写成 `postgresql://` 会报驱动错误 |
| `alembic upgrade head` 报 `extension "vector" is not available` | pgvector 二进制没装到 PG 机器上，见 [§2.1](#21-postgresql--pgvector) |
| 前端请求全部 CORS 失败 | `CORS_ORIGINS` 未包含前端实际域名（含协议和端口，逗号分隔）；空值等于不挂 CORS 中间件 |
| 上传的知识库文档一直不 `ready` | `kb_worker` 没起，或余额不足停在 `insufficient_balance`（充值后 `POST /kb/documents/{id}/retry`） |
| 流式对话要等很久才一次性返回 | Nginx 没关 `proxy_buffering` |
| 限流不准 / 多 worker 下 RPM 超限 | 未配置 Redis，退化为进程内计数 |
| 容器里改了 `.env` 不生效 | `.env` 是启动时注入环境变量，必须 `docker compose up -d` 重建容器，`restart` 不重读 |
| 容器写 `data/kb_files` 报 Permission denied | 宿主目录属主与容器内 uid 10001 不匹配：`sudo chown -R 10001 data/kb_files` |
| 重启后限流计数被清 | Redis 未持久化，可接受；compose 里已开 `appendonly` |

---

## 10. 相关文档

- [部署与本地完整环境（环境变量逐项说明）](./DEPLOYMENT.md)
- [README](../README.md)
- [API 契约](./API_CONTRACT.md)
- [数据库设计](./DATABASE_DESIGN.md)
- [Stripe 沙箱](./PAYMENT_STRIPE_SANDBOX.md)
