# 部署与本地完整环境

本文说明如何启动含 **PostgreSQL（含 pgvector）+ Redis + kb_worker + 支付沙箱 + 赠金定时任务** 的完整环境，供新开发者按文档验收二期、知识库与赠送余额能力。

## 1. 前置依赖

| 组件 | 用途 | 建议版本 |
|------|------|----------|
| Python | 后端 | 3.11+ |
| Node.js | 前端 | 20+ |
| PostgreSQL | 主库 | 14+（13 亦可） |
| pgvector | 知识库向量存储（PostgreSQL 扩展） | 0.5+（需 HNSW 支持） |
| Redis | 限流 / 缓存 | 6+ |

### 1.1 安装 pgvector 扩展

知识库功能依赖 [pgvector](https://github.com/pgvector/pgvector)。数据库迁移会自动执行
`CREATE EXTENSION IF NOT EXISTS vector`，但**扩展二进制必须先安装到 PostgreSQL 所在机器**：

| 环境 | 安装方式 |
|------|----------|
| Windows | 下载对应 PG 大版本的 [pgvector 预编译包](https://github.com/pgvector/pgvector/releases)（或 `nmake` 自编译），把 `vector.dll` 放入 PG `lib\`、`vector.control` 与 `sql\*.sql` 放入 `share\extension\`，重启 PG 服务 |
| Debian / Ubuntu | `sudo apt install postgresql-16-pgvector`（按 PG 版本替换数字） |
| macOS (Homebrew) | `brew install pgvector` |
| Docker / CI | 直接使用官方镜像 `pgvector/pgvector:pg16`（内置扩展，推荐 CI 用它替换 `postgres` 镜像） |

验证：`SELECT * FROM pg_available_extensions WHERE name = 'vector';` 能看到版本号即可；
执行 `alembic upgrade head` 后 `installed_version` 不为空。

> HNSW 索引要求 pgvector ≥ 0.5.0。本项目 `kb_chunks.embedding` 列使用固定维度
> `KB_EMBEDDING_DIM`（默认 1536），首期全平台 embedding 模型维度需统一；
> 修改该配置需重建向量列与索引（详见 `DATABASE_DESIGN.md` 7.3）。

## 2. 后端环境变量

在 `backend/.env` 配置（密钥不落库、不入代码）。可用完整 URL，或使用分片字段由应用拼装。

### 2.1 数据库与 Redis

```env
# 方式 A：完整 URL
DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/tokenhub
REDIS_URL=redis://:password@localhost:6379/0

# 方式 B：分片字段（未设 DATABASE_URL / REDIS_URL 时生效）
postgresql_user=...
postgresql_password=...
postgresql_host=localhost
postgresql_port=5432
postgresql_database=tokenhub
redis_user=
redis_password=
redis_host=localhost
redis_port=6379
```

**Redis 必填**：二期限流依赖 Redis；未启动 Redis 时部分调用会失败或降级异常。

### 2.1.1 知识库（可选覆盖）

```env
# kb_chunks 向量列维度（默认 1536）；迁移前确定，修改需重建向量列与 HNSW 索引
KB_EMBEDDING_DIM=1536
# 原文件本地存储根目录（相对路径相对 backend 工作目录）
KB_STORAGE_ROOT=./data/kb_files
# 创建知识库时使用的默认 embedding 模型 alias（须已启用且配置计费规则）
KB_DEFAULT_EMBEDDING_MODEL=text-embedding-3-small
# 角色配额字段为 NULL 时的系统默认（可覆盖）
# KB_DEFAULT_MAX_KB_COUNT=5
# KB_DEFAULT_MAX_DOCS_PER_KB=100
# KB_DEFAULT_MAX_STORAGE_BYTES=1073741824
# KB_DEFAULT_MAX_FILE_SIZE_BYTES=20971520
# KB_UPLOAD_BATCH_LIMIT=10
# 单用户同时处理中的文档数上限（parsing/chunking/embedding）
# KB_MAX_CONCURRENT_DOCS_PER_USER=2
# ingest worker 轮询与批量
# KB_WORKER_POLL_INTERVAL_SECONDS=2.0
# KB_WORKER_BATCH_SIZE=10
# 单 job 最大重试次数
# KB_MAX_RETRY=3
# 向量化每批合并的分块条数（内部 embedding 批量请求）
# KB_EMBEDDING_BATCH_SIZE=16
# 内部向量化上游并发上限（低优先级，避免与在线 chat 争抢渠道 RPM/TPM）
# KB_EMBEDDING_MAX_CONCURRENT=2
```

| 变量 | 说明 |
|------|------|
| `KB_STORAGE_ROOT` | 上传原文件落盘根目录；启动前确保进程可写。删除知识库/文档时同步清理 |
| `KB_DEFAULT_EMBEDDING_MODEL` | 创建知识库时省略 `embedding_model` 的默认 alias；也可用管理端 `/admin/kb/settings` 覆盖 |
| `KB_EMBEDDING_BATCH_SIZE` / `KB_EMBEDDING_MAX_CONCURRENT` | 控制 ingest 向量化吞吐与对上游的压力 |

知识库解析引擎额外依赖（已写入 `backend/pyproject.toml`）：

| 包 | 用途 | 选型理由 |
| --- | --- | --- |
| `pypdf` | PDF 文本层提取 | 轻量、维护活跃；无文本层即可判定扫描件 |
| `python-docx` | DOCX 正文与标题样式 | 官方推荐库，可识别 Heading 1–6 |
| `openpyxl` | XLSX 行列读取 | 只读模式适合解析；不引入完整 Office 栈 |

TXT/MD/HTML/CSV 使用标准库。Token 估算与网关计费预估一致（约 4 字符 ≈ 1 token）。

### 2.2 安全与会话

```env
API_KEY_PEPPER=change-me-in-production
CHANNEL_KEY_ENCRYPTION_KEY=change-me-in-production-32bytes!!
JWT_SECRET=change-me-jwt-secret-in-production
JWT_EXPIRE_MINUTES=1440
CORS_ORIGINS=http://localhost:8088
ADMIN_USERNAME=admin
ADMIN_PASSWORD=admin123
MONEY_SCALE=10000
BUSINESS_TIMEZONE=Asia/Shanghai
# 赠送风控（最小货币单位；默认 1 万元 / 操作人当日 10 万元）
GIFT_MAX_AMOUNT_PER_GRANT=100000000
GIFT_MAX_AMOUNT_PER_OPERATOR_DAILY=1000000000
```

| 变量 | 说明 |
|------|------|
| `BUSINESS_TIMEZONE` | 业务时区。赠金 `expires_in_days` 取该时区到期日 23:59:59 再转 UTC；操作人日累计也按该时区自然日统计 |
| `GIFT_MAX_AMOUNT_PER_GRANT` | 单笔赠送上限（最小货币单位） |
| `GIFT_MAX_AMOUNT_PER_OPERATOR_DAILY` | 单操作人当日累计赠送上限；已撤回批次不计入 |

### 2.3 支付（Mock / Stripe）

```env
# Mock 沙箱（本地默认即可）
MOCK_PAYMENT_SECRET=mock-payment-secret
PAYMENT_ORDER_EXPIRE_MINUTES=30
PAYMENT_RECONCILE_STALE_SECONDS=300
PAYMENT_PUBLIC_BASE_URL=http://localhost:8000

# Stripe Test mode（可选；见 PAYMENT_STRIPE_SANDBOX.md）
STRIPE_SECRET_KEY=sk_test_xxx
STRIPE_WEBHOOK_SECRET=whsec_xxx
```

| 变量 | 说明 |
|------|------|
| `MOCK_PAYMENT_SECRET` | Mock 回调 HMAC 密钥；前端 `VITE_MOCK_PAYMENT_SECRET` 须一致 |
| `STRIPE_SECRET_KEY` | Stripe Test/Live Secret Key |
| `STRIPE_WEBHOOK_SECRET` | Webhook Signing secret |
| `PAYMENT_PUBLIC_BASE_URL` | 对外 API 根，用于拼装 success/cancel 与文档回调地址 |

金额：内部单位为 `MONEY_SCALE`（默认 10000 = 1 元）。Stripe CNY 以「分」计价，创建 Stripe 单时金额须能整除换算到分。

## 3. 启动步骤

```bash
# 1) 确保 PostgreSQL（已装 pgvector）、Redis 已运行

# 2) 后端 API
cd backend
python -m venv .venv
# Windows: .venv\Scripts\activate
pip install -e ".[dev]"
# 配置 .env：DATABASE_URL、Redis、JWT、KB_DEFAULT_EMBEDDING_MODEL、BUSINESS_TIMEZONE、GIFT_* 等
mkdir -p data/kb_files   # Windows PowerShell: New-Item -ItemType Directory -Force data\kb_files
alembic upgrade head
uvicorn app.main:app --reload --port 8000

# 3) 知识库 ingest worker（另开终端，与 API 同 venv）
cd backend
python -m app.tasks.kb_worker --loop
# 调试单次：python -m app.tasks.kb_worker --once

# 4) 前端（再开终端）
cd frontend
npm install
# 可选：echo VITE_MOCK_PAYMENT_SECRET=mock-payment-secret > .env.local
npm run dev
```

- API：http://localhost:8000  
- 控制台：http://localhost:8088  
- 管理端：http://localhost:8088/admin/login  

健康检查：`GET http://localhost:8000/health`

### 3.1 kb_worker 说明

- API 进程**不会**自动跑解析/分块/向量化；上传后文档停留在 `uploaded`/`pending_*`，须由 `kb_worker` 拉取 `kb_ingest_jobs` 推进到 `ready`。
- 多实例并行安全：领取 job 使用 `SELECT ... FOR UPDATE SKIP LOCKED`，不会重复消费。
- 向量化余额不足时文档停在 `insufficient_balance`，**无任何扣费**；充值后对文档 `POST /kb/documents/{id}/retry`。
- 生产建议将 worker 作为独立进程/容器常驻，并监控失败 job 与磁盘占用（`KB_STORAGE_ROOT`）。

管理端可调的全局项（覆盖环境变量默认值）：`GET/PATCH /admin/kb/settings`（默认 embedding 模型、单文件上限、批量上限、embedding 批大小与并发）。

## 4. 支付沙箱验收

### Mock（推荐本地）

1. 用户登录 → 「在线充值」→ 选择个人或组织钱包 → 渠道 **Mock** → 创建支付单。
2. 点击「模拟支付成功」：前端用 HMAC 调 `POST /payments/callbacks/mock` 并入账。
3. 在「钱包」或「我的组织」确认余额与流水。

### Stripe

按 [PAYMENT_STRIPE_SANDBOX.md](./PAYMENT_STRIPE_SANDBOX.md) 配置 Webhook / CLI，渠道选 **Stripe**，用测试卡 `4242…` 完成 Checkout。

掉单对账任务：

```bash
cd backend
python -m app.tasks.reconcile_payments
```

## 5. 知识库验收前置检查

1. pgvector 扩展已安装且迁移成功（`kb_chunks.embedding` 列存在）。
2. 管理端已配置至少一个 **embedding** 模型 + 渠道映射 + 计费规则；`KB_DEFAULT_EMBEDDING_MODEL`（或 admin settings）指向该 alias。
3. 用户个人钱包有足够余额（向量化按 embedding 规则预扣/实扣）。
4. `kb_worker --loop` 在跑；`KB_STORAGE_ROOT` 可写。
5. 按 [VALIDATION_PLAN.md](./VALIDATION_PLAN.md) §3.7 / §5.3 走验收口径。

## 5.1 赠送余额验收前置检查

1. `backend/.env` 已配置 `BUSINESS_TIMEZONE`（默认 `Asia/Shanghai`）、`GIFT_MAX_AMOUNT_PER_GRANT`、`GIFT_MAX_AMOUNT_PER_OPERATOR_DAILY`。
2. 迁移已到 `013_gift_credit`（`alembic upgrade head`）。存量钱包 `gift_balance = gift_held = 0`。
3. 生产 cron 已挂上 `expire_gifts`（每小时）与 `reconcile_gifts`（每日）；发布后可手动补跑一次过期回收。
4. 按 [VALIDATION_PLAN.md](./VALIDATION_PLAN.md) §3.8 / §5.4 走赠送 → 调用拆分 → 撤回 / 过期 → 对账。

`expire_gifts --dry-run` 只报告不写库，适合上线前摸底。`reconcile_gifts` **只告警不修数**，退出码 1 表示账本或用量拆分不一致，须人工核对流水后再改数字。

## 6. 生产注意

- 必须替换所有 `change-me-*` 与默认管理员密码。
- 生产勿依赖 Mock 渠道；关闭或限制 `MOCK_PAYMENT_SECRET` 暴露面。
- 前端构建产物由 Nginx/CDN 托管时，将 `/auth`、`/keys`、`/orgs`、`/payments`、`/chat`、`/kb`、`/admin`、`/v1` 等反代到后端。
- 多 worker 部署时 Redis 与数据库连接池按负载调优；`kb_worker` 副本数按 ingest 积压调节，注意 `KB_EMBEDDING_MAX_CONCURRENT`。
- 定期备份 `KB_STORAGE_ROOT` 与数据库；删库级联删文件，误删不可从钱包流水恢复原文。

## 7. 定时任务（cron）

以下任务均为 CLI，不依赖任务队列。生产用 cron 或 systemd timer 调度；建议在 `backend` 目录、同一 venv 下执行。`--dry-run` 只报告不写库。

| 任务 | 命令 | 建议频率 | 说明 |
|------|------|----------|------|
| Hold 对账 | `python -m app.tasks.reconcile_holds` | 每 10 分钟 | 超时未结算 hold 补 capture 或 release |
| 用量日聚合 | `python -m app.tasks.aggregate_usage_daily --yesterday` | 每日 | 写入 `usage_daily_summary` |
| 支付对账 | `python -m app.tasks.reconcile_payments` | 每 5 分钟 | 掉单补入账（见支付沙箱文档） |
| 赠金过期回收 | `python -m app.tasks.expire_gifts` | **每小时** | 到期批次作废并写 `gift_expire` 流水；`gift_held>0` 的钱包本轮跳过，下一轮再收 |
| 赠金账本对账 | `python -m app.tasks.reconcile_gifts` | **每日** | 校验批次 remaining 与两池字段、用量赠金与 capture 拆分；**只告警不修数** |

```cron
# 未结算 hold 对账
*/10 * * * * cd /path/to/backend && .venv/bin/python -m app.tasks.reconcile_holds

# 用量日聚合（UTC 昨日）
30 0 * * * cd /path/to/backend && .venv/bin/python -m app.tasks.aggregate_usage_daily --yesterday

# 赠金到期回收：每小时
0 * * * * cd /path/to/backend && .venv/bin/python -m app.tasks.expire_gifts

# 赠金账本对账：每日 02:15，不一致时退出码为 1，请接告警
15 2 * * * cd /path/to/backend && .venv/bin/python -m app.tasks.reconcile_gifts
```

赠金过期回收也可在发布后手动补跑一次。对账任务发现不一致时**不要自动改钱包数字**，须人工核对流水后再修。

```bash
# 上线前摸底（不写库）
python -m app.tasks.expire_gifts --dry-run

# 正式回收
python -m app.tasks.expire_gifts

# 账本对账（ok=false 时退出码 1）
python -m app.tasks.reconcile_gifts
```

## 8. 相关文档

- [Linux 部署操作手册（源码 / Docker Compose / 预打包镜像）](./DEPLOY_LINUX.md)
- [README](../README.md)
- [API 契约](./API_CONTRACT.md)
- [验证计划](./VALIDATION_PLAN.md)
- [口径变更影响说明](./GIFT_CALIBER_CHANGE.md)
- [知识库 PRD](./prd/knowledge-base.md)
- [Stripe 沙箱](./PAYMENT_STRIPE_SANDBOX.md)
- [二期范围](./PHASE2_SCOPE.md)
