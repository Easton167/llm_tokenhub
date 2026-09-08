# API 契约设计

| 项目 | 内容 |
| --- | --- |
| 文档名称 | LLM Gateway API 契约设计 |
| 输入依据 | `docs/PRD.md`（索引）/`docs/prd/`、`docs/TECH_DESIGN.md`、`docs/prd/knowledge-base.md`、`docs/prd/wallet.md` |
| 版本 | v0.4（含赠送余额双资金池） |

---

## 1. 设计原则

- 对外模型调用接口优先兼容 OpenAI API，降低 SDK 迁移成本。
- 管理端 API 使用 REST 风格，先服务后台管理和自动化配置。
- 所有错误响应采用 OpenAI 兼容 error 结构。
- 二期支持组织计费主体：API Key 可绑定 `personal` 或 `org:{uuid}`；未设置时走账户默认。
- 所有写接口后续实现时都应支持审计日志。

---

## 2. 鉴权方式

### 2.1 模型调用鉴权

```http
Authorization: Bearer sk-xxxxxxxx
```

平台 API Key：

- 明文仅创建时返回一次。
- 数据库存储 hash、prefix、status、owner user。
- MVP 中 API Key 绑定个人计费主体。

### 2.2 管理端与用户端会话鉴权

管理端与用户控制台使用 JWT 会话：

```http
Authorization: Bearer eyJ...
```

- 用户端：`POST /auth/register`、`POST /auth/login` 签发 access token；用于 `/keys`、`/wallets/*`、`/orgs/*`、`/payments`、`/usage`、`/auth/me`、`/chat/*`、`/kb/*`。
- 管理端：`POST /admin/auth/login` 签发管理员 JWT；用于 `/admin/*`（除 login）。身份来自 `admin_users`（无同名记录时回落 `.env` 超管）；业务接口按权限码放行。
- JWT 不得用于 `/v1/*` 模型调用；模型调用必须使用平台 API Key。
- 知识库 RAG 问答 `POST /chat/kb-completions`：同时需要用户 JWT + 平台 API Key（`X-Api-Key`），JWT 校验会话/知识库归属，API Key 走现有 chat 计费链路。

---

## 3. 对外 OpenAI 兼容 API

### 3.1 查询模型

```http
GET /v1/models
Authorization: Bearer sk-xxxxxxxx
```

响应：

```json
{
  "object": "list",
  "data": [
    {
      "id": "gpt-4o",
      "object": "model",
      "created": 1710000000,
      "owned_by": "llm-gateway"
    }
  ]
}
```

规则：

- 只返回当前 Key 所属用户有角色授权的模型。
- 禁用模型、未配置启用渠道的模型不返回或标记不可用，MVP 建议不返回。

### 3.2 Chat Completions 非流式

```http
POST /v1/chat/completions
Authorization: Bearer sk-xxxxxxxx
Content-Type: application/json
```

请求：

```json
{
  "model": "gpt-4o",
  "messages": [
    {"role": "user", "content": "你好"}
  ],
  "temperature": 0.7,
  "max_tokens": 1024,
  "stream": false
}
```

响应：

```json
{
  "id": "chatcmpl_xxx",
  "object": "chat.completion",
  "created": 1710000000,
  "model": "gpt-4o",
  "choices": [
    {
      "index": 0,
      "message": {"role": "assistant", "content": "你好！"},
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 12,
    "completion_tokens": 8,
    "total_tokens": 20
  }
}
```

### 3.3 Chat Completions 流式

请求：

```json
{
  "model": "gpt-4o",
  "messages": [
    {"role": "user", "content": "你好"}
  ],
  "stream": true
}
```

响应头：

```http
Content-Type: text/event-stream
Cache-Control: no-cache
Connection: keep-alive
```

响应体：

```text
data: {"id":"chatcmpl_xxx","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"你"},"finish_reason":null}]}

data: {"id":"chatcmpl_xxx","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"好"},"finish_reason":null}]}

data: {"id":"chatcmpl_xxx","object":"chat.completion.chunk","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":12,"completion_tokens":8,"total_tokens":20}}

data: [DONE]
```

---

## 4. 错误响应

统一结构：

```json
{
  "error": {
    "message": "Insufficient balance",
    "type": "billing_error",
    "code": "insufficient_balance"
  }
}
```

