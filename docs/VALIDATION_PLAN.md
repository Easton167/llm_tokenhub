# 验证与测试计划

| 项目 | 内容 |
| --- | --- |
| 文档名称 | LLM Gateway 验证与测试计划 |
| 输入依据 | `docs/PRD.md`（索引）/`docs/prd/`、`docs/TECH_DESIGN.md`、`docs/MVP_SCOPE.md`、`docs/prd/knowledge-base.md`、`docs/prd/wallet.md`、`docs/prd/billing.md` |
| 版本 | v0.3（含赠送余额验收口径） |

---

## 1. 验证目标

本计划用于约束后续 AI 编码和人工验收，确保每个阶段都有可重复验证方式。重点验证：

- OpenAI 兼容调用是否可用。
- 鉴权、权限和错误码是否正确。
- 钱包预扣、实扣、释放是否一致。
- 失败请求是否不计费。
- 流式中断是否按 partial 处理。
- 路由、重试和上游失败释放是否可靠。
- 日志、用量、流水是否可追踪。
- 知识库：上传→解析分块→向量化→命中测试→Chat 引用问答→用量对账闭环。
- 赠送余额：双池预扣实扣、FIFO 批次、用量拆分、收入口径、过期回收与对账。

---

## 2. 测试分层

| 层级 | 目标 | 示例 |
| --- | --- | --- |
| 单元测试 | 验证纯函数和服务逻辑 | 费用计算、Key hash、路由权重 |
| 事务测试 | 验证数据库一致性 | 并发预扣、重复实扣、释放冻结 |
| API 测试 | 验证接口行为 | `/v1/models`、chat 非流式、错误码 |
| Mock 上游测试 | 验证上游不同响应 | 200、401、429、5xx、timeout |
| 流式测试 | 验证 SSE 行为 | chunk 透传、`[DONE]`、中断结算 |
| 回归测试 | 固定核心业务样例 | PRD 计费示例、失败不计费 |
| 手工验收 | 验证真实使用路径 | curl、OpenAI SDK、后台配置 |

---

## 3. 核心测试用例

### 3.1 鉴权

| 编号 | 场景 | 期望 |
| --- | --- | --- |
| AUTH-01 | 无 Authorization header | 401 `invalid_api_key` |
| AUTH-02 | Key 格式错误 | 401 `invalid_api_key` |
| AUTH-03 | Key hash 不存在 | 401 `invalid_api_key` |
| AUTH-04 | Key 已禁用 | 401 `invalid_api_key` |
| AUTH-05 | Key 有效 | 返回用户与 Key 上下文 |

### 3.2 模型权限

| 编号 | 场景 | 期望 |
| --- | --- | --- |
| PERM-01 | 模型不存在 | 404 `model_not_found` |
| PERM-02 | 模型禁用 | 404 `model_not_found` |
| PERM-03 | 用户角色未授权 | 403 `model_not_authorized` |
| PERM-04 | 用户角色已授权 | 校验通过 |
| PERM-05 | `/v1/models` 查询 | 只返回授权模型 |

### 3.3 钱包与计费

| 编号 | 场景 | 期望 |
| --- | --- | --- |
| BILL-01 | 余额不足预扣 | 402，不写 usage，不调用上游 |
| BILL-02 | 余额足够预扣 | 现金池 `balance` 减少、`held` 增加；若赠金优先则先动 `gift_balance` / `gift_held` |
| BILL-03 | 实扣小于预扣 | 差额按占用逆序释放（先退现金，再退赠送） |
| BILL-04 | 实扣等于预扣 | `held` 清零，无额外释放 |
| BILL-05 | 实扣大于预扣 | 按策略追加扣费或标记估算不足 |
| BILL-06 | 重复 capture | 不重复扣费 |
| BILL-07 | 失败 release | 冻结全额释放 |
| BILL-08 | 并发 hold | 不出现负余额 |

### 3.4 路由

| 编号 | 场景 | 期望 |
| --- | --- | --- |
| ROUTE-01 | 无模型映射 | 503 `no_available_channel` |
| ROUTE-02 | 渠道禁用 | 不参与候选 |
| ROUTE-03 | 渠道熔断 | 不参与候选 |
| ROUTE-04 | 多优先级渠道 | 高优先级优先 |
| ROUTE-05 | 同优先级多渠道 | 按权重分布 |
| ROUTE-06 | 上游 429 | 在预算内换 Key/渠道 |
| ROUTE-07 | 上游全部失败 | 释放预扣，返回错误 |

### 3.5 OpenAI 兼容接口

