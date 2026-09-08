# 后端学习计划（LLM_TokenHub）

> 面向：有 3 个月 Python 基础、了解 FastAPI 语法的开发者。
> 目标：系统掌握本项目后端，能独立排查问题、开发新功能、安全地升级迭代。
> 预计投入：每周 8–12 小时，约 10–12 周完成。
> 2026-07 更新：项目新增「知识库（RAG 问答）」专项（见 `docs/prd/knowledge-base.md`），本计划相应增加阶段 8，并调整实战选题。
> 2026-08 更新：项目新增「赠送余额 / 双资金池」专项（见 `docs/prd/wallet.md#赠送余额gift-credit`），它**改造了钱包与计费主链路**，本计划相应增加阶段 6，原阶段 6/7/8 顺延为 7/8/9。
> 2026-08 更新：管理后台从「`.env` 单一账号」升级为独立的管理员表 + 角色权限码（RBAC），与前台 `users` / `roles` 完全隔离。阶段 2 补表、阶段 3 补鉴权链路。

---

## 使用方法

- 每个阶段都有「读什么、做什么、过关标准」三部分，**必须完成过关标准才进入下一阶段**。
- 阶段 1–9 另有**实施细则**（怎么读、怎么做、怎么验证），见 [`docs/learning/`](./learning/README.md)。本文件回答「读什么」，细则回答「怎么读」。
- 学习顺序刻意安排为：先跑通 → 再由外到内沿一条请求链路深挖 → 最后动手改。
- 所有动手练习都在新的 git 分支上做（`git checkout -b learn/xxx`），做完可以丢弃，不污染主分支。

### 贯穿全程的三个习惯

1. **先读测试再读实现**。`backend/tests/` 里每个测试文件就是对应模块的"使用说明书"，比直接读 service 代码好懂得多。
2. **用调试器跟踪，不要只靠眼睛读**。在 VS Code/Cursor 里给关键函数打断点，发一个真实请求，单步看数据怎么流动。一次断点调试胜过十遍静读。
3. **把 AI 当老师而不是代写**。读不懂的代码，让 AI 解释"为什么这么写、不这么写会怎样"；自己写完练习后让 AI 做 code review。不要让 AI 直接替你写练习。

---

## 阶段 0：把项目跑起来，建立全局地图（约 1 周）

**目标**：本地能启动服务、能跑测试、能用 curl 完成一次真实调用；脑中有模块地图。

### 读

`docs/MVP_SCOPE.md` —— 理解这个项目在做什么生意（鉴权 → 路由 → 转发 → 计费）
`docs/PHASE2_SCOPE.md` —— 理解二期加了什么（组织钱包、支付、报表、限流）
`docs/prd/knowledge-base.md` —— 粗读前两节，知道知识库专项在做什么（上传 → 解析分块 → 向量化 → 检索问答），细节留到阶段 8
`docs/GIFT_CALIBER_CHANGE.md` 第 1 节 —— 一张表看懂钱包从「一个余额」变成「现金池 + 赠送池」，以及「收入」的定义变了，细节留到阶段 6
`docs/API_CONTRACT.md` —— 粗读，知道有哪些接口，不用记细节
`docs/DEPLOYMENT.md` + `backend/.env` —— 理解启动需要哪些依赖（PostgreSQL、Redis）

### 做

启动依赖（Postgres/Redis），执行 `alembic upgrade head`，用 `uvicorn app.main:app --reload` 启动
访问 `/health`、`/docs`（Swagger UI），把所有路由分组浏览一遍
跑通全部测试：`pytest`，记录耗时和数量，确认环境无误
按 `docs/MVP_SCOPE.md` 第 5 节的验收口径，用 curl 从零配置出一个可调用模型并完成一次 `/v1/chat/completions` 调用（管理 API 建 Provider → Channel → 模型 → 计费规则 → 给用户充值 → 建 API Key → 调用）

### 过关标准