| HTTP | code | type | 说明 |
| --- | --- | --- | --- |
| 400 | `invalid_request` | `invalid_request_error` | 请求参数错误 |
| 400 | `unsupported_file_type` | `invalid_request_error` | 知识库上传：扩展名/真实类型不支持或不匹配 |
| 400 | `kb_quota_exceeded` | `quota_error` | 知识库数量触达角色配额 |
| 400 | `kb_doc_quota_exceeded` | `quota_error` | 单库文档数触达配额 |
| 400 | `kb_storage_quota_exceeded` | `quota_error` | 存储占用触达配额 |
| 400 | `file_too_large` | `quota_error` | 单文件超过大小上限 |
| 400 | `batch_too_large` | `quota_error` | 单次批量上传超过上限 |
| 400 | `kb_disabled` | `invalid_request_error` | 知识库已被禁用（不可上传/检索） |
| 401 | `invalid_api_key` | `authentication_error` | API Key 无效或禁用 |
| 401 | `unauthorized` | `authentication_error` | JWT 无效或缺失 |
| 403 | `forbidden` | `permission_error` | 无资源访问权（含挂载他人知识库） |
| 403 | `model_not_authorized` | `permission_error` | 无模型权限 |
| 402 | `insufficient_balance` | `billing_error` | 余额不足（`gift_balance + balance + credit_limit`） |
| 404 | `not_found` | `invalid_request_error` | 资源不存在（知识库/文档/分块等） |
| 404 | `model_not_found` | `invalid_request_error` | 模型不存在或不可用 |
| 409 | `conflict` / `idempotency_conflict` | `conflict_error` | 状态冲突 / 幂等冲突 |
| 409 | `gift_already_consumed` | `conflict_error` | 赠送批次已消耗完，无可撤回余额 |
| 422 | `gift_amount_exceeds_limit` | `invalid_request_error` | 赠送金额超过单笔或操作人日累计上限 |
| 429 | `rate_limit_exceeded` | `rate_limit_error` | 触发限流 |
| 502 | `upstream_error` | `upstream_error` | 上游错误 |
| 503 | `no_available_channel` | `service_unavailable_error` | 无可用渠道 |
| 500 | `internal_error` | `server_error` | 系统错误 |

---

## 5. 管理 API

### 5.1 Provider

```http
POST /admin/providers
GET /admin/providers
GET /admin/providers/{provider_id}
PATCH /admin/providers/{provider_id}
DELETE /admin/providers/{provider_id}
```

核心字段：

```json
{
  "name": "OpenAI",
  "adapter_type": "openai_compatible",
  "default_base_url": "https://api.openai.com/v1"
}
```

### 5.2 Channel

```http
POST /admin/channels
GET /admin/channels
GET /admin/channels/{channel_id}
PATCH /admin/channels/{channel_id}
DELETE /admin/channels/{channel_id}
POST /admin/channels/{channel_id}/test
```

核心字段：

```json
{
  "provider_id": "uuid",
  "name": "openai-main",
  "base_url": "https://api.openai.com/v1",
  "adapter_type": "openai_compatible",
  "priority": 100,
  "weight": 10,
  "timeout_ms": 60000,
  "max_retries": 2,
  "status": 0
}
```

创建时 `adapter_type`、`base_url` 可省略：分别继承所属 Provider 的 `adapter_type`、`default_base_url`。若 Provider 无 `default_base_url` 且请求未传 `base_url`，返回 400。

### 5.3 Channel Key

```http
POST /admin/channels/{channel_id}/keys
GET /admin/channels/{channel_id}/keys
PATCH /admin/channels/{channel_id}/keys/{key_id}
DELETE /admin/channels/{channel_id}/keys/{key_id}
```

创建请求：

```json
{
  "api_key": "sk-upstream-secret",
  "weight": 10,
  "rpm_limit": 60,
  "tpm_limit": 100000
}
```

列表响应必须脱敏：

```json
{
  "id": "uuid",
  "key_masked": "****abcd",
  "weight": 10,
  "status": 0
}
```

### 5.4 Model

```http
POST /admin/models
GET /admin/models
GET /admin/models/{model_id}
PATCH /admin/models/{model_id}
DELETE /admin/models/{model_id}
```