| 编号 | 场景 | 期望 |
| --- | --- | --- |
| API-01 | `GET /v1/models` | 返回 OpenAI list 结构 |
| API-02 | 非流式 chat 成功 | 返回 chat.completion |
| API-03 | 非流式 usage 返回 | 写 usage log 并扣费 |
| API-04 | 上游无 usage | 使用估算并标记 `is_estimated` |
| API-05 | 请求参数错误 | 400 `invalid_request` |

### 3.6 流式

| 编号 | 场景 | 期望 |
| --- | --- | --- |
| STREAM-01 | `stream=true` | 返回 `text/event-stream` |
| STREAM-02 | 上游 chunk | 按 OpenAI chunk 格式透传 |
| STREAM-03 | 正常结束 | 返回 `data: [DONE]` 并实扣 |
| STREAM-04 | 客户端中断 | 状态 partial，按已产出估算计费 |
| STREAM-05 | 上游开始前失败 | 释放冻结，不计费 |
| STREAM-06 | 上游输出后失败 | 状态 partial，按已产出内容计费 |

### 3.7 知识库（对齐 `docs/prd/knowledge-base.md` 验收口径）

自动化覆盖以 `backend/tests/test_kb_*.py` 为主；下列用例用于手工/回归勾选。

| 编号 | 场景 | 期望 |
| --- | --- | --- |
| KB-01 | 上传 TXT/MD/PDF/DOCX/HTML/XLSX 各一份，worker 跑完 | 全部 `ready`；分块数与 `embedding_status=success` 向量数一致 |
| KB-02 | 向量化成功 | 有 `usage`（`source=kb_ingest`）与钱包 hold/capture；金额与规则一致 |
| KB-03 | 余额不足时触发向量化 | 文档 `insufficient_balance`；无 usage 扣费、无 capture |
| KB-04 | 禁用某分块后命中测试 / Chat 检索 | 不再返回该分块；重新启用后可再次命中 |
| KB-05 | 编辑分块内容后，用与新内容强相关问题命中测试 | 该分块排名提升（或进入 Top-K） |
| KB-06 | Chat 挂载知识库提问 | 回答含 citations，可定位到分块；卸载后同问题回退普通回答 |
| KB-07 | `miss_strategy=strict` 问库外问题 | 固定文案「知识库中未找到相关内容」；不调用 chat 模型、无 chat 扣费（可有 `kb_query`） |
| KB-08 | 会话挂载他人 `kb_id` | `403 forbidden`；无检索、无扣费 |
| KB-09 | 删除知识库 | 原文件、分块、向量清理；钱包与用量历史保留 |
| KB-10 | 触达库数/文档数/存储配额 | `400` + `kb_quota_exceeded` / `kb_doc_quota_exceeded` / `kb_storage_quota_exceeded`，提示明确 |
| KB-11 | 管理员禁用知识库 | 用户上传/命中测试 → `kb_disabled`；启用后恢复 |
| KB-12 | 重新分块未确认 | `confirm≠true` → `400`；确认后旧分块删除并重新向量化计费 |

对应自动化测试文件：

| 文件 | 侧重 |
| --- | --- |
| `test_kb_models.py` | 表结构 / 配额字段 |
| `test_kb_api.py` | CRUD、上传、配额错误码 |
| `test_kb_parse_chunk.py` | 解析与分块引擎 |
| `test_kb_ingest.py` | worker 状态机、6 类样例、并发领取 |
| `test_kb_embed.py` | 向量化计费与余额不足 |
| `test_kb_chunks.py` | 分块工作台编辑/禁用/重分块 |
| `test_kb_retrieval.py` | 命中测试与过滤 |
| `test_kb_rag_chat.py` | Chat RAG、strict/fallback、越权 403 |
| `test_kb_admin.py` | 管理端配置、禁用、用户汇总 |

### 3.8 赠送余额（对齐 `docs/prd/wallet.md` 赠送余额与 `docs/prd/billing.md` 收入口径）

自动化覆盖以 `backend/tests/test_gift_*.py` 为主。金额一律整数最小单位。