能画出一张模块地图（手画即可）：`app/api`（三类入口：v1 网关 / admin 后台 / 用户端）、`app/services`、`app/adapters`、`app/db/models`、`app/core`、`app/tasks`、`app/kb`（解析分块引擎）、`app/storage`（文件存储）各自负责什么
能用一句话回答："一个 chat 请求进来，钱是什么时候扣的？"（此时答不准确没关系，写下你的猜想，阶段 4 验证）

---

## 阶段 1：框架基座——应用是怎么组装起来的（约 1 周）

> 实施细则：[`docs/learning/stage-1-framework.md`](./learning/stage-1-framework.md)

**目标**：搞懂 FastAPI 应用的骨架：配置、生命周期、中间件、异常处理、数据库会话。这些是所有后续代码的地基。

### 读（按顺序）

`app/main.py` —— `create_app()`、lifespan、路由注册顺序
`app/core/config.py` —— pydantic-settings 怎么从 `.env` 读配置
`app/middleware/request_id.py` —— 中间件原理，request_id 怎么贯穿一次请求 `app/core/errors.py` —— 全局异常处理器，OpenAI 兼容错误结构怎么统一生成
`app/core/logging.py` —— structlog 结构化日志
- [ ] `app/db/session.py` —— 异步 engine / session 的创建，`get_db` 依赖注入
- [ ] `app/api/deps.py` —— FastAPI `Depends` 的项目级用法

### 需要补的概念（读不懂时再补，不要提前全学）

- `async/await` 与事件循环：为什么所有接口和 DB 操作都是 async
- FastAPI 依赖注入（`Depends`）：官方文档 Dependencies 一章
- Python 上下文管理器（`@asynccontextmanager`）

### 做

- [ ] 断点跟踪：在 `RequestIdMiddleware` 和某个接口函数打断点，发一个请求，观察执行顺序
- [ ] 练习：新增一个 `GET /health/detail` 接口，返回数据库和 Redis 的连通状态，并仿照现有风格写一个测试

### 过关标准

- [ ] 能解释：一个 HTTP 请求从进入到返回，依次经过哪些层（中间件 → 路由 → 依赖 → 处理函数 → 异常处理器）
- [ ] 能解释：`settings` 是什么时候被实例化的，改 `.env` 后为什么要重启

---

## 阶段 2：数据层——模型与迁移（约 1 周）

> 实施细则：[`docs/learning/stage-2-data-layer.md`](./learning/stage-2-data-layer.md)

**目标**：看懂所有表结构和关系，掌握 Alembic 迁移，这是"升级迭代能力"的核心前提。

### 读

- [ ] `docs/DATABASE_DESIGN.md` —— 对照着读下面的 model 文件
- [ ] `app/db/models/user.py`、`api_key.py`、`wallet.py`（含 `GiftGrant` 赠金批次表）、`channel.py` —— 核心四类
- [ ] `app/db/models/org.py`、`payment.py`、`log.py`、`chat.py`、`usage_daily.py`、`channel_cost.py`、`admin.py` —— 粗读（`admin.py` 是后台账号，和前台 `users` / `roles` 不是同一套）
- [ ] `backend/alembic/versions/` 下按编号 001→014 顺序读一遍迁移，体会表结构如何演进（009 知识库表与 pgvector、013 赠送余额、014 后台 RBAC 先扫一眼即可，分别留到阶段 8、阶段 6、阶段 3 再细看）

### 需要补的概念

- SQLAlchemy 2.0 声明式模型、`relationship`、异步查询（`select(...)` + `session.execute`）
- 数据库事务：commit / rollback / 隔离级别的直觉理解
- 为什么金额用整数最小单位存储，而不是 float

### 做

- [ ] 画 ER 图：手画 user、api_key、wallet、org、channel、model、计费规则之间的关系
- [ ] 练习（学习分支上做）：给 `api_key` 表加一个 `note` 备注字段——改 model → `alembic revision --autogenerate` → 检查生成的迁移脚本 → `upgrade` → `downgrade` 再回滚，完整走一遍
- [ ] 用 psql 或数据库 GUI 直接看阶段 0 那次调用在 `request_log`、`usage_log`、钱包流水表里留下的记录