核心字段：

```json
{
  "alias": "gpt-4o",
  "display_name": "GPT-4o",
  "model_type": "chat",
  "capabilities": {
    "stream": true,
    "vision": false,
    "max_context_tokens": 128000
  },
  "visibility": "role_restricted",
  "status": 0
}
```

### 5.5 Model Mapping

```http
POST /admin/models/{model_id}/mappings
GET /admin/models/{model_id}/mappings
PATCH /admin/models/{model_id}/mappings/{mapping_id}
DELETE /admin/models/{model_id}/mappings/{mapping_id}
```

请求：

```json
{
  "channel_id": "uuid",
  "upstream_model_name": "gpt-4o-2024-11-20",
  "weight": 10,
  "status": 0
}
```

### 5.6 Billing Rule

```http
POST /admin/models/{model_id}/billing-rules
GET /admin/models/{model_id}/billing-rules
PATCH /admin/models/{model_id}/billing-rules/{rule_id}
DELETE /admin/models/{model_id}/billing-rules/{rule_id}
```

请求（创建）：

```json
{
  "price_input": 180000,
  "price_output": 720000,
  "price_cached_input": 90000,
  "price_per_call": 0,
  "cost_input": 150000,
  "cost_output": 600000,
  "multiplier": "1.000",
  "min_charge_unit": 1,
  "currency": "CNY",
  "effective_at": "2026-06-24T00:00:00.000000Z"
}
```

说明：

- 单价单位为每 1M tokens 对应的最小货币单位。
- 新建规则自动生成新版本。
- 历史版本不可物理删除，只能作废：`DELETE`（或 `PATCH` 将 `status` 置为禁用）为软删除，规则记录保留供对账与审计；`DELETE` 返回 `204`。

### 5.7 Wallet

```http
GET /admin/wallets/users/{user_id}
POST /admin/wallets/users/{user_id}/recharge
POST /admin/wallets/users/{user_id}/adjust
GET /admin/wallets/users/{user_id}/transactions
GET /admin/wallets/orgs/{org_id}
POST /admin/wallets/orgs/{org_id}/recharge
POST /admin/wallets/orgs/{org_id}/adjust
GET /admin/wallets/orgs/{org_id}/transactions
GET /admin/wallets/{wallet_id}/transactions
```

充值请求：

```json
{
  "amount": 1000000,
  "remark": "manual recharge"
}
```

金额为最小货币单位（默认 `MONEY_SCALE=10000`，即 1 元 = 10000）。`recharge` / `adjust` **只动现金池**，不能把钱充进赠送池。

钱包响应（`balance` 语义已收窄为现金池，不再表示全部余额）：

```json
{
  "id": "uuid",
  "owner_type": "personal",
  "owner_id": "uuid",
  "balance": 500000,
  "held": 1710,
  "gift_balance": 30000,
  "gift_held": 0,
  "credit_limit": 0,
  "total_available": 530000,
  "currency": "CNY"
}
```

- `balance` / `held`：现金池可用 / 冻结（可退可提）。
- `gift_balance` / `gift_held`：赠送池可用 / 冻结（不可退不可提）。
- `total_available`：**总可用额** = `gift_balance + balance + credit_limit`（402 判定与前端展示用这个）。
- 流水含 `gift_amount`（其中赠送池部分，符号与 `amount` 一致；纯现金为 0）。

### 5.7.1 赠送余额（`gift` 权限资源）

权限：管理员 JWT，需要 `gift:view` / `gift:manage`（与钱包充值/调账的 `wallet:*` 分权，有钱包写权限不能顺带赠送）。每次赠送 / 撤回写审计 `wallet.gift_grant` / `wallet.gift_revoke`。

```http
POST /admin/wallets/users/{user_id}/gifts
POST /admin/wallets/orgs/{org_id}/gifts
GET  /admin/wallets/{wallet_id}/gifts?status=&limit=&offset=
POST /admin/gifts/{grant_id}/revoke
```

赠送请求：

```json
{
  "amount": 1000000,
  "expires_in_days": 30,
  "reason": "promotion",
  "remark": "618 活动新用户赠送",
  "idempotency_key": "promo-618-user-8f3a"
}
```