| 编号 | 场景 | 期望 |
| --- | --- | --- |
| GIFT-01 | 后台向个人/组织钱包赠送 | `gift_balance` 增加；写 `gift_grant` 流水与 `gift_grants` 批次；审计 `wallet.gift_grant` |
| GIFT-02 | 同一 `idempotency_key` 重复赠送 | HTTP 200 返回同一 `grant_id`，`gift_balance` 只加一次 |
| GIFT-03 | 超过单笔或操作人日累计上限 | 422 `gift_amount_exceeds_limit`，不写批次 |
| GIFT-04 | 未指定到期且未 `never_expires=true` | 422，防误操作永久有效 |
| GIFT-05 | `expires_in_days=N` | 到期时刻 = `BUSINESS_TIMEZONE` 到期日 23:59:59 的 UTC |
| GIFT-06 | 现金为 0、赠金充足 | hold 成功，不 402；前端不误报「余额不足」 |
| GIFT-07 | 扣减优先级 | 赠送（批次 `expires_at` FIFO）→ 现金 → 授信 |
| GIFT-08 | PRD 跨池示例：赠 30、现金 1000，预扣 100、实扣 60 | hold：`gift_held=30, held=70`；capture：`gift_charge=30, cash_charge=30`；终态 `gift_balance=0, balance=970` |
| GIFT-09 | capture 差额退还 | 先退现金占用，再退赠送占用（与「按 actual 重新分摊」等价） |
| GIFT-10 | 失败 release | `gift_hold` / `cash_hold` 原路退回，批次 `remaining` 不变 |
| GIFT-11 | 用量拆分 | `gift_charge_amount + cash_charge_amount = charge_amount`；纯赠金毛利 = `-cost_amount` |
| GIFT-12 | 报表收入 | `revenue_amount` 含赠金但不是平台收入；`gross_profit = cash_revenue_amount - cost_amount` |
| GIFT-13 | 撤回未消耗剩余 | `gift_balance` 减少 `remaining`；批次 `revoked`；已消费部分不追回 |
| GIFT-14 | 撤回已耗尽批次 | 409 `gift_already_consumed` |
| GIFT-15 | 过期回收 | `gift_expire` 流水；`gift_held>0` 的钱包本轮跳过，释放后下一轮可收 |
| GIFT-16 | 对账 | `Σ(active remaining) == gift_balance + gift_held`；`Σ gift_charge_amount == Σ -capture.gift_amount`；不一致只告警不修数 |
| GIFT-17 | 退款上限 | 只看现金池 `balance`，历史赠金消费不参与 |
| GIFT-18 | 组织成员额度 | `quota_used` 按总消费累计，不区分资金来源 |

对应自动化测试文件：

| 文件 | 侧重 |
| --- | --- |
| `test_gift_models.py` | 表结构 / CHECK / 存量回填 |
| `test_gift_ledger.py` | 发放 / 撤回 / 过期 / FIFO 核销与幂等 |
| `test_gift_billing.py` | 双池 hold/capture/release、PRD 示例、等价性 |
| `test_gift_api.py` | 后台赠送/撤回、风控、审计、前台只读 |
| `test_gift_usage_reports.py` | 用量拆分、日汇总、报表毛利、fund_source |
| `test_gift_expire_reconcile.py` | 过期跳过规则、对账只告警 |
| `test_gift_e2e_regression.py` | 任务 9 全链路数值对照 |

既有资金测试（改造后须全绿）：`test_wallet_billing.py`、`test_org_wallet.py`、`test_org_gateway_billing.py`、`test_kb_embed.py`。

---

## 4. PRD 计费回归样例

金额单位：`1 元 = 10000 单位`。

### 示例 A：普通 chat

输入：

- 输入 1500 tokens
- 输出 800 tokens
- 输入单价 18 元 / 1M
- 输出单价 72 元 / 1M
- 折扣 1.0

期望：

- 输入费用 0.027 元
- 输出费用 0.0576 元
- 总费用 0.0846 元
- 最小单位金额 846

### 示例 B：缓存命中

输入：

- 输入 2000 tokens，其中缓存 1600，非缓存 400
- 输出 500 tokens
- 非缓存输入 18 元 / 1M
- 缓存输入 9 元 / 1M
- 输出 72 元 / 1M
- 折扣 0.9

期望：

- 折扣后费用 0.05184 元
- 向上取整到 0.0001 元
- 最小单位金额 519

### 示例 C：预扣和实扣

输入：

- `max_tokens=2000`
- 输入估算 1500 tokens
- 预扣金额 1710 单位
- 实扣金额 846 单位

期望：

- 释放金额 864 单位
- 最终扣费 846 单位

### 示例 D：赠金与现金混合支付（对齐 billing.md）

输入：

- 应付 846 单位，用户赠送池 300、现金池 5000
- 预扣 1710 → 冻结 `gift 300 + cash 1410`
- 实扣 846 → 退还 864（逆序先退现金）

期望：

- `charge_amount = 846`，`gift_charge_amount = 300`，`cash_charge_amount = 546`
- 上游成本 705；**真实毛利 = 546 - 705 = -159**（营销净投入），而非旧口径 +141
- `gift_charge_amount + cash_charge_amount = charge_amount`

