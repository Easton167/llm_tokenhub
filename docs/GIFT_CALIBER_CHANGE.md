# 赠送余额：口径变更影响说明

| 项目 | 内容 |
| --- | --- |
| 文档名称 | `balance` 语义收窄与收入口径变更 |
| 依据 | `docs/prd/wallet.md`、`docs/prd/billing.md`（D4–D7）、赠送专项任务 9 |
| 日期 | 2026-08-17 |
| 受众 | 前端、财务、运营、BI、对外文档维护者 |

---

## 1. 发生了什么

赠送余额专项把钱包拆成两个资金池，并改了「收入」的定义。这不是新计费公式，**应付金额怎么算不变**；变的是「这笔钱从哪扣」和「哪一部分算平台收入」。

| 变更 | 旧口径 | 新口径 |
| --- | --- | --- |
| `wallets.balance` | 用户全部可用余额 | **仅现金池**（充值 / 调账，可退可提） |
| 总可用额 | `balance + credit_limit` | `gift_balance + balance + credit_limit`（字段 `total_available`） |
| 402 判定 | 现金不足即拒绝 | 两池 + 授信合计不足才拒绝 |
| `usage_logs.charge_amount` | 被当作收入 | 仍是应付总额，**不是收入** |
| 平台收入 | `Σ charge_amount` 或报表 `revenue_amount` | `Σ cash_charge_amount` / `cash_revenue_amount` |
| 毛利 | `charge_amount - cost_amount` | `cash_charge_amount - cost_amount`（纯赠金时为负，属预期） |
| 退款 / 提现上限 | 容易按「全部余额」理解 | **只看现金池** `balance`；已花掉的赠金不参与 |

赠金消费记营销费用核销，不记收入。约束：`gift_charge_amount + cash_charge_amount = charge_amount`（整数拆分，无比例取整）。

---

## 2. 对下游的影响

### 2.1 对外文档与 API 客户端

- 文档或 SDK 若写「`balance` = 账户余额」，用户会以为钱变少了（赠金被拆出去了）。
- 必须同时展示 `gift_balance` 与 `total_available`；余额不足文案用总可用额，不要只看 `balance == 0`。
- 新错误码：`409 gift_already_consumed`、`422 gift_amount_exceeds_limit`。契约见 [API_CONTRACT.md](./API_CONTRACT.md) §5.7.1。
- OpenAI 兼容 `/v1/*` 响应结构不变；计费只影响 402 判定。

### 2.2 财务报表 / 运营看板

- 旧看板若把 `revenue_amount` 或 `Σ usage_logs.charge_amount` 标成「收入」，上线赠金后会把送出去的钱算进收入，毛利虚高。
- 正确映射：

| 报表要的数 | 用这个字段 |
| --- | --- |
| 计费总额（GMV 类，含赠金抵扣） | `revenue_amount` / `charge_amount` |
| 平台收入 | `cash_revenue_amount` / `cash_charge_amount` |
| 赠金消耗（营销核销） | `gift_consumed` / `gift_charge_amount` |
| 赠金对应上游成本 | `gift_cost` |
| 真实毛利 | `gross_profit`（已按现金收入 − 成本） |

- 管理端 `GET /admin/reports/*` 已按新口径出数；对账接口另校 `usage_gift_total` 与 capture `gift_amount`。

### 2.3 已有 BI / SQL

上线后直接跑旧 SQL 会错。常见查询与改法：

```sql
-- 旧：把应付当收入
SELECT sum(charge_amount) AS revenue FROM usage_logs;

-- 新：平台收入
SELECT sum(cash_charge_amount) AS revenue FROM usage_logs;

-- 旧：钱包余额
SELECT owner_id, balance FROM wallets;

-- 新：总可用额（展示 / 402）vs 可退现金
SELECT owner_id,
       gift_balance + balance + credit_limit AS total_available,
       balance AS refundable_cash,
       gift_balance
FROM wallets;
```

日表：不要再用 `usage_daily_summary.revenue_amount` 当收入；用 `cash_revenue_amount`。存量迁移已回填 `cash_revenue_amount = revenue_amount`、`gift_revenue_amount = 0`（迁移 `013_gift_credit`），**回填日之前没有赠金，数字碰巧相等；回填日之后必须改查询**。

流水：`wallet_transactions.amount` 仍是总变动；其中赠送池部分看 `gift_amount`。类型新增 `gift_grant` / `gift_revoke` / `gift_expire`。

账本不变式（对账任务每日校验，**只告警不修数**）：

```text
Σ(gift_grants.remaining WHERE status='active') == wallets.gift_balance + wallets.gift_held
Σ usage_logs.gift_charge_amount == Σ (-wallet_transactions.gift_amount WHERE type='capture')
```

---

## 3. 迁移建议

1. **先改查询再放量赠送**。未改 BI 之前不要大批量发放赠金，否则报表无法解释。
2. **前端 / 对外帮助中心**：钱包页同时展示充值余额、赠送余额、总可用额；注明赠金不可退、不可提、有有效期。
3. **财务关账**：约定从启用日切口径；启用日前继续用旧收入（当时无赠金，与新口径一致）；启用日后收入只取现金列。不要把历史 `revenue_amount` 重算——存量赠金列为 0。
4. **退款工单**：审核上限改为现金池；若用户余额「看起来很多」但全是赠金，应拒绝退款并引导花掉或等过期。
5. **告警**：`reconcile_gifts` 退出码 1、报表 `gift_split_mismatch` 必须进值班，禁止自动改钱包数字。
6. **不要做的事**：不要把充值入口改成可充进赠送池；不要把赠金算进可提现；不要用 float 或按比例分摊拆两池。

---

## 4. 相关文档

- 产品规则：[wallet.md](./prd/wallet.md)、[billing.md](./prd/billing.md)
- 契约 / 部署：[API_CONTRACT.md](./API_CONTRACT.md)、[DEPLOYMENT.md](./DEPLOYMENT.md)
- 验收：[VALIDATION_PLAN.md](./VALIDATION_PLAN.md) §3.8 / §5.4
- 表结构：[DATABASE_DESIGN.md](./DATABASE_DESIGN.md) §8