| 字段 | 规则 |
| --- | --- |
| `amount` | 最小货币单位整数，必填且 > 0；受 `GIFT_MAX_AMOUNT_PER_GRANT` 单笔上限约束 |
| `expires_in_days` / `expires_at` / `never_expires` | **三选一**。都不传 → 422。`never_expires=true` 才允许永久有效。`expires_in_days` 到期时刻 = 业务时区（`BUSINESS_TIMEZONE`）到期日 23:59:59 再转 UTC |
| `reason` | 必填：`promotion` / `compensation` / `internal` / `partnership` / `other` |
| `idempotency_key` | 必填。同一钱包重复提交返回**已存在批次**（HTTP 200，不重复发放） |

赠送 / 批次响应：

```json
{
  "id": "uuid",
  "wallet_id": "uuid",
  "amount": 1000000,
  "remaining": 1000000,
  "reason": "promotion",
  "expires_at": "2026-09-16T15:59:59.000000Z",
  "status": "active",
  "granted_by": "uuid",
  "revoked_by": null,
  "revoked_at": null,
  "idempotency_key": "promo-618-user-8f3a",
  "remark": "618 活动新用户赠送",
  "created_at": "2026-08-17T01:00:00.000000Z",
  "updated_at": "2026-08-17T01:00:00.000000Z"
}
```

`status`：`active` / `used_up` / `expired` / `revoked`。撤回请求体可选 `{ "remark": "..." }`。

赠送错误码：

| HTTP | code | 场景 |
| --- | --- | --- |
| 200 | — | 发放成功，或同一 `idempotency_key` 返回已有批次 |
| 403 | `forbidden` | 非管理员 |
| 409 | `gift_already_consumed` | 撤回时批次 `remaining = 0` 或已非 `active` |
| 422 | `gift_amount_exceeds_limit` | 超过单笔上限，或操作人当日累计上限（已撤回批次不计入日累计） |
| 422 | `invalid_request` | 未指定到期方式、三选一冲突、金额非法等 |

风控配置见 [DEPLOYMENT.md](./DEPLOYMENT.md) `GIFT_MAX_AMOUNT_PER_GRANT` / `GIFT_MAX_AMOUNT_PER_OPERATOR_DAILY`。日累计按 `BUSINESS_TIMEZONE` 自然日统计。

### 5.8 Usage

```http
GET /admin/request-logs?from=&to=&user_id=&model_id=&status=
GET /admin/usage-logs?from=&to=&user_id=&model_id=&source=&fund_source=
GET /admin/usage-summary?from=&to=&group_by=model
POST /admin/usage/daily-aggregate
```

用量明细含 `gift_charge_amount` / `cash_charge_amount`（之和等于 `charge_amount`）。`fund_source=gift|cash|mixed` 按资金来源筛选。

### 5.9 组织（管理端）

```http
GET /admin/orgs?status=&limit=&offset=
GET /admin/orgs/{org_id}
POST /admin/orgs/{org_id}/deactivate
```

停用请求可选 `{ "reason": "..." }`（写入审计）。

### 5.10 支付单（管理端）

```http
GET /admin/payments?user_id=&org_id=&status=&provider=&limit=&offset=
GET /admin/payments/{order_no}
```

### 5.11 运营报表（管理端）

日期参数 `from_date` / `to_date` 为 **UTC 日历日**（`YYYY-MM-DD`）。

```http
GET /admin/reports/trend?from_date=&to_date=&org_id=&user_id=&model_id=&channel_id=
GET /admin/reports/top?dimension=user|org|model&from_date=&to_date=&limit=10
GET /admin/reports/channel-share?from_date=&to_date=
GET /admin/reports/gifts?from_date=&to_date=
GET /admin/reports/reconcile?from_date=&to_date=
```

`DailyMetric` 字段：`summary_date, request_count, input/output/cached_tokens, revenue_amount, cash_revenue_amount, gift_consumed, gift_charge_amount, cash_charge_amount, gift_cost, cost_amount, gross_profit`。

口径变更（相对二期「收入 = 计费金额」）：