### 过关标准

- [ ] 不看文档能说出：一次成功的 chat 调用会写哪几张表
- [ ] 能独立完成"加字段 + 迁移 + 回滚"的完整流程

---

## 阶段 3：认证与权限——请求是怎么被放行的（约 1–1.5 周）

> 实施细则：[`docs/learning/stage-3-auth.md`](./learning/stage-3-auth.md)

**目标**：掌握三套鉴权入口（用户 JWT、网关 API Key、管理端 JWT），以及**两套互不共用的角色**：前台角色管「能调哪些模型」，后台角色管「能进哪些菜单、能做哪些操作」。

### 读（按调用链顺序）

- [ ] `tests/test_auth_permission.py` 和 `tests/test_user_api.py` —— 先看行为
- [ ] `app/core/security.py` —— bcrypt 密码哈希、JWT 签发与校验、API Key 的生成与哈希存储
- [ ] `app/api/auth.py` + `app/services/auth_service.py` —— 注册/登录链路
- [ ] `app/api/deps.py` —— `Bearer sk-xxx` 是怎么被解析成"当前用户 + 当前 Key"的
- [ ] `app/services/permission_service.py` —— 前台角色-模型授权矩阵怎么生效（对应 `/v1/models` 只返回有权限的模型）
- [ ] `tests/test_admin_rbac_api.py` + `tests/test_admin_permissions.py` —— 先看后台 RBAC 的行为契约
- [ ] `app/db/models/admin.py` + `app/services/admin_rbac_service.py` + `app/api/admin/auth.py` + `app/api/admin/deps.py` —— 管理员从哪来（库表，或 `.env` 兜底超管）
- [ ] `app/core/admin_permissions.py` + `app/api/admin/accounts.py` —— 权限码怎么从 URL 推导、菜单怎么过滤、提权怎么挡住

### 需要补的概念

- JWT 的结构（header.payload.signature）与无状态会话
- 为什么 API Key 只存哈希不存明文；为什么用 bcrypt 而不是 md5
- RBAC：资源 × 操作 = 权限码；fail-closed（漏配拒绝，而不是放行）

### 做

- [ ] 断点跟踪一次带 `Authorization: Bearer sk-xxx` 的请求，看 Key 如何被查库验证
- [ ] 练习：新增一个 `GET /me/summary` 接口，要求登录态，返回当前用户的 Key 数量和钱包余额，附测试

### 过关标准

- [ ] 能解释 JWT 鉴权和 API Key 鉴权分别用在哪些路由、为什么要分两套
- [ ] 能解释一个用户"看不到某个模型"时，代码在哪一层拦住了
- [ ] 能说出前台 `roles` 和后台 `admin_roles` 为什么不能共用一张表
- [ ] 能解释：一个只有 `channel:view` 的管理员去 `POST /admin/channels`，是哪一层返回 403 的

---

## 阶段 4：核心网关链路——本项目的心脏（约 2 周，最重要）

> 实施细则：[`docs/learning/stage-4-gateway.md`](./learning/stage-4-gateway.md)

**目标**：把 `/v1/chat/completions` 从进到出的完整链路刻进脑子里：鉴权 → 限流 → 路由选渠道 → 预扣费 → 适配器转发 → 流式响应 → 实扣/释放 → 记录用量。

### 读（严格按此顺序）

- [ ] `tests/test_openai_api.py` —— 先从测试理解预期行为（含失败不扣费、余额不足 402 等场景）
- [ ] `app/api/v1/chat.py` —— 入口编排，重点看非流式和流式两条分支
- [ ] `app/services/routing_service.py` + `tests/test_routing_adapter.py` —— 优先级+权重怎么选渠道、失败怎么重试
- [ ] `app/adapters/base.py` → `openai_compatible.py` → `anthropic.py` → `registry.py` —— 适配器模式：怎么把不同厂商协议抹平
- [ ] `app/services/billing_service.py` —— hold（预扣）/ capture（实扣）/ release（释放）三段式计费
- [ ] `app/services/usage_service.py` —— 用量与日志落库
- [ ] `app/api/v1/models.py`、`app/api/v1/embeddings.py` —— 对照 chat 看简化版链路
- [ ] `app/services/rate_limit_service.py` + `tests/test_rate_limit.py` —— Redis 限流