---

## 5. 手工验收流程

### 5.1 初始化配置

后续代码完成后，应能通过后台 API 或 seed 脚本完成：

1. 创建用户。
2. 创建个人钱包并充值。
3. 创建角色。
4. 创建模型。
5. 授权角色使用模型。
6. 创建 Provider。
7. 创建 Channel。
8. 创建 Channel Key。
9. 创建 Model Mapping。
10. 创建 Billing Rule。
11. 创建平台 API Key。

### 5.2 curl 验证

查询模型：

```bash
curl -H "Authorization: Bearer sk_xxx" http://localhost:8000/v1/models
```

非流式调用：

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer sk_xxx" \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o","messages":[{"role":"user","content":"hello"}],"stream":false}'
```

流式调用：

```bash
curl -N http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer sk_xxx" \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o","messages":[{"role":"user","content":"hello"}],"stream":true}'
```

余额不足验证：

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer sk_xxx" \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o","messages":[{"role":"user","content":"hello"}],"max_tokens":999999}'
```

期望返回 `402 insufficient_balance`。

### 5.3 知识库手工验收（全链路）

前置：见 [DEPLOYMENT.md](./DEPLOYMENT.md) §5（pgvector、embedding 模型、余额、`kb_worker`）。

建议路径（与验收口径一一对应）：

1. **上传 6 类文件 → 就绪**：创建知识库 → 上传 TXT/MD/PDF/DOCX/HTML/XLSX → 观察文档至 `ready`；核对分块数与成功向量数。
2. **用量对账**：用量页/API 可见 `kb_ingest`；钱包有对应 hold/capture。可另测零余额上传至 `insufficient_balance` 且无扣费。
3. **调整分块**：禁用一分块 → 命中测试不再出现；编辑另一分块关键词 → 相关 query 排名上升；可选重新分块（须确认）。
4. **命中测试**：`POST /kb/{id}/hit-test`，页面明示本次 `estimated_charge_amount`（`kb_query`）。
5. **Chat 带引用问答**：会话挂载该库 → 提问 → 流式结束后有 citations；切 `strict` 问无关问题 → 未找到文案且无模型调用；卸载知识库后同问题无引用。
6. **权限与清理**：换账号挂载他人 `kb_id` → 403；删库后文件与向量不在，历史 usage 仍在；调低角色配额验证拒绝提示。

### 5.4 赠送余额端到端（全链路数值对照）

前置：迁移已到 `013_gift_credit`；`BUSINESS_TIMEZONE=Asia/Shanghai`。金额单位：`1 元 = 10000`。

建议路径（可重复执行：`pytest tests/test_gift_e2e_regression.py -q`）：

1. **后台赠送 100 元（有效期 30 天）**：`POST /admin/wallets/users/{id}/gifts`，`amount=1000000`，`expires_in_days=30`。钱包另有现金 50 元（500000）。
2. **调用 1（纯赠金）**：预扣 20 元、实扣 15 元。查 `usage_logs` 与 hold/capture 的 `gift_amount`。
3. **调用 2（赠金用尽转现金）**：预扣/实扣 90 元。剩余赠金 85 元全部花完，现金补 5 元。
4. **调用 3（纯现金）**：实扣 1 元，全部现金。
5. **撤回剩余**：原 100 元批次 `remaining=0` → `409 gift_already_consumed`。另发一笔未消耗批次再撤回，确认现金池不动。
6. **过期任务**：发放一笔已到期批次，跑 `expire_due_gifts`，`gift_balance` 与 `remaining` 归零。
7. **对账**：`reconcile_gifts` 无钱包差异、用量赠金与 capture 拆分一致。

数值对照表（与 `test_gift_e2e_regression.py` 断言一致）：