| 字段 | 口径 |
| --- | --- |
| `revenue_amount` | 计费总额 = 赠金抵扣 + 现金支付，**不是**平台收入 |
| `cash_revenue_amount` / `cash_charge_amount` | **平台真实收入**（现金池支付） |
| `gift_consumed` / `gift_charge_amount` | 赠金消耗，记营销费用核销，不计入收入 |
| `gross_profit` | `cash_revenue_amount - cost_amount`（纯赠金消费时为负，属预期） |

约束：`gift_charge_amount + cash_charge_amount = charge_amount`。迁移建议见 [GIFT_CALIBER_CHANGE.md](./GIFT_CALIBER_CHANGE.md)。

`GET /admin/reports/gifts` 看板：`gift_balance_total`（当前敞口）、区间内 `granted_amount` / `revoked_amount` / `expired_amount` / `gift_consumed` / `gift_cost`、累计发放/消耗与 `redemption_rate_bps`（核销率，基点）。

`GET /admin/reports/reconcile` 额外返回 `usage_gift_total` / `capture_gift_total` / `gift_matched` / `alerts`（不一致时含 `gift_split_mismatch` 供标红）。

### 5.12 知识库（管理端）

鉴权：管理员 JWT。路径前缀 `/admin/kb`。

```http
GET /admin/kb/settings
PATCH /admin/kb/settings
GET /admin/kb/user-stats?user_id=
GET /admin/kb?owner_user_id=&status=&limit=&offset=
POST /admin/kb/{kb_id}/disable
POST /admin/kb/{kb_id}/enable
```

全局配置（覆盖值 + 生效值）：

```json
{
  "default_embedding_model": "text-embedding-3-small",
  "max_file_size_bytes": 20971520,
  "upload_batch_limit": 10,
  "embedding_batch_size": 16,
  "embedding_max_concurrent": 2,
  "effective_default_embedding_model": "text-embedding-3-small",
  "effective_max_file_size_bytes": 20971520,
  "effective_upload_batch_limit": 10,
  "effective_embedding_batch_size": 16,
  "effective_embedding_max_concurrent": 2
}
```

- `PATCH` 仅更新传入字段；空 body → `400 invalid_request`。
- 用户汇总：`kb_count` / `doc_count` / `storage_bytes` / `embed_input_tokens`（`source=kb_ingest` 累计）。
- 禁用后用户不可上传、不可检索/命中测试；启用恢复。列表不含文档正文与分块内容。

---

## 6. 用户侧 API

鉴权：除特别说明外，本节接口使用 **用户 JWT**（`Authorization: Bearer <access_token>`）。  
模型调用仍使用平台 API Key，见 §3。

### 6.1 认证

```http
POST /auth/register
POST /auth/login
GET /auth/me
```

注册请求：

```json
{ "email": "user@example.com", "password": "password123", "nickname": "可选" }
```

登录响应：

```json
{ "access_token": "eyJ...", "token_type": "bearer" }
```

### 6.2 Key / 钱包 / 用量 / Chat 会话

```http
POST /keys
GET /keys
PATCH /keys/{key_id}
DELETE /keys/{key_id}
GET /wallets/me
GET /wallets/me/gifts
GET /wallets/me/transactions
GET /usage?from=&to=&model=&source=&fund_source=
GET /usage/daily?from_date=&to_date=&model_id=
GET /chat/sessions
POST /chat/sessions
GET /chat/sessions/{session_id}
PATCH /chat/sessions/{session_id}
DELETE /chat/sessions/{session_id}
PUT /chat/sessions/{session_id}/messages
```

创建 / 更新 Key 请求字段：

```json
{
  "name": "local-dev",
  "billing_subject": "personal",
  "allowed_models": ["gpt-4o"],
  "rpm_limit": 60,
  "tpm_limit": 100000,
  "expires_at": null
}
```

- `billing_subject`：`null`/省略=账户默认；`"personal"`；`"org:{uuid}"`。PATCH 传 `null` 可清除。
- `allowed_models`：空=继承角色授权；非空=与角色授权取交集。
- `rpm_limit` / `tpm_limit`：`null`=不限。
- `GET /wallets/me` 响应字段与管理端钱包相同（含 `gift_balance` / `gift_held` / `total_available`）。
- `GET /wallets/me/gifts`：当前用户 **active** 赠送批次，**不暴露** `granted_by` / `revoked_by` / `idempotency_key`。
- 用量明细含 `gift_charge_amount` / `cash_charge_amount`（之和等于 `charge_amount`）。`fund_source=gift|cash|mixed`。

