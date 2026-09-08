# AI 编码任务拆分

| 项目 | 内容 |
| --- | --- |
| 文档名称 | LLM Gateway AI 编码任务拆分 |
| 输入依据 | `docs/PRD.md`（索引）/`docs/prd/`、`docs/TECH_DESIGN.md`、`docs/MVP_SCOPE.md` |
| 版本 | v0.1 |

---

## 1. 使用原则

后续让 AI 编写代码时，必须小批次推进。每次任务只实现一个明确阶段，并要求 AI 同时提供测试或验证方式。

通用要求：

- 每个任务开始前先阅读 `docs/PRD.md` 索引下相关子文档（`docs/prd/`）、`docs/TECH_DESIGN.md`、`docs/MVP_SCOPE.md`。
- 不允许提前实现二期能力，尤其是组织钱包、在线支付、多租户、智能路由。
- 所有金额计算禁止使用 float。
- 钱包流水只追加，不允许覆盖历史流水。
- 平台 API Key 明文不落库。
- 上游 Channel Key 必须加密存储或在代码中预留加密接口，不允许明文展示。
- 每批任务必须有测试、curl 示例或其他可重复验证方式。

---

## 2. 推荐任务顺序

```mermaid
flowchart LR
    T1["任务1: 后端骨架"] --> T2["任务2: 数据模型"]
    T2 --> T3["任务3: 鉴权权限"]
    T3 --> T4["任务4: 管理配置API"]
    T4 --> T5["任务5: 钱包计费"]
    T5 --> T6["任务6: 路由适配器"]
    T6 --> T7["任务7: OpenAI兼容接口"]
    T7 --> T8["任务8: 流式与故障处理"]
    T8 --> T9["任务9: 日志对账"]
    T9 --> T10["任务10: 前端与部署文档"]
```

---

## 3. 任务 1：后端工程骨架

### 目标

创建 FastAPI 后端项目骨架、配置系统、数据库连接、健康检查和测试框架。不实现业务模块。

### 建议提示词

```text
基于 docs/PRD.md、docs/TECH_DESIGN.md、docs/MVP_SCOPE.md，创建 FastAPI 后端工程骨架。
范围只包含配置、应用初始化、健康检查、数据库 session、基础错误结构和 pytest 测试框架。
不要实现用户、钱包、渠道、计费、路由等业务模块。
完成后提供运行测试和启动服务的命令。
```

### 交付物

- `backend/app/main.py`
- `backend/app/core/config.py`
- `backend/app/core/errors.py`
- `backend/app/db/session.py`
- `backend/tests/test_health.py`
- `backend/pyproject.toml` 或等价依赖文件

### 验收标准

- 服务可启动。
- `/health` 返回正常。
- 测试框架可运行。
- 配置来自环境变量。

---

## 4. 任务 2：数据库模型与迁移

### 目标

实现 MVP 所需数据库模型和 Alembic 迁移。

### 建议提示词

```text
基于 docs/TECH_DESIGN.md 的数据模型，实现 MVP 数据库模型和 Alembic 初始化迁移。
只实现个人钱包相关表，不实现组织钱包业务表。
金额字段使用整数，钱包流水只追加。
完成后提供迁移命令和模型测试。
```

### 交付物

- SQLAlchemy models
- Alembic env 与首个 migration
- 基础模型测试

### 关键表

- `users`
- `roles`
- `user_roles`
- `role_model_grants`
- `api_keys`
- `providers`
- `channels`
- `channel_keys`
- `models`
- `model_channel_map`
- `billing_rules`
- `wallets`
- `wallet_transactions`
- `request_logs`
- `usage_logs`
- `audit_logs`

### 验收标准

- 迁移可在空 PostgreSQL 库执行成功。
- 关键唯一约束存在。
- `wallet_transactions(ref_request_id)`、`request_logs(request_id)` 支持幂等。

---

## 5. 任务 3：API Key 鉴权与模型权限

### 目标

实现平台 API Key 的创建、哈希存储、Bearer 鉴权、角色模型授权校验。

### 建议提示词

```text
实现平台 API Key 鉴权和模型权限校验。
API Key 明文仅创建时返回一次，数据库只存 hash 和 prefix。
实现依赖函数，供 /v1/* 接口复用。
实现角色到模型的授权校验。
不要实现钱包扣费和上游调用。
```

### 交付物

- `AuthService`
- `PermissionService`
- API Key 生成与校验逻辑
- 权限校验测试

### 验收标准

- 无效 Key 返回 401。
- 禁用 Key 返回 401。
- 未授权模型返回 403。
- 授权模型通过校验。

---

## 6. 任务 4：管理配置 API

### 目标

实现后台管理 API，让系统可以从零配置出一个可调用模型。

### 建议提示词

```text
实现 MVP 管理 API：Provider、Channel、Channel Key、Model、Model Mapping、Billing Rule、Role 授权。
Channel Key 入库前加密或接入加密接口，列表只返回脱敏值。
所有写操作预留 audit log。
不要实现前端页面。
```

### 交付物

- Provider CRUD
- Channel CRUD
- Channel Key CRUD
- Model CRUD
- Model Mapping CRUD
- Billing Rule 创建与历史查询
- Role 与模型授权 API

### 验收标准

- 能通过 API 创建 Provider、Channel、Key、Model、Mapping、Billing Rule。
- Key 列表不返回明文。
- Model alias 唯一。
- Billing Rule 按版本保存。

---

## 7. 任务 5：钱包与计费服务

### 目标

实现个人钱包充值、预扣、实扣、释放、流水和幂等。

### 建议提示词