| 步骤 | 字段 | 预期 | 实际 |
| --- | --- | --- | --- |
| 赠送后 | gift_balance / balance / total_available | 1000000 / 500000 / 1500000 | 同预期 |
| 赠送后 | 批次 remaining / status | 1000000 / active | 同预期 |
| 调用 1 hold | amount / gift_amount | -200000 / -200000 | 同预期 |
| 调用 1 capture | amount / gift_amount | -150000 / -150000 | 同预期 |
| 调用 1 usage | charge / gift_charge / cash_charge | 150000 / 150000 / 0 | 同预期 |
| 调用 1 后钱包 | gift_balance / balance | 850000 / 500000 | 同预期 |
| 调用 2 hold | amount / gift_amount | -900000 / -850000 | 同预期 |
| 调用 2 capture | amount / gift_amount | -900000 / -850000 | 同预期 |
| 调用 2 usage | charge / gift_charge / cash_charge | 900000 / 850000 / 50000 | 同预期 |
| 调用 2 后钱包 | gift_balance / balance | 0 / 450000 | 同预期 |
| 调用 3 usage | gift_charge / cash_charge | 0 / 10000 | 同预期 |
| 调用 3 后钱包 | gift_balance / balance | 0 / 440000 | 同预期 |
| 撤回原批次 | HTTP / code | 409 / gift_already_consumed | 同预期 |
| 撤回未消耗批次后 | gift_balance / balance | 0 / 440000 | 同预期 |
| 过期回收后 | 到期批次 remaining / status | 0 / expired | 同预期 |
| 对账 | wallet mismatches / gift_split_matched | 空 / true | 同预期 |
| 不变式 | Σ active remaining == gift_balance + gift_held | 成立 | 同预期 |

手工 curl 序列见 [API_CONTRACT.md §8](./API_CONTRACT.md)。过期 / 对账 CLI 见 [DEPLOYMENT.md §7](./DEPLOYMENT.md)。

---

## 6. 发布前检查清单

- 所有测试通过（`cd backend && pytest -q`）。
- 没有明文平台 API Key 落库。
- 没有明文上游 Channel Key 出现在接口响应或日志中。
- 钱包并发测试通过。
- 失败请求不生成扣费 usage log。
- 成功请求能关联 request log、usage log 和 wallet transaction。
- 流式响应能正常结束并结算。
- 流式中断有 partial 记录。
- 管理 API 写操作有审计记录或预留审计入口。
- README / 部署文档能让新开发者启动含 pgvector 与 `kb_worker` 的完整环境。
- 知识库验收口径 KB-01～KB-12 已勾选或由自动化等价覆盖。
- 赠送余额验收口径 GIFT-01～GIFT-18 已勾选或由 `tests/test_gift_*.py` 覆盖；端到端数值对照表与预期一致。
- 报表「收入」使用 `cash_revenue_amount`，未把赠金算进毛利。
- `expire_gifts` / `reconcile_gifts` 已写入部署 cron。
- 中文文档与前端文案 UTF-8 无乱码（严格解码 + 关键词抽查）。

---

## 7. 每批 AI 编码后的复盘问题

每批代码完成后，需要回答：

- 这批是否引入了超出 MVP 的能力？
- 是否修改了计费、钱包或权限核心规则？
- 是否新增了未测试的错误分支？
- 是否有明文密钥、金额 float、流水覆盖等高风险问题？
- 是否需要同步更新 `docs/TECH_DESIGN.md`、`docs/API_CONTRACT.md` 或 `docs/DATABASE_DESIGN.md`？
- 知识库改动是否影响向量化计费幂等或 RAG 越权隔离？
- 赠送改动是否破坏双池不变式、收入口径或既有纯现金测试？

---

## 8. 全量回归记录

| 项目 | 内容 |
| --- | --- |
| 日期 | 2026-08-17 |
| 范围 | 一、二期 + 知识库专项 + 赠送余额专项（任务 9 收尾） |
| 命令 | `cd backend && pytest -q` |
| 自动化结果 | **456 passed**（110.80s）。本地手工脚本 `tests/test_openai.py`、`tests/test_url_key.py` 已由 `pyproject.toml` `addopts --ignore` 排除，不参与收集 |
| 端到端数值 | `tests/test_gift_e2e_regression.py` 通过；对照表见 §5.4，每步实际值与预期值一致 |
| 既有资金测试 | `test_wallet_billing` / `test_org_wallet` / `test_org_gateway_billing` / `test_kb_embed` 含于全量 456 passed |
| 验收口径 | GIFT-01～GIFT-18 由 `tests/test_gift_*.py` 覆盖；KB-01～KB-12 由 `tests/test_kb_*.py` 覆盖 |
| 中文完整性 | UTF-8 严格解码通过（无 BOM）：`API_CONTRACT` / `DEPLOYMENT` / `VALIDATION_PLAN` / `README` / `GIFT_CALIBER_CHANGE` / `MVP_SCOPE` / `PHASE2_SCOPE` / `TECH_DESIGN` / `prd/wallet.md` / `prd/billing.md`。前端 `src` 关键词存在：赠送余额×11、充值余额×10、总可用额×4、不可退款×6、不可提现×6。文档中出现的 `??` 仅为「乱码症状说明」或 TS 空值合并的文字描述，不是损坏汉字 |

上一轮（2026-07-27，知识库任务 12）：375 passed。