### 需要补的概念

- httpx 异步客户端、流式请求
- SSE（Server-Sent Events）：`text/event-stream` 格式、FastAPI 的 `StreamingResponse` / 异步生成器
- 适配器（Adapter）设计模式
- "预扣-实扣-释放"这类资金安全模式（类似支付领域的 authorization/capture）

### 做

- [ ] **画时序图**（本阶段最重要的产出）：一次流式 chat 请求的完整时序，包含每一次数据库写入和上游 HTTP 调用发生的时机
- [ ] 断点跟踪一次非流式调用 + 一次流式调用，验证你画的图
- [ ] 故障演练：把渠道的上游地址改成一个不存在的地址，发起调用，观察：返回什么错误？重试了吗？预扣的钱释放了吗？在代码里找到对应逻辑
- [ ] 故障演练：把钱包余额清零再调用，确认 402 且不请求上游

### 过关标准

- [ ] 能不看代码复述完整链路，说清每一步失败时系统如何兜底
- [ ] 能回答：流式响应中途断开，用户会被扣多少钱？代码依据在哪？
- [ ] 能回答：同一个 `request_id` 重复结算为什么不会重复扣费（幂等在哪实现）？

---

## 阶段 5：钱包、计费与支付——资金安全（约 1.5 周）

> 实施细则：[`docs/learning/stage-5-wallet-payment.md`](./learning/stage-5-wallet-payment.md)

**目标**：掌握钱包体系（个人/组织）、计费主体、支付对接和对账任务。这是业务上最不能出错的部分。

> 读之前先知道一件事：`wallet.balance` 现在只是**现金池**，还有一个赠送池（`gift_balance`）。本阶段先只看现金这一条线（把 `gift_*` 字段当成恒为 0），双池分摊留到阶段 6 专门学，否则两件事混在一起会很难读懂。

### 读

- [ ] `tests/test_wallet_billing.py`、`tests/test_org_wallet.py`、`tests/test_billing_subject.py` —— 先测试
- [ ] `app/services/wallet_service.py` —— 流水只追加、余额怎么算
- [ ] `app/services/billing_subject_service.py` + `app/services/quota_service.py` —— Key 绑定的计费主体（个人 vs 组织）、成员额度
- [ ] `app/services/org_service.py` + `app/api/orgs.py` —— 组织域
- [ ] `app/payments/base.py` → `mock.py` → `stripe.py` + `app/services/payment_service.py` —— 支付抽象与 Stripe 接入（配合 `docs/PAYMENT_STRIPE_SANDBOX.md`）
- [ ] `app/tasks/reconcile_holds.py`、`app/tasks/reconcile_payments.py` —— 定时对账：为什么需要它们兜底

### 需要补的概念

- 幂等性（idempotency）：为什么支付回调和结算都必须幂等
- append-only 账本的好处（可审计、可重算）
- 数据库层面防并发扣超：行锁 / 条件更新

### 做

- [ ] 手工对账练习：发起几次调用后，用 SQL 验证「流水求和 == 钱包余额」
- [ ] 读懂一个你选的对账任务，写出它处理的异常场景清单（比如：预扣了但服务崩了没结算，会发生什么？）
- [ ] 练习：给钱包加一个 `GET /wallets/me/statement` 月度对账单接口（按月汇总充值/消费），附测试

### 过关标准

- [ ] 能解释一笔钱从"充值"到"被一次调用消耗"的完整流水记录
- [ ] 能说出至少三个本项目为资金安全做的设计（幂等、hold/capture、append-only、对账任务……）