Chat 会话：

- 使用用户 JWT；仅能访问本人会话。
- `PUT .../messages` 整表替换消息列表（含多轮上下文），可选同步 `title` / `model` / `temperature` / `max_tokens`；消息可带 `citations`（RAG 引用快照）。
- 会话可挂载知识库：`kb_ids` / `miss_strategy`（`strict`|`fallback`）/ `retrieval_top_k` / `retrieval_score_threshold`，创建与 PATCH 均可设置；挂载时校验归属与库状态（active）。
- 模型调用仍走 `POST /v1/chat/completions`（平台 API Key），由前端传入完整 `messages` 历史。
- 挂载知识库时的问答编排：`POST /chat/kb-completions`（JWT + `X-Api-Key`），检索拼装后走现有 chat 流式链路，结束后追加 citations SSE 事件；**不改动** `/v1/*` 协议。

```http
POST /chat/kb-completions
Authorization: Bearer <JWT>
X-Api-Key: sk-...
Content-Type: application/json

{
  "session_id": "uuid",
  "model": "gpt-4o",
  "messages": [{"role": "user", "content": "公司年假几天？"}],
  "stream": true
}
```

流式结束后额外事件示例：

```json
{
  "object": "chat.completion.citations",
  "citations": [
    {
      "chunk_id": "uuid",
      "kb_id": "uuid",
      "document_id": "uuid",
      "document_name": "手册.pdf",
      "content_summary": "…",
      "score": 0.91,
      "index": 1
    }
  ],
  "rag": {
    "hit": true,
    "miss_strategy": "fallback",
    "used_knowledge_base": true,
    "call_model": true
  }
}
```

`miss_strategy=strict` 且未命中时：直接流式返回固定文案「知识库中未找到相关内容」，**不调用模型**（仍可能产生 query 向量化费用）。

创建 Key 响应示例：

```json
{
  "id": "uuid",
  "name": "local-dev",
  "key": "sk-xxxxxxxxx",
  "prefix": "sk-xxxxxxx...",
  "status": 0,
  "billing_subject": "personal",
  "allowed_models": [],
  "rpm_limit": null,
  "tpm_limit": null,
  "created_at": "2026-06-24T00:00:00.000000Z"
}
```

注意：`key` 字段仅创建时返回一次，列表接口不返回。`DELETE` 为软禁用（`status=disabled`）。

### 6.3 组织

```http
POST /orgs
GET /orgs
GET /orgs/{org_id}
PATCH /orgs/{org_id}
GET /orgs/{org_id}/members
POST /orgs/{org_id}/members
PATCH /orgs/{org_id}/members/{member_id}
DELETE /orgs/{org_id}/members/{member_id}
GET /orgs/{org_id}/wallet
GET /orgs/{org_id}/wallet/transactions
GET /orgs/{org_id}/reports/daily?from_date=&to_date=
GET /orgs/{org_id}/reports/members?from_date=&to_date=
```

- 创建：`{ "name", "billing_fallback": "deny"|"personal" }`；创建者成为 Owner。
- 邀请成员：`{ "user_id", "org_role": "member"|"admin"|"owner" }`（目标用户须已注册；无邮件邀请码）。
- 更新成员：至少一项 `{ org_role, can_consume_org_wallet, quota_period, quota_limit }`。
- 组织钱包与成员分摊报表：Owner/Admin；按天日报：活跃成员可读。钱包响应与个人钱包相同（含 `gift_balance` / `total_available`）。

### 6.4 在线支付

```http
POST /payments
GET /payments?status=&org_id=&limit=&offset=
GET /payments/{order_no}
POST /payments/callbacks/mock
POST /payments/callbacks/stripe
```

创建支付单：

```json
{
  "amount": 10000,
  "target_type": "personal",
  "org_id": null,
  "provider": "mock",
  "return_url": "http://localhost:8088/recharge"
}
```

- `target_type=org` 时必填 `org_id`，且当前用户须为组织 Owner。
- `provider`：`mock`（本地沙箱）或 `stripe`。
- 回调端点无需用户 JWT；Mock 需 HMAC 签名（`MOCK_PAYMENT_SECRET`）；Stripe 验 Webhook 签名。
- 同一支付单重复回调只入账一次（幂等键 `payment:{order_no}`）。