```text
实现 MVP 个人钱包和计费服务。
金额使用整数最小单位，禁止 float。
实现 hold、capture、release，并保证 request_id 幂等。
失败请求不计费，释放冻结额度。
提供并发扣费和重复结算测试。
```

### 交付物

- `WalletService`
- `BillingService`
- 后台充值/调账 API
- 钱包流水查询 API
- 计费计算函数

### 验收标准

- 余额不足时 hold 失败。
- hold 成功后 `balance` 减少、`held` 增加。
- capture 后释放多余冻结。
- release 后冻结全额回余额。
- 重复 capture 不重复扣费。
- 并发 hold 不会导致余额为负。

---

## 8. 任务 6：路由与适配器

### 目标

实现模型到渠道的路由选择，以及 OpenAI-compatible 上游适配器。

### 建议提示词

```text
实现 RoutingService 和 OpenAICompatibleAdapter。
路由按模型映射、渠道启用状态、健康状态、优先级和权重选择。
适配器负责构造上游请求、解析非流式响应、解析流式 chunk 和 usage。
使用 Mock 上游测试，不调用真实厂商。
```

### 交付物

- `RoutingService`
- `BaseAdapter`
- `OpenAICompatibleAdapter`
- 上游请求/响应 schema
- Mock 上游测试

### 验收标准

- 禁用渠道不参与路由。
- 高优先级渠道优先。
- 同优先级按权重分布。
- OpenAI-compatible 响应可转换为平台标准响应。

---

## 9. 任务 7：OpenAI 兼容接口

### 目标

实现 `/v1/models` 和 `/v1/chat/completions` 非流式完整链路。

### 建议提示词

```text
实现 OpenAI 兼容 /v1/models 和 /v1/chat/completions 非流式接口。
必须串联鉴权、权限、预扣、路由、适配器、实扣、request log、usage log。
上游失败时释放冻结且不计费。
使用 Mock 上游完成集成测试。
```

### 交付物

- `/v1/models`
- `/v1/chat/completions`
- OpenAI 兼容响应结构
- OpenAI 兼容错误结构
- 集成测试

### 验收标准

- 成功请求产生 request log、usage log、wallet transaction。
- 失败请求不产生计费 usage log。
- 返回结构可被 OpenAI SDK 基本解析。

---

## 10. 任务 8：流式响应与故障处理

### 目标

补齐 `stream=true` 的 SSE 转发、流式中断处理、故障转移和释放冻结。

### 建议提示词

```text
为 /v1/chat/completions 实现 stream=true 的 SSE 流式转发。
支持上游 chunk 透传、[DONE]、usage 收集、客户端中断后的 partial 结算。
实现上游 429/5xx/timeout 的有限重试和失败释放冻结。
提供流式 Mock 测试。
```

### 交付物

- SSE StreamingResponse
- 流式 usage 收集
- partial 请求状态
- retry/failover 逻辑
- 流式测试

### 验收标准

- 客户端能连续收到 SSE chunk。
- 完整流式响应能正常实扣。
- 中断后按已产出内容 partial 计费。
- 上游全部失败释放冻结。

---

## 11. 任务 9：日志、对账与可观测性

### 目标

补齐结构化日志、请求追踪、未结算 hold 对账任务和基础指标。

### 建议提示词

```text
实现 request_id 贯穿日志、请求日志、用量日志、钱包流水查询，以及未结算 hold 的对账任务。
MVP 可先用定时脚本或命令实现对账，不要求完整任务队列。
补充基础 Prometheus 指标或预留 metrics endpoint。
```

### 交付物

- [x] request_id middleware
- [x] 结构化日志
- [x] usage 查询 API
- [x] transactions 查询 API
- [x] hold reconcile 任务
- [x] 基础指标

### 验收标准

- [x] 每个请求日志包含 request_id。
- [x] 超时 hold 可被对账任务释放或补结算。
- [x] 可按模型、时间、用户查询用量。

---

## 12. 任务 10：前端、部署与操作文档

### 目标

在后端核心链路稳定后，再补前端和部署文档。

### 建议提示词

```text
基于已实现的后端 API，补充最小管理后台和用户页面。
后台优先支持渠道、模型、计费规则、钱包充值和用量查询。
前台支持 API Key 管理、钱包查看和简单 Chat。
同时补充本地启动、环境变量、数据库迁移、curl 验证文档。
```

### 交付物

- 管理后台页面（未完成，仍走 `/admin` API）
- [x] 用户 Key/钱包页面（`frontend/`）
- [x] 简单 Chat 页面（SSE、本地会话历史）
- [x] 用户注册登录 JWT（`/auth/*`）
- [x] `README.md`（根目录启动说明）
- 部署文档（待补）
- curl 验证脚本（可参考 `API_CONTRACT.md` §8 与用户 API 测试）

### 验收标准

- 管理员可在 UI 中配置可调用模型。（管理 UI 待办；API 已具备）
- [x] 用户可在 UI 中创建 Key 并查看余额。
- [x] Chat 页面可完成一次流式调用（需配置上游渠道与余额）。
- [x] 新开发者可按 README 启动后端与用户前端。

---

## 13. 每批任务的固定验收模板

后续每次给 AI 一个任务时，要求它最终按以下格式回复：

```text
完成内容：
- ...

修改文件：
- ...

验证命令：
- ...

验证结果：
- ...

未完成/风险：
- ...
```

---

## 14. 不应交给 AI 一次性完成的任务

以下提示词风险较高，不建议使用：

```text
请根据 PRD 直接实现完整大模型聚合平台。
```

```text
一次性实现后端、前端、计费、组织钱包、支付和部署。
```

```text
先把所有表和所有 API 都写出来，测试之后再补。
```

这些任务过大，容易导致代码结构失控、计费事务不可靠、测试缺失和二期能力混入 MVP。