---

## 阶段 6：赠送余额与双资金池——改造资金主链路（约 1.5 周，新增）

> 实施细则：[`docs/learning/stage-6-gift-credit.md`](./learning/stage-6-gift-credit.md)

**目标**：掌握 2026-08 新增的赠送余额专项：钱包拆成「现金池 + 赠送池」，赠金按批次发放、按 FIFO 消耗、会过期、可撤回，扣费顺序变成 赠送 → 现金 → 授信，平台收入口径也跟着变。这是全项目**风险最高的一段代码**（改错就是真实资损），也是学习「如何安全改造一条已上线的资金链路」的最佳样本。

### 读（按调用链顺序）

- [ ] `docs/prd/wallet.md#赠送余额gift-credit` —— 精读，重点是「为什么必须物理隔离」「扣减优先级」「跨池分摊示例」三小节
- [ ] `docs/prd/billing.md` 的「赠送余额抵扣与收入口径」 + `docs/GIFT_CALIBER_CHANGE.md` —— 为什么赠金消费**不算收入**
- [ ] `tests/test_gift_ledger.py`、`tests/test_gift_billing.py` —— 照例先从测试看行为，尤其 `test_gift_billing.py` 里的「等价性」参数化测试
- [ ] `app/db/models/wallet.py` 的 `GiftGrant` + `alembic/versions/013_gift_credit.py` —— 批次表的约束、存量数据怎么回填、downgrade 怎么写
- [ ] `app/services/gift_service.py` —— 批次账本四原语：发放（幂等）、撤回（只退未消耗）、过期、FIFO 核销
- [ ] `app/services/wallet_service.py` 的 `hold` / `capture` / `release` —— **本阶段核心**：跨池拆分怎么做到「不改方法签名、5 个调用方零改动」
- [ ] `app/api/admin/wallets.py` + `app/api/wallets.py` —— 后台赠送/撤回接口的权限（`gift` 是独立权限资源，有钱包写权限不能顺带赠送）、风控上限、幂等键与审计
- [ ] `app/services/usage_service.py` + `report_service.py` —— `gift_charge_amount` / `cash_charge_amount` 拆分怎么写进用量与报表
- [ ] `app/tasks/expire_gifts.py` + `reconcile_gifts.py` —— 过期回收（为什么 `gift_held > 0` 要跳过整个钱包）与只告警不修数的对账

### 需要补的概念

- 双资金池记账：为什么赠金不能只加一条备注流水（退款上限、收入口径、过期能力三者会同时崩）
- FIFO 批次核销：为什么按 `expires_at` 升序花、永久有效的排最后
- 整数拆分 vs 比例分摊：为什么跨池只用整数加减，不做按比例取整
- 账本不变式与对账：`Σ(active 批次 remaining) == gift_balance + gift_held` 为什么是「资金正确性」的总开关
- 会计视角：预付款是负债、赠金是营销费用——同一笔钱在报表里为什么待遇不同

### 做

- [ ] **手算再验证**（本阶段最重要的产出）：对着 PRD 示例（赠送池 30、现金池 1000，预扣 100、实扣 60）先在纸上算出两池终态，再用 `pytest tests/test_gift_billing.py -v` 对答案
- [ ] 断点跟踪一次混合支付调用：从 `hold` 拆分 → 上游调用 → `capture` 退差额 → `gift_service.consume` 核销批次，记录每一步两池的 4 个金额字段
- [ ] 用 SQL 验证不变式：`Σ(active 批次 remaining)` 是否等于 `gift_balance + gift_held`；`Σ usage_logs.gift_charge_amount` 是否等于 capture 流水的 `Σ(-gift_amount)`
- [ ] 全链路手工演练：后台赠送 100（有效期 30 天）→ 发起调用 → 看用量拆分 → 赠金花完后自动转现金 → 后台撤回剩余 → 跑 `python -m app.tasks.expire_gifts --dry-run`
- [ ] 故障演练：现金余额为 0、赠送余额充足时发起调用，确认**不返回 402**（这是 `available` 口径变更的直接体现）
- [ ] 逆向思考题：如果当初把 `capture` 的差额退还写成「先退赠送再退现金」，会发生什么？在测试里手工改一下断言，看哪个用例先红