### 6.5 知识库（用户端）

鉴权：用户 JWT。路径前缀 `/kb`。资源按 `owner_user_id` 隔离；访问他人资源 → `403 forbidden`。

#### 6.5.1 知识库 CRUD

```http
GET /kb
POST /kb
GET /kb/{kb_id}
PATCH /kb/{kb_id}
DELETE /kb/{kb_id}
```

创建请求：

```json
{
  "name": "产品手册",
  "description": "可选",
  "embedding_model": "text-embedding-3-small",
  "chunk_size": 500,
  "chunk_overlap": 50,
  "separators": null,
  "top_k": 5,
  "score_threshold": 0.35
}
```

- `embedding_model` 省略则用系统默认；须为已启用的 `model_type=embedding` 模型。创建后不可更换。
- 触达角色库数配额 → `400 kb_quota_exceeded`。
- 响应含：`id, name, description, embedding_model_id, embedding_model, embedding_dim, chunk_size, chunk_overlap, separators, top_k, score_threshold, doc_count, chunk_count, storage_bytes, status, created_at, updated_at`。
- `status`：`active` | `disabled`（管理员禁用后用户侧只读受限）。
- `DELETE` 级联清理文档、分块、向量与本地原文件；钱包/用量历史保留。`204`。

#### 6.5.2 文档上传与状态

```http
POST /kb/{kb_id}/documents
GET /kb/{kb_id}/documents?page=1&page_size=20
GET /kb/documents/{doc_id}
POST /kb/documents/{doc_id}/retry
DELETE /kb/documents/{doc_id}
```

上传：`multipart/form-data`，字段名 `files`（可多文件）。支持扩展名：`.txt` / `.md` / `.pdf` / `.docx` / `.html` / `.csv` / `.xlsx`。

| 错误 | code |
| --- | --- |
| 类型不支持或不匹配 | `unsupported_file_type` |
| 单文件过大 | `file_too_large` |
| 批量过多 | `batch_too_large` |
| 文档数配额 | `kb_doc_quota_exceeded` |
| 存储配额 | `kb_storage_quota_exceeded` |
| 库已禁用 | `kb_disabled` |

文档状态机：`uploaded` → `parsing` → `chunking` → `pending_embedding` → `embedding` → `ready`；旁路：`insufficient_balance`（余额不足停等，无扣费）、`partial`、`failed`。异步处理依赖 `kb_worker`（见部署文档）。

文档响应字段：`id, kb_id, file_name, file_type, file_size_bytes, status, error_message, total_tokens, progress, chunk_size, chunk_overlap, separators, created_at, updated_at`。

`POST .../retry`：失败或余额不足文档可重试。`DELETE` → `204`，同步扣减库级存储计数并删文件。

#### 6.5.3 分块工作台

```http
GET /kb/documents/{doc_id}/chunks?page=1&page_size=20&q=
POST /kb/documents/{doc_id}/chunks
POST /kb/documents/{doc_id}/rechunk
PATCH /kb/chunks/{chunk_id}
DELETE /kb/chunks/{chunk_id}
POST /kb/chunks/batch
```

分块字段：`id, document_id, kb_id, seq, content, token_count, source_location, is_enabled, is_manually_edited, embedding_status, created_at, updated_at`。

- `embedding_status`：`pending` | `success` | `failed`。编辑内容后自动置 `pending` 并入队增量向量化。
- `PATCH`：可改 `content` 和/或 `is_enabled`；禁用分块不参与检索。
- 批量：

```json
{ "chunk_ids": ["uuid", "..."], "action": "enable|disable|delete" }
```

响应：`{ "action", "affected" }`。

- 重新分块须显式确认：

```json
{ "confirm": true, "chunk_size": 600, "chunk_overlap": 80, "separators": null }
```

`confirm` 不为 `true` → `400 invalid_request`。将删除现有分块（含人工编辑）并重新计费向量化。

#### 6.5.4 命中测试

```http
POST /kb/{kb_id}/hit-test
```

```json
{ "query": "年假几天？", "top_k": 5, "score_threshold": 0.35 }
```

