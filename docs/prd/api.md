# 关键 API 设计

> 返回 [PRD 索引](../PRD.md)

> 落地契约以 `API_CONTRACT.md` 为准。产品能力见 [统一调用网关](./gateway.md)。

---

## 鉴权约定（MVP）

| 场景 | 凭证 | 说明 |
| --- | --- | --- |
| 模型调用 `/v1/*` | 平台 API Key（`Bearer sk-...`） | 明文仅创建时返回一次 |
| 用户端 REST（`/auth`、`/keys`、`/wallets`、`/usage`） | 用户 JWT（`Bearer eyJ...`） | 邮箱密码登录签发 |
| 管理端 `/admin/*` | JWT + 管理角色（MVP 可暂放宽） | 与平台 API Key 分离 |

---

## 对外统一调用入口（OpenAI 兼容）

基地址：`https://{your-domain}/v1`，鉴权：`Authorization: Bearer {平台API Key}`。

### Chat Completions（非流式）
```http
POST /v1/chat/completions
Authorization: Bearer sk-xxxx
Content-Type: application/json

{
  "model": "gpt-4o",
  "messages": [{"role": "user", "content": "你好"}],
  "temperature": 0.7,
  "max_tokens": 1024,
  "stream": false
}
```
响应（OpenAI 标准结构）：
```json
{
  "id": "chatcmpl-xxx",
  "object": "chat.completion",
  "model": "gpt-4o",
  "choices": [{"index": 0, "message": {"role": "assistant", "content": "你好！"}, "finish_reason": "stop"}],
  "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20}
}
```

### Chat Completions（流式 SSE）
请求设 `"stream": true`，响应为 `text/event-stream`：
```text
data: {"id":"chatcmpl-xxx","object":"chat.completion.chunk","choices":[{"delta":{"content":"你"},"index":0}]}

data: {"id":"chatcmpl-xxx","object":"chat.completion.chunk","choices":[{"delta":{"content":"好"},"index":0}]}

data: {"choices":[{"delta":{},"finish_reason":"stop","index":0}],"usage":{"prompt_tokens":12,"completion_tokens":8,"total_tokens":20}}

data: [DONE]
```

### 其他兼容端点
- `GET /v1/models`：返回当前 Key 有权使用的模型列表（OpenAI 结构）。
- `POST /v1/embeddings`：向量化（二期）。
- `POST /v1/rerank`、`POST /v1/images/generations`：按需扩展（二期）。

### 计费主体指定（可选头）
```http
X-Billing-Subject: org:{org_id}   # 覆盖默认/Key绑定主体（二期）
```

### 统一错误码
| HTTP | 业务码 | 说明 |
| --- | --- | --- |
| 401 | invalid_api_key | Key 无效/禁用 |
| 401 | unauthorized | JWT 无效/登录失败 |
| 403 | model_not_authorized | 角色无权使用该模型 |
| 402 | insufficient_balance | 余额/额度不足（赠送 + 现金 + 授信合计） |
| 404 | model_not_found | 模型不存在 |
| 429 | rate_limited | 触发限流 |
| 503 | no_available_channel | 无可用渠道 |
| 502 | upstream_error | 上游错误(已重试失败) |
| 409 | gift_already_consumed | 赠送批次已消耗完，无可撤回余额 |
| 422 | gift_amount_exceeds_limit | 赠送金额超过单笔 / 日累计上限 |

## 管理端 / 用户端 REST 接口（示例）

> 鉴权：后台用 JWT + 管理角色；前台用户操作用 JWT。`/v1` 仍用平台 API Key。

### 用户认证（前台）
```http
POST   /auth/register                   # 邮箱密码注册（自动创建个人钱包）
POST   /auth/login                      # 登录，返回 access_token
GET    /auth/me                         # 当前用户资料（需 JWT）
```

### 渠道管理
```http
POST   /admin/channels                 # 创建渠道
GET    /admin/channels                  # 列表(分页/筛选)
PATCH  /admin/channels/{id}             # 更新(优先级/权重/状态)
POST   /admin/channels/{id}/keys        # 新增上游Key
PATCH  /admin/channels/{id}/keys/{kid}  # 更新/禁用Key
POST   /admin/channels/{id}/test        # 健康检查/连通测试
```

### 模型管理
```http
POST   /admin/models                    # 创建模型(别名/类型/能力)
PATCH  /admin/models/{id}               # 更新(可见性/状态)
POST   /admin/models/{id}/mappings      # 配置模型-渠道映射(上游名/权重)
```

### 计费规则
```http
POST   /admin/models/{id}/billing-rules # 新建计费规则版本
GET    /admin/models/{id}/billing-rules # 规则历史
```

### 用户 / 角色 / 组织
```http
POST   /admin/roles                     # 角色CRUD
POST   /admin/roles/{id}/models         # 配置角色授权模型(矩阵)
POST   /admin/users/{id}/roles          # 给用户分配角色
POST   /admin/orgs                       # 创建组织（二期）
POST   /admin/orgs/{id}/members          # 添加成员（二期）
PATCH  /admin/orgs/{id}/members/{mid}    # 设置成员额度/消费开关（二期）
```

### 钱包与用量
```http
POST   /admin/wallets/{id}/recharge      # 后台充值/调账
GET    /wallets/me                        # 我的个人钱包（前台，JWT）
GET    /wallets/me/transactions           # 我的流水（前台，JWT）
GET    /wallets/org/{id}                  # 组织钱包（二期）
GET    /usage?from&to&model               # 我的用量明细（前台，JWT；group_by 汇总为二期）
GET    /admin/usage-logs                  # 运营用量查询
```

### 赠送余额（后台，超管 / 财务角色）
```http
POST   /admin/wallets/users/{user_id}/gifts    # 赠送（个人钱包）
POST   /admin/wallets/orgs/{org_id}/gifts      # 赠送（组织钱包）
GET    /admin/wallets/{wallet_id}/gifts        # 批次列表（金额/剩余/到期/状态/原因）
POST   /admin/gifts/{grant_id}/revoke          # 撤回未消耗部分
GET    /wallets/me/gifts                        # 我的赠送余额批次（前台，JWT）
```

赠送请求体：
```json
{
  "amount": 100000,
  "expires_in_days": 30,
  "reason": "promotion",
  "remark": "618 活动新用户赠送",
  "idempotency_key": "promo-618-user-8f3a"
}
```
- `amount`：最小货币单位整数，必填且 > 0，受单笔上限约束。
- `expires_in_days` 与 `expires_at` 二选一；两者都不传则**永久有效**（需显式传 `"never_expires": true` 确认，防误操作）。
- `idempotency_key`：必填，同一钱包重复提交返回已存在的批次（HTTP 200，不重复发放）。

钱包查询响应（新增字段，`balance` 语义收敛为现金池）：
```json
{
  "balance": 500000,
  "held": 1710,
  "gift_balance": 30000,
  "gift_held": 0,
  "total_available": 530000,
  "currency": "CNY"
}
```

用量明细新增资金来源拆分：
```json
{
  "request_id": "req_xxx",
  "charge_amount": 846,
  "gift_charge_amount": 300,
  "cash_charge_amount": 546
}
```

### 用户端 Key 管理
```http
POST   /keys                              # 创建API Key（JWT；仅返回一次明文）
GET    /keys                              # 列表（脱敏，JWT）
DELETE /keys/{id}                         # 禁用 Key（软禁用，JWT）
```

---