### 过关标准

- [ ] 能不看文档说清：一次调用的钱从哪个池扣、按什么顺序、失败时怎么原路退回
- [ ] 能解释「按占用逆序退差额」为什么等价于「用实扣金额重新按优先级分摊」——这是整个专项最容易写错的一点
- [ ] 能回答：用户钱包里有 500 赠金 + 100 现金，来申请退款，最多能退多少？依据在哪
- [ ] 能回答：一笔纯赠金消费，报表里的毛利为什么是负数，这是 bug 还是预期
- [ ] 能说出赠金到期回收为什么要跳过 `gift_held > 0` 的钱包，以及这个简化的代价是什么

---

## 阶段 7：周边能力——可观测性、报表、后台任务（约 1 周)

> 实施细则：[`docs/learning/stage-7-observability.md`](./learning/stage-7-observability.md)

**目标**：掌握让系统"可运维"的部分，这是从"能写功能"到"能维护系统"的分界线。

### 读

- [ ] `app/core/metrics.py` + `tests/test_observability.py` —— Prometheus 指标
- [ ] `app/core/logging.py` 回顾 —— 结构化日志 + request_id 串联排障
- [ ] `app/services/usage_daily_service.py` + `app/tasks/aggregate_usage_daily.py` —— 为什么要做日聚合表而不是每次实时算
- [ ] `app/services/report_service.py` + `app/api/admin/reports.py` —— 报表（注意收入/毛利用的是**现金口径**，见阶段 6）
- [ ] `tests/conftest.py` —— 精读！理解测试的数据库 fixture、依赖覆盖是怎么搭的，这是你以后写测试的模板

### 做

- [ ] 访问 `/metrics`，找到并解释 3 个指标的含义
- [ ] 制造一个报错，然后只靠日志里的 request_id 把这次请求的完整日志链找出来
- [ ] 练习：新增一个自定义指标（如按模型统计的 token 消耗 counter），在 `/metrics` 中看到它

### 过关标准

- [ ] 给你一个"用户反馈某次调用扣费异常"的场景，你能说出排查步骤（request_id → request_log → usage_log（含赠金/现金拆分）→ 钱包流水 → 上游返回）

---

## 阶段 8：知识库与 RAG——文件流水线 + 向量检索（约 2 周）

> 实施细则：[`docs/learning/stage-8-knowledge-base.md`](./learning/stage-8-knowledge-base.md)

**目标**：掌握 2026-07 新增的知识库专项：文件上传 → 解析 → 分块 → 异步流水线 → 向量化（复用网关计费）→ 检索问答。这条链路把前面学的适配器、计费、后台任务全部串了起来，是很好的综合复习。

> 进度提示：知识库专项已全部落地，含检索/命中测试（`retrieval_service.py`）、分块工作台（`tests/test_kb_chunks.py`）与 Chat RAG 编排（`rag_chat_service.py`、`api/chat_rag.py`），学完后可一并读这三块。

### 读（按调用链顺序）