- `top_k` / `score_threshold` 省略则用库默认。
- 库非 `active` → `400 kb_disabled`。
- query 向量化走 embedding 计费（`source=kb_query`），余额不足 → `402 insufficient_balance`。

响应：

```json
{
  "query": "年假几天？",
  "top_k": 5,
  "score_threshold": 0.35,
  "hits": [
    {
      "chunk_id": "uuid",
      "document_id": "uuid",
      "document_name": "手册.pdf",
      "content": "…",
      "score": 0.91,
      "seq": 3,
      "source_location": {"page": 2},
      "token_count": 120
    }
  ],
  "estimated_charge_amount": 12,
  "request_id": "req_xxx",
  "input_tokens": 8
}
```

用量来源约定：

| source | 场景 |
| --- | --- |
| `kb_ingest` | 文档向量化（含重新分块、增量重嵌入） |
| `kb_query` | 命中测试 / Chat RAG 的 query 向量化 |
| `chat` | RAG 后实际调用 chat 模型 |

---

## 7. 幂等与追踪

建议客户端可传：

```http
X-Request-ID: req_xxx
```

若未传，由网关生成。系统内部必须用同一个 `request_id` 关联：

- request log
- usage log
- hold transaction
- capture transaction
- release transaction
- upstream request trace

同一 `request_id` 的结算操作必须幂等。

---

## 8. MVP curl 验证样例

### 查询模型

```bash
curl -H "Authorization: Bearer sk_xxx" http://localhost:8000/v1/models
```

### 非流式调用

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer sk_xxx" \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o","messages":[{"role":"user","content":"hello"}],"stream":false}'
```

### 流式调用

```bash
curl -N http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer sk_xxx" \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o","messages":[{"role":"user","content":"hello"}],"stream":true}'
```

### Mock 沙箱充值（示意）

```bash
# 1. 创建支付单
curl -X POST http://localhost:8000/payments \
  -H "Authorization: Bearer <user_jwt>" \
  -H "Content-Type: application/json" \
  -d '{"amount":10000,"target_type":"personal","provider":"mock"}'

# 2. 前端「在线充值」页可一键模拟支付；或自行按 MOCK_PAYMENT_SECRET 签名后回调
# POST /payments/callbacks/mock
```

### 知识库：创建 → 上传 → 命中测试（示意）

```bash
# 创建知识库
curl -X POST http://localhost:8000/kb \
  -H "Authorization: Bearer <user_jwt>" \
  -H "Content-Type: application/json" \
  -d '{"name":"手册库","embedding_model":"text-embedding-3-small"}'

# 上传文档（需另开终端跑 kb_worker 至 status=ready）
curl -X POST "http://localhost:8000/kb/<kb_id>/documents" \
  -H "Authorization: Bearer <user_jwt>" \
  -F "files=@./sample.pdf" \
  -F "files=@./sample.md"

# 命中测试
curl -X POST "http://localhost:8000/kb/<kb_id>/hit-test" \
  -H "Authorization: Bearer <user_jwt>" \
  -H "Content-Type: application/json" \
  -d '{"query":"年假几天？"}'
```

Stripe 沙箱步骤见 [PAYMENT_STRIPE_SANDBOX.md](./PAYMENT_STRIPE_SANDBOX.md)。

### 后台赠送（示意）

```bash
# 向用户个人钱包赠送 100 元（有效期 30 天）
curl -X POST http://localhost:8000/admin/wallets/users/<user_id>/gifts \
  -H "Authorization: Bearer <admin_jwt>" \
  -H "Content-Type: application/json" \
  -d '{"amount":1000000,"expires_in_days":30,"reason":"promotion","remark":"验收赠送","idempotency_key":"e2e-grant-1"}'

# 查询批次
curl -H "Authorization: Bearer <admin_jwt>" \
  "http://localhost:8000/admin/wallets/<wallet_id>/gifts"

# 撤回未消耗剩余
curl -X POST http://localhost:8000/admin/gifts/<grant_id>/revoke \
  -H "Authorization: Bearer <admin_jwt>" \
  -H "Content-Type: application/json" \
  -d '{"remark":"验收撤回"}'

# 前台查看自己的赠送批次
curl -H "Authorization: Bearer <user_jwt>" http://localhost:8000/wallets/me/gifts
```