# LLM TokenHub

大模型聚合平台（LLM Gateway）：OpenAI 兼容调用、个人/组织钱包计费、在线充值、知识库 RAG 问答、用户控制台与运营报表。

## 目录

- `backend/` — FastAPI 后端（含 `kb_worker` 异步 ingest）
- `frontend/` — 用户端与管理后台（Chat / 知识库 / Key / 组织 / 充值 / 报表等）
- `docs/` — PRD、API 契约与部署说明

## 本地启动

完整环境变量、pgvector 与 worker 说明见 [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)。

### 1. 依赖服务

- PostgreSQL + **pgvector**（知识库向量列；扩展须先装到 PG 再跑迁移）
- Redis（限流等；可用 `REDIS_URL` 或分片字段拼连接串）

### 2. 后端 API

```bash
cd backend
python -m venv .venv
# Windows: .venv\Scripts\activate
pip install -e ".[dev]"
# 配置 backend/.env（数据库、Redis、JWT_SECRET、KB_*、支付密钥、BUSINESS_TIMEZONE、GIFT_* 等）
# 确保 KB_STORAGE_ROOT 目录可写（默认 ./data/kb_files）
alembic upgrade head
uvicorn app.main:app --reload --port 8000
```

常用环境变量：`DATABASE_URL`、`REDIS_URL`、`API_KEY_PEPPER`、`JWT_SECRET`、`CORS_ORIGINS`（默认 `http://localhost:8088`）、`KB_STORAGE_ROOT`、`KB_DEFAULT_EMBEDDING_MODEL`、`KB_EMBEDDING_DIM`、`KB_EMBEDDING_MAX_CONCURRENT`、`MOCK_PAYMENT_SECRET`、`STRIPE_SECRET_KEY`、`STRIPE_WEBHOOK_SECRET`、`PAYMENT_PUBLIC_BASE_URL`、`BUSINESS_TIMEZONE`（默认 `Asia/Shanghai`，赠金到期与操作人日累计按此时区）、`GIFT_MAX_AMOUNT_PER_GRANT`（单笔赠送上限，默认 1 万元）、`GIFT_MAX_AMOUNT_PER_OPERATOR_DAILY`（操作人当日累计上限，默认 10 万元）。

### 3. 知识库 Worker（必开，否则文档不会变为 ready）

另开终端，与 API 使用同一虚拟环境：

```bash
cd backend
python -m app.tasks.kb_worker --loop
```

单次调试：`python -m app.tasks.kb_worker --once`。

### 4. 用户前端

```bash
cd frontend
npm install
npm run dev
```

浏览器打开 http://localhost:8088 。开发态通过 Vite 代理访问后端 `localhost:8000`（含 `/orgs`、`/payments`、`/kb`、`/chat`）。

可选：`VITE_MOCK_PAYMENT_SECRET` 与后端 `MOCK_PAYMENT_SECRET` 保持一致（默认均为 `mock-payment-secret`），以便「在线充值」页完成 Mock 沙箱回调。

### 5. 典型用户路径

1. 注册 / 登录（JWT）
2. 在「API Key」创建 Key（可配置计费主体、模型范围、RPM/TPM），并设为 Chat 凭证
3. （可选）「我的组织」建组织 → 邀成员 → 设额度 → 「在线充值」为组织钱包充值
4. 管理员配置渠道、模型（含 embedding）、计费规则（也可手工调账）
5. 「知识库」创建库 → 上传 TXT/MD/PDF/DOCX/HTML/XLSX → 等待 worker 至就绪 → 分块调整 / 命中测试
6. 「对话」挂载知识库流式问答（`/chat/kb-completions`，回答带引用）；或普通 `/v1` Chat
7. 「用量报表」查看按天汇总；向量化来源为 `kb_ingest` / `kb_query`；用量明细可按资金来源（赠金 / 现金 / 混合）筛选
8. 「个人钱包」查看充值余额与赠送余额（赠金不可退不可提，到期作废）

### 6. 管理后台

打开 http://localhost:8088/admin/login

默认固定账号（可在 `backend/.env` 修改）：

- 用户名：`admin`
- 密码：`admin123`

环境变量：`ADMIN_USERNAME`、`ADMIN_PASSWORD`。

管理端页面：组织管理、支付单查询、运营报表（含赠金看板）、知识库全局配置与用户汇总（禁用/启用违规库）、钱包赠送 / 撤回。

生产还需调度两个赠金任务（详见 [DEPLOYMENT.md §7](docs/DEPLOYMENT.md)）：`python -m app.tasks.expire_gifts`（每小时）与 `python -m app.tasks.reconcile_gifts`（每日，不一致退出码 1）。

## 测试

```bash
cd backend
pytest -q
```

知识库相关用例见 `tests/test_kb_*.py`。验收口径与手工清单见 [docs/VALIDATION_PLAN.md](docs/VALIDATION_PLAN.md)。

## 文档

- [部署与环境变量](docs/DEPLOYMENT.md)（含 pgvector、`kb_worker`、`KB_*`、赠金 `BUSINESS_TIMEZONE` / `GIFT_*` 与 cron）
- [验证与测试计划](docs/VALIDATION_PLAN.md)
- [口径变更影响说明](docs/GIFT_CALIBER_CHANGE.md)
- [Stripe 沙箱](docs/PAYMENT_STRIPE_SANDBOX.md)
- [API 契约](docs/API_CONTRACT.md)
- [知识库 PRD](docs/prd/knowledge-base.md)
- [二期范围](docs/PHASE2_SCOPE.md)
- [PRD 索引](docs/PRD.md)