- [ ] `docs/prd/knowledge-base.md` —— 精读功能设计、文档处理状态机、数据库概览三节
- [ ] `docs/AI_TASK_BREAKDOWN_KB.md` —— 看任务怎么拆、为什么"向量化计费必须独立成任务"（第 16 节）
- [ ] `tests/test_kb_api.py`、`tests/test_kb_parse_chunk.py`、`tests/test_kb_ingest.py`、`tests/test_kb_embed.py` —— 照例先从测试看行为
- [ ] `app/db/models/kb.py` + `alembic/versions/009_kb_tables_pgvector.py` —— 四张新表、pgvector 向量列与 HNSW 索引
- [ ] `app/storage/base.py` → `local.py` —— 文件存储抽象（为什么路径用随机标识、接口要兼容 S3）
- [ ] `app/services/kb_service.py` + `kb_file_validation.py` + `app/api/kb.py` —— 知识库/文档 API、归属隔离、配额与真实类型嗅探
- [ ] `app/kb/`（`parsers.py`、`chunking.py`、`types.py`、`errors.py`）—— 6 类文件解析与"结构优先 + 长度约束"分块，纯逻辑层
- [ ] `app/services/kb_ingest_service.py` + `app/tasks/kb_worker.py` —— 异步流水线：job 消费（`FOR UPDATE SKIP LOCKED`）、状态机流转、幂等与重试
- [ ] `app/services/embedding_gateway.py` —— 内部调用方身份怎么复用路由/适配器/hold-capture-release 链路（对照阶段 4、5、6 学的内容：向量化同样会先花赠金）

### 需要补的概念

- RAG 基本原理：为什么"检索 + 注入上下文"能让模型回答私有知识
- embedding 与向量相似度（cosine）；pgvector 的向量列与 HNSW 索引
- 后台 worker 消费队列的经典姿势：`SELECT ... FOR UPDATE SKIP LOCKED`、幂等键、有限重试
- 文件真实类型嗅探（为什么不能只看扩展名）

### 做

- [ ] 跑 `python -m app.tasks.kb_worker`，上传一份中文 Markdown 和一份 PDF，观察文档状态机从 `uploaded` 一路走到 `ready`（或 `pending_embedding`），对照 PRD 状态机图记录每次流转
- [ ] 用 SQL 看这次处理留下的记录：`kb_documents`、`kb_chunks`（含向量列）、`kb_ingest_jobs`、`usage_logs`（`source=kb_ingest`）、钱包 hold/capture 流水
- [ ] 故障演练：把钱包余额清零再上传文档，确认文档停在 `insufficient_balance` 且无任何扣费；充值后 retry 能继续
- [ ] 断点跟踪一次向量化 job：从 worker 取到 job 开始，看预扣 → 批量调 embedding → 实扣/释放的完整过程

### 过关标准

- [ ] 能不看文档画出文档处理状态机，说清每个状态由哪段代码流转、失败时停在哪
- [ ] 能解释向量化的钱是怎么扣的：为什么复用网关内部链路而不是直连上游，幂等键怎么保证重复执行不重复扣费
- [ ] 能回答：同一个 job 被两个 worker 实例同时拉到会发生什么？代码靠什么避免重复处理？

---

## 阶段 9：迭代实战——独立交付一个真实功能（约 2 周）

> 实施细则：[`docs/learning/stage-9-delivery.md`](./learning/stage-9-delivery.md)

**目标**：完整走一遍"文档 → 迁移 → service → API → 测试"的交付流程，验证你已具备迭代能力。

从下面选 1–2 个（由易到难），在学习分支上完整实现：

- [ ] **入门**：让 `api_keys.last_used_at` 真的被更新——字段和 schema 都有了，但全项目没有一处在写它（鉴权成功时更新 + **必须做节流**，否则每个请求多一次 DB 写）
- [ ] **入门**：给后台加一个只读资源——在 `ADMIN_RESOURCES` 登记前缀，补一条 `resolve_required_permission` 测试，确认 `test_every_admin_route_resolves_to_a_grantable_permission` 仍绿（漏配 = 只有超管能访问）
- [ ] **入门+**：`GET /keys/{id}/usage-summary`，单个 Key 最近 N 天的请求数 / token / 消费额（纯新增只读接口，一次 SQL 聚合）
- [ ] **进阶**：Key 级别的每日消费限额（涉及计费链路插入检查点 + Redis 或日聚合表）
- [ ] **进阶**：新增一个上游适配器（如 Gemini 原生协议），跑通非流式 + 流式（最能检验阶段 4 的掌握程度）
- [ ] **进阶**：赠金到期提醒（`docs/prd/wallet.md` 明确列为二期）——扫描 3 天内到期的批次，产出站内提醒记录，注意幂等：同一批次同一轮只提醒一次
- [ ] **进阶**：批量赠送（同样是 PRD 二期项）——后台按用户列表一次发放，复用 `gift_service.grant` 的幂等键，部分失败要能报告到具体用户，不能半途留下脏账
- [ ] **综合**：管理后台新增"渠道成本 vs 用户收入"毛利报表接口（涉及 usage_daily、channel_cost 两张聚合表）

