# MVP 范围说明

| 项目 | 内容 |
| --- | --- |
| 文档名称 | LLM Gateway MVP 范围说明 |
| 输入依据 | `docs/PRD.md`（索引）/`docs/prd/`、`docs/TECH_DESIGN.md` |
| 版本 | v0.1 |

---

## 1. MVP 目标

MVP 的目标不是一次性完成完整 AI 中台，而是验证最核心的商业闭环：

```text
用户使用平台 API Key 调用统一模型接口，平台完成鉴权、路由、上游转发、计费扣费和用量记录。
```

首版成功标准：

- 一个用户可以创建或被分配平台 API Key。
- 用户可以通过 OpenAI SDK 或 HTTP 调用 `/v1/chat/completions`。
- 平台可以把请求转发到 OpenAI-compatible 上游渠道。
- 请求成功后按实际 token 用量扣除个人钱包余额（后续赠送专项起，扣减顺序为赠送池 → 现金池；`balance` 仅表示现金）。
- 请求失败时不计费，并释放已冻结额度。
- 后台可以维护模型、渠道、上游 Key、计费规则和钱包充值。

---

## 2. MVP 用户故事

| 编号 | 用户故事 | 验收标准 |
| --- | --- | --- |
| U1 | 作为平台管理员，我可以配置一个上游厂商和渠道 | 后台 API 可创建 Provider、Channel、Channel Key，并能禁用 |
| U2 | 作为平台管理员，我可以发布一个对外模型别名 | 模型别名可映射到具体渠道和上游模型名 |
| U3 | 作为平台管理员，我可以配置模型计费规则 | 调用时能命中生效中的规则版本 |
| U4 | 作为用户，我可以使用平台 API Key 调用模型 | `Authorization: Bearer sk-xxx` 可完成鉴权 |
| U5 | 作为用户，我只能看到有权限的模型 | `/v1/models` 只返回角色授权模型 |
| U6 | 作为用户，我有个人钱包余额 | 总可用额不足时请求被拒绝；充足时可预扣（现金不足但赠金充足时同样可调用） |
| U7 | 作为用户，我能收到流式响应 | `stream=true` 时返回 OpenAI 兼容 SSE |
| U8 | 作为运营，我能查询请求和用量 | 可按用户、模型、时间查询 request log 和 usage log |
| U9 | 作为财务/运营，我能追踪每笔扣费 | 钱包流水只追加，能关联 request_id |

---

## 3. MVP 功能边界

### 3.1 对外调用

MVP 包含：

- `POST /v1/chat/completions`
- `GET /v1/models`
- OpenAI 兼容请求结构
- OpenAI 兼容错误结构
- 非流式响应
- 流式 SSE 响应
- 基于平台 API Key 的 Bearer 鉴权

MVP 不包含：

- `/v1/embeddings`
- `/v1/rerank`
- `/v1/images/generations`
- Function calling 的完整兼容验证
- Assistants、Responses API 等高级 OpenAI API

### 3.2 渠道与路由

MVP 包含：

- Provider 管理
- Channel 管理
- Channel Key 加密存储和脱敏展示
- Model-Channel Mapping
- 启用/禁用状态
- 基础健康状态
- 优先级 + 权重选择
- 上游失败后的有限重试

MVP 不包含：

- 按成本/延迟/质量智能路由
- A/B 灰度
- 多地域就近路由
- SLA 分级路由

### 3.3 计费与钱包

MVP 包含：

- 个人钱包
- 后台手工充值/调账（只入现金池）
- 预扣 hold
- 实扣 capture
- 释放 release
- 失败请求不计费
- 流式部分成功按已产出内容计费
- 计费规则版本化
- 金额整数最小单位
- 钱包流水只追加

MVP 不包含：

- 组织钱包
- 成员额度
- 在线支付
- 套餐包
- 订阅制
- 企业授信
- 自动充值
- 赠送余额双资金池（后置于赠送专项，见 `AI_TASK_BREAKDOWN_GIFT.md`；落地后 `balance` 语义收窄为现金池）

### 3.4 用户与权限

MVP 包含：

- 用户表
- 角色表
- 用户-角色绑定
- 角色-模型授权矩阵
- 平台 API Key 管理
- Key 绑定个人计费主体

MVP 不包含：

- 组织成员角色
- Key 级模型范围
- Key 级 RPM/TPM 配额
- SSO/OAuth
- 多租户隔离

### 3.5 前端

MVP 交付顺序建议以后端 API 为主。若进入前端开发，优先级如下：

1. 后台管理页面：模型、渠道、计费规则、钱包、用量。
2. 用户侧 Key 管理和钱包页面。
3. 简单 Chat 页面。

MVP 不要求在第一批代码中完成完整前端。

---

## 4. 暂缓到二期的能力

| 能力 | 暂缓理由 | 前置条件 |
| --- | --- | --- |
| 组织钱包 | 会引入计费主体切换、成员额度、组织权限 | 个人钱包链路稳定 |
| 成员额度 | 需要额度周期、组织内审计和对账 | 组织钱包完成 |
| 在线支付 | 涉及支付回调、幂等、对账和风控 | 钱包流水稳定 |
| Embeddings/Rerank/Image | 请求响应和计费维度不同 | Chat 链路稳定 |
| 多厂商私有协议 | 协议差异大，测试成本高 | Adapter 接口稳定 |
| 报表看板 | 依赖真实 usage 数据沉淀 | request/usage logs 稳定 |
| 智能路由 | 需要历史延迟、质量、成本数据 | 基础路由稳定 |

---

## 5. 验收口径

MVP 可以进入下一阶段的最低验收口径：

- 使用 curl 调用 `/v1/chat/completions` 能返回非流式结果。
- 使用 curl 或 OpenAI SDK 调用 `stream=true` 能连续接收 SSE chunk。
- 余额不足时返回 `402 insufficient_balance`，且不调用上游。总可用额 = 赠送 + 现金 + 授信。
- 上游失败时返回标准错误，预扣金额被释放。
- 成功请求生成一条 request log、一条 usage log、至少一组钱包流水。
- 重复结算同一 `request_id` 不会重复扣费。
- 管理 API 可从零配置出一个可调用模型。

## 6. 后续口径变更（赠送专项，非 MVP 当时范围）

MVP 交付时钱包只有一个 `balance`。赠送余额专项落地后：

- `balance` / `held` 只表示**现金池**；总可用额看 `total_available`。
- 平台收入看 `cash_charge_amount` / `cash_revenue_amount`，不再用 `charge_amount` / `revenue_amount` 当收入。

影响与迁移建议见 [`GIFT_CALIBER_CHANGE.md`](./GIFT_CALIBER_CHANGE.md)。

---

## 7. 变更控制

若后续 AI 编码中出现以下情况，应先更新本文档和 `docs/TECH_DESIGN.md`，再继续写代码：

- MVP 想加入组织钱包或在线支付。
- 计费规则从纯按量变成套餐/订阅。
- 支持非 OpenAI-compatible 厂商私有协议。
- 需要多租户或私有化交付。
- Chat 会话正文持久化策略变更（保留周期、加密、合规导出等）。