交付要求（模拟真实迭代规范，参考 `docs/CODING_CONVENTIONS.md` 和 `docs/MVP_SCOPE.md` 第 6 节的变更控制）：

1. 先写一段简短设计说明（改哪些表、哪些接口、失败场景）
2. 迁移脚本可 upgrade 也可 downgrade
3. 新增代码有对应测试，且 `pytest` 全绿
4. 让 AI 对你的 diff 做 code review，理解并回应每一条意见

### 过关标准

- [ ] 独立完成至少一个功能的全流程交付，全程没有让 AI 代写核心逻辑

---

## 附录 A：知识补给清单（按需查阅，不要提前通读）

| 主题 | 推荐资料 | 对应阶段 |
| --- | --- | --- |
| FastAPI 依赖注入、中间件、生命周期 | FastAPI 官方文档（有中文版） | 1 |
| async/await 原理 | Real Python "Async IO in Python" 或 B 站异步编程课 | 1 |
| SQLAlchemy 2.0 异步用法 | SQLAlchemy 官方 "ORM Quick Start" + "Asyncio" 章节 | 2 |
| Alembic 迁移 | Alembic 官方 tutorial（很短） | 2 |
| JWT | jwt.io 的 introduction | 3 |
| RBAC / fail-closed | 本项目 `app/core/admin_permissions.py` 顶部 docstring（无需外部资料） | 3 |
| SSE 流式响应 | MDN "Server-sent events" | 4 |
| Pydantic v2 | 官方 docs 的 Models / Settings 章节 | 1、4 |
| pytest fixture / pytest-asyncio | pytest 官方 fixture 章节 | 7 |
| Redis 基础 | Redis 官方 "Redis in Action" 前几章即可 | 4 |
| 双资金池 / 赠金记账 | 本项目 `docs/prd/wallet.md` + `docs/GIFT_CALIBER_CHANGE.md`（无需外部资料，项目文档就是最好的教材） | 6 |
| 预付款与营销费用的会计差异 | 任意一篇「预收账款 vs 促销费用」科普即可，够用就行 | 6 |
| RAG 入门（检索增强生成） | 任意一篇 "What is RAG" 综述 + 本项目 `docs/prd/knowledge-base.md` | 8 |
| pgvector 向量检索 | pgvector 官方 README（很短，重点看 cosine 与 HNSW 索引） | 8 |
| 队列消费与 SKIP LOCKED | PostgreSQL 文档 `SELECT ... FOR UPDATE SKIP LOCKED` 相关章节或任一篇 "Postgres as a job queue" 博客 | 8 |

## 附录 B：常用命令速查

```bash
# 启动开发服务（backend 目录下）
uvicorn app.main:app --reload

# 启动知识库处理 worker（阶段 8，backend 目录下）
python -m app.tasks.kb_worker

# 赠金过期回收与账本对账（阶段 6，--dry-run 只看不改）
python -m app.tasks.expire_gifts --dry-run
python -m app.tasks.reconcile_gifts

# 跑全部测试 / 跑单个文件 / 跑单个测试
pytest
pytest tests/test_openai_api.py
pytest tests/test_openai_api.py -k "stream" -v

# 数据库迁移
alembic upgrade head          # 升级到最新
alembic downgrade -1          # 回退一个版本
alembic revision --autogenerate -m "add xxx"   # 自动生成迁移

# 学习练习用分支
git checkout -b learn/stage1-ping
git checkout main             # 练完回主分支
```
