# 阶段 5 实施细则：钱包、计费与支付——资金安全

> 配套 `docs/learning_plan.md` 的「阶段 5」。阅读模式字母见 [README](./README.md#二四种读代码的方式后面每条读单都会标)。
> 前置：阶段 4 过关（尤其是 hold/capture/release 三段式）。
> 产出物：`docs/learning/notes/stage-5.md`，含 1 份手工对账 SQL 结果 + 1 份对账任务的异常场景清单 + 1 个可跑的月度对账单接口。

**本阶段一句话**：阶段 4 你看的是「钱怎么被花掉」，这一阶段看的是「钱怎么进来、归谁、以及**当代码崩了钱怎么办**」。这是全项目最不能出错的部分。

---

## 零、一条必须先立下的规矩：先只看现金

`wallet.balance` 现在只是**现金池**，旁边还有一个赠送池 `gift_balance`。

**本阶段请把所有 `gift_*` 字段当成恒为 0**，遇到 `_gift_first_split`、`gift_service.consume`、`gift_charge_amount` 这些一律跳过。两件事混在一起读，你会两件都读不懂。

对照表（读代码时遇到哪个，先查这张表决定读不读）：

| 现在要读（现金主线） | 跳过（阶段 6） |
| --- | --- |
| `recharge` / `adjust` | `TX_GIFT_GRANT/REVOKE/EXPIRE` 常量 |
| `hold/capture/release` 里的 `cash_*` 分支 | `_gift_first_split`（`wallet_service.py:121-128`） |
| `payment_orders` 整条充值链 | `gift_grants` 表、`app/services/gift_service.py` |
| `quota_service` | `get_charge_split`（`wallet_service.py:512-534`） |
| `reconcile_holds` / `reconcile_payments` | `expire_gifts` / `reconcile_gifts` |
| `/wallets/me`、`/wallets/me/transactions` | `/wallets/me/gifts`（`api/wallets.py:28-37`） |

---

## 一、怎么读

### 读单总览

| # | 内容 | 模式 | 预算 | 核心问题 |
| --- | --- | --- | --- | --- |
| D1 | 4 个测试文件 | T | 60 min | 先看行为 |
| D2 | `wallet_service.py` 现金主线 | Q | 60 min | 账本怎么记 |
| D3 | `billing_subject_service.py` | Q | 50 min | 这笔钱算谁的 |
| D4 | `quota_service.py` | Q | 40 min | 成员额度 |
| D5 | `org_service.py` + `api/orgs.py` | S+Q | 40 min | 组织域 |
| D6 | 支付四件套 | Q | 70 min | 充值 + 幂等 |
| D7 | 两个对账任务 | Q | 50 min | 崩了怎么办 |

---

### D1 · 先读测试（模式 T，60 分钟）

四个文件，只看函数名和 assert，整理成场景表。**特别标出下面这些**，它们是本阶段的骨架：

`tests/test_wallet_billing.py`（22 个用例）——挑这 6 个精读：

| 测试 | 行号 | 教你什么 |
| --- | --- | --- |
| `test_hold_fails_when_insufficient_balance` | `348` | 402 的触发条件 |
| `test_hold_idempotent` | `370` | 同 request_id 重复 hold 只扣一次 |
| `test_capture_releases_excess_hold` | `390` | **预扣 100 实扣 60，差额 40 怎么回来的** |
| `test_capture_idempotent_no_double_charge` | `446` | |
| `test_concurrent_hold_no_negative_balance` | `674` | **并发怎么防超扣** |
| `test_full_billing_flow` | `807` | 端到端 |

`tests/test_billing_subject.py`（15 个用例）——这是本阶段最烧脑的一块，把这张表抄全：

| 测试 | 场景 | 结果 |
| --- | --- | --- |
| `test_header_overrides_key_and_account` | 请求头指定主体 | 头优先级最高 |
| `test_key_binding_used_when_header_absent` | 无头 | 用 Key 绑定的 |
| `test_unbound_key_uses_account_default_personal` | 都没有 | 用账户默认 |
| `test_header_other_user_forbidden` | 头里写别人的个人钱包 | **403** |
| `test_header_org_non_member_forbidden` | 头里写非成员组织 | **403** |
| `test_key_org_cannot_consume_fallback_personal` | Key 绑组织但无消费权 | **回退个人** |
| `test_key_org_cannot_consume_fallback_deny` | 同上但组织配了 deny | **402** |

**看出规律了吗**：显式指定（header）出错 → 403 硬拒；隐式来源（Key/账户默认）出错 → 尝试回退。为什么这样设计？（提示：用户明确说了要用 A 却给他用 B，是更严重的错误）

`tests/test_member_quota.py`：重点 `test_quota_check_stricter_of_balance_and_quota`（`:164`）、`test_concurrent_consume_does_not_exceed_limit`（`:245`）、`test_unlimited_quota_limit_zero`（`:546`）。

`tests/test_payment_orders.py`：重点 **`test_callback_three_times_credits_wallet_once`**（`:147`）——这一条就是「幂等」的全部意义。

---

### D2 · `wallet_service.py` 现金主线（模式 Q，60 分钟）

阶段 4 你已经读过 hold/capture/release，这次补齐另外三块。

#### 2.1 余额模型（先确认这件事）

**`wallets.balance` 是实时字段，不是流水求和。**

每次变更的顺序永远是：先改 `wallet.balance` → 再 `_append_transaction`（`:82-118`）追加一条流水，流水里带 `balance_after = wallet.balance` 的**快照**。

这带来一个重要能力：**对账**。如果有人绕过代码直接 UPDATE 了 wallets 表，最新一条流水的 `balance_after` 就对不上当前 `balance` 了。这就是任务 1 要写的 SQL。

#### 2.2 充值与调账

| 函数 | 行号 | 说明 |
| --- | --- | --- |
| `recharge` | `203-252` | `balance += amount`，写 `type=recharge` 流水，用 `ref_request_id` 幂等 |
| `adjust` | `254-286` | 后台调账，正负都行，但**不能让 balance 变负**（`:270-273`） |

问题：

1. `recharge` 的幂等键是 `ref_request_id`。支付回调传进来的是什么？（去 `payment_service.py:43-45` 看 `payment:{order_no}`。为什么用订单号而不是随机 id？）
2. `adjust` 允许负数，那它和「扣费」有什么区别？为什么扣费不能用 adjust 实现？

#### 2.3 并发控制（本节重点）

只有**一种**机制：**行锁**。

```python
_get_wallet_for_update   # :45-58   SELECT ... FOR UPDATE
```

被 `recharge:230` / `adjust:269` / `hold:314` / `capture:380` / `release:487` 全部调用。

**注意项目里没有用** `UPDATE ... WHERE balance >= x` 这种条件更新。所以防超扣是三件东西合力：

```
行锁（串行化同一钱包的并发）
  + 应用层检查 available < amount → 402   (:316-322)
  + 数据库 CheckConstraint balance >= 0    (wallet.py:40)
```

问题（认真想，这是资金安全的核心）：

1. 只有应用层检查、没有行锁，会发生什么？（画一个两个请求交错执行的时序）
2. 有了行锁，为什么还要数据库 CheckConstraint？（提示：谁能绕过应用层？）
3. 行锁锁到什么时候释放？（事务提交时。那阶段 4 的 commit ① 在 `chat.py:203`，意味着锁只持有很短一段时间——**如果不 commit 就去调上游，锁会持有几十秒**。现在你彻底懂 commit ① 的必要性了）
4. 跑一下 `pytest tests/test_wallet_billing.py::test_concurrent_hold_no_negative_balance -v`，读它的代码，看它怎么构造并发。

#### 2.4 `available` 的三个口径（容易搞混，列出来记）

| 场景 | 公式 | 位置 |
| --- | --- | --- |
| hold 前校验 | `gift_balance + balance + credit_limit` | `wallet_service.py:316-317` |
| 组织额度检查 | `balance + credit_limit`（**不含赠金**） | `quota_service.py:206` |
| API 返回 `total_available` | `gift_balance + balance + credit_limit` | `schemas/admin.py:345-347` |

第二行和另外两行不一致——这是有意的还是遗漏？（留个问号，阶段 6 回来看）

---

### D3 · `billing_subject_service.py`（模式 Q，50 分钟）

**「这次调用的钱从谁的钱包出」**，这个决策比想象中复杂。

#### 3.1 决策优先级（`select_candidate`，`:115-131`）

```
1. 请求头 X-Billing-Subject     ← 最高
2. api_keys.billing_subject     ← Key 绑定
3. users.default_billing_subject ← 账户默认
4. "personal"                    ← 兜底
```

主体字符串的四种格式（`parse_billing_subject_ref`，`:60-101`）：`personal` / `user:{uuid}` / `personal:{uuid}` / `org:{uuid}`。

#### 3.2 组织分支的完整决策树（`_resolve_org`，`:199-273`）

这是本阶段最需要画图的一段。自己画一棵树，包含这些分支：

```
指定了 org:{uuid}
├─ 不是成员 / 成员停用 / can_consume_org_wallet=False
│   ├─ 来源是 header（显式）   → 403 ForbiddenError          :259-265
│   └─ 来源是 Key/默认（隐式） → 走回退                       :267-273
├─ 组织不存在                  → 强制回退个人                 :288-289
└─ 可以消费，但额度/余额不足
    └─ 看 org.billing_fallback
        ├─ "personal" → 回退个人钱包，标记 fell_back_to_personal  :291-301
        └─ "deny"     → 402 InsufficientBalanceError              :303-306
```

问题：

1. 为什么显式指定失败要 403、隐式失败要回退？（D1 已经问过一次，现在从代码里确认）
2. `fell_back_to_personal` 这个标记有什么用？（用户以为在花公司的钱，实际花了自己的——这件事需要被记录下来吗？去 grep 一下这个标记最后去了哪）
3. `resolve` 传了 `estimated_amount=hold_amount`（`chat.py:191`）。也就是说**主体决策依赖预估金额**。为什么？（因为「组织额度够不够」要拿金额去比）

---

### D4 · `quota_service.py`（模式 Q，40 分钟）

成员额度是「组织里每个人每月最多花多少」。三条约定要先记住：

- **没有 `member_quotas` 行 = 不限制**
- **`quota_limit == 0` = 不限制**（不是"一分钱都不能花"，注意这个反直觉设计，`:144-145`、`:250-251`）
- 额度按周期重置，用的是**惰性重置**：不跑定时任务，而是在读取时发现跨周期了就归零（`apply_lazy_period_reset`，`:121-140`）

**惰性重置值得单独想 5 分钟**：为什么不用定时任务在每月 1 号把所有人的 `quota_used` 清零？（提示：10 万个成员，定时任务要更新 10 万行，而且跑挂了怎么办；惰性重置是「谁用到谁重置」，天然分摊、天然幂等）

核心两个函数：

| 函数 | 行号 | 做什么 |
| --- | --- | --- |
| `quota_check` | `189-226` | 检查时取 **min(成员剩余额度, 组织可用余额)** —— 两个约束取更严的 |
| `quota_consume` | `228-263` | capture 之后累加 `quota_used`，超额抛 `InsufficientBalanceError`（`:256-258`），带 `FOR UPDATE`（`:247-248`） |

调用点在阶段 4 见过：`chat.py:240-245` 的 `_settle_charge` 里，**只有组织主体且 charge>0 才调**。

问题：`quota_consume` 在 capture **之后**调用。如果 quota_consume 抛异常了，capture 已经扣的钱会回滚吗？（去看它们在不在同一个事务里——`chat.py:231-245` 用的是同一个 session，commit 在 `:444`。所以会一起回滚。那用户会收到什么？）

---

### D5 · 组织域（模式 S + Q，40 分钟）

S 模式扫 `app/api/orgs.py` 的路由表：

| Method | Path | 行号 | 最低权限 |
| --- | --- | --- | --- |
| POST | `/orgs` | `45-57` | 任意登录用户 |
| GET | `/orgs` | `60-72` | 登录用户（只看自己的） |
| GET | `/orgs/{id}` | `75-86` | Member |
| PATCH | `/orgs/{id}` | `89-102` | **Owner** |
| GET | `/orgs/{id}/wallet` | `105-111` | Admin |
| GET | `/orgs/{id}/wallet/transactions` | `114-138` | Admin |
| GET | `/orgs/{id}/reports/daily` | `141-163` | Member |
| GET | `/orgs/{id}/reports/members` | `166-187` | Admin |
| GET/POST | `/orgs/{id}/members` | `191-213` | Member / Admin |
| PATCH/DELETE | `/orgs/{id}/members/{mid}` | `216-267` | Admin |

权限用三个依赖分级实现（`app/api/org_deps.py`）：`get_org_member_access`（`:17-26`）/ `get_org_admin_access`（`:29-38`）/ `get_org_owner_access`（`:41-50`），底层是 `org_service.role_rank`（`:37-38`）owner(3) > admin(2) > member(1)。

Q 模式只精读一个函数：`org_service.create_org`（`:98-127`）。注意 `:120`——**创建者是 Owner，但 `can_consume_org_wallet` 默认是 `False`**。

这是一个非常值得学的默认值设计：老板建了公司账户，默认自己也不能拿它花钱，必须显式打开。想想这个默认值挡住了什么事故。

---

### D6 · 支付（模式 Q，70 分钟）

#### 6.1 抽象层（`app/payments/base.py`，10 分钟）

三个方法：

```python
create_payment(...)  -> CreatePaymentResult(external_order_no, pay_url, raw)   :52-64
verify_callback(...) -> CallbackVerificationResult(..., apply: bool)           :66-73
query_order(...)     -> QueryPaymentResult(status: paid/pending/failed/expired) :75-82
```

`CallbackVerificationResult.apply`（`:19-30`）这个字段值得注意：验签通过了，但**这个事件可能和我们无关**（Stripe 会推很多种事件）。看 `stripe.py:257-267` 怎么用它。

#### 6.2 状态机（`app/services/payment_service.py`）

```
created ──┬─→ paid      （终态）
          ├─→ expired   （终态）
          └─→ failed    （终态）
```

合法流转表在 `payment_service.py:33-40`。

**回调处理的完整分支**（`_apply_verified_callback`，`:333-388`）——逐行读这 55 行，它是本阶段最该抄进笔记的一段：

| 分支 | 行号 | 处理 |
| --- | --- | --- |
| 拿行锁 | `340` | `FOR UPDATE` |
| 已经是 `paid` | `347-349` | **直接返回，幂等** |
| 已经是 `expired`/`failed` | `351-355` | `ConflictError`，**不入账** |
| `created` 但已超时 | `357-361` | 标 `expired`，`ConflictError` |
| 回调说失败 | `363-368` | 标 `failed` |
| **金额对不上** | `370-373` | `InvalidRequestError` |
| 成功 | `375-386` | 标 `paid` + `wallet_service.recharge` |

「金额对不上就拒绝」这一条特别重要——想想没有它会怎样。

#### 6.3 幂等的四层（**本阶段核心概念**）

```
1. 支付单终态检查      payment_service.py:347-349   已 paid 直接返回
2. 支付单行锁          payment_service.py:160-172   并发回调串行化
3. recharge 的幂等键   payment_service.py:43-45     ref_request_id = "payment:{order_no}"
4. 数据库唯一索引      wallet.py:79-86              (wallet_id, ref_request_id, type)
```

问题：第 1 层就够了吗？为什么还要第 3、4 层？（提示：想象两个回调**同时**到达，都在第 1 层看到 status 还是 `created`）

这和阶段 4 的 hold/capture 幂等是**同一套模式**。看出来了就说明你抓到了这个项目的设计范式。

#### 6.4 本地跑通一次充值（20 分钟，动手）

Mock 支付的签名算法（`app/payments/mock.py:22-33`）：

```
message = f"{order_no}|{external_order_no}|{int(success)}|{amount}"
sign    = HMAC-SHA256(key=settings.mock_payment_secret, message).hexdigest()
```

secret 默认 `"mock-payment-secret"`（`config.py:71-72`，可用 `MOCK_PAYMENT_SECRET` 覆盖）。

完整流程：

```powershell
# 1. 创建支付单（需要用户 JWT）。amount 是最小货币单位，9999 ≈ 1 元
curl.exe -X POST http://localhost:8000/payments `
  -H "Authorization: Bearer <你的JWT>" -H "Content-Type: application/json" `
  -d '{\"amount\":9999,\"target_type\":\"personal\",\"provider\":\"mock\"}'
# 记下响应里的 order_no 和 external_order_no
```

```powershell
# 2. 算签名（在激活了 venv 的终端里）
python -c "from app.payments.mock import mock_sign_payload; print(mock_sign_payload(order_no='<order_no>', external_order_no='<external>', success=True, amount=9999))"
```

```powershell
# 3. 回调（不需要鉴权——想想为什么，以及签名在这里承担了什么职责）
curl.exe -X POST http://localhost:8000/payments/callbacks/mock `
  -H "Content-Type: application/json" `
  -d '{\"order_no\":\"<order_no>\",\"external_order_no\":\"<external>\",\"success\":true,\"amount\":9999,\"sign\":\"<签名>\"}'

# 4. 再发一次一模一样的回调！观察钱包只加了一次
# 5. 把 amount 改成 8888 重算签名再发一次，观察被拒绝
```

第 4、5 步不要跳，这是你亲手验证幂等和金额校验的机会。

---

### D7 · 对账任务（模式 Q，50 分钟）

**这一节回答本阶段的终极问题：代码崩在半路，钱怎么办。**

#### 7.1 `app/tasks/reconcile_holds.py`

```powershell
python -m app.tasks.reconcile_holds --dry-run          # 先看不改
python -m app.tasks.reconcile_holds --expire-seconds 60 --limit 500
```

**它找什么**（`find_stale_holds`，`:56-98`）：

```sql
-- 伪代码
type = 'hold' AND ref_request_id IS NOT NULL
  AND 不存在同 (wallet_id, ref_request_id) 的 capture
  AND 不存在同 (wallet_id, ref_request_id) 的 release
  AND created_at < now - expire_seconds     -- 默认 settings.hold_expire_seconds = 3600
```

翻译成人话：**「预扣了，但既没实扣也没释放，而且已经过了一小时」的悬挂冻结**。

**它怎么处理**（`reconcile_stale_holds`，`:135-273`）：

| 情况 | 处理 | 行号 |
| --- | --- | --- |
| 有对应的 `usage_log` | 说明调用其实成功了，只是结算没写完 → **补 capture**，金额用 `usage.charge_amount` | `189-229` |
| 没有 `usage_log` | 说明调用没成功 → **全额 release** | `230-250` |
| `quota_consume` 补不上 | 只记日志，不阻断资金对账 | `209-215` |
| 单条处理异常 | 计数 + rollback，继续下一条 | `251-260` |

**这就是阶段 4 演练 D 加分题里那笔"悬在半空的钱"的归宿。**

#### 7.2 `app/tasks/reconcile_payments.py`

```powershell
python -m app.tasks.reconcile_payments --dry-run
```

找什么：`status='created'` 且超过 `settings.payment_reconcile_stale_seconds`（默认 300 秒）的订单。

处理：**主动去问支付渠道这笔单到底成没成**（`query_order`），然后：

| 查回来的状态 | 处理 |
| --- | --- |
| `paid` | 标 paid + 幂等 recharge —— **补掉单** |
| `failed` | 标 failed |
| `expired` | 标 expired |
| `pending` | 保持不动 |

「掉单」是支付领域的经典问题：用户付了钱，但回调因为网络问题没到。**光有回调是不够的，必须有主动查询兜底**——这是本阶段最该记住的一条工程经验。

问题（写进笔记）：

1. 两个任务都支持 `--dry-run`。为什么资金相关的任务**必须**有 dry-run？
2. 两个任务都是「单条出错不影响其他条」（rollback 后继续）。这个设计对不对？如果改成「一条出错就全部回滚」会怎样？
3. `reconcile_holds` 默认阈值 1 小时。设成 1 分钟会有什么风险？（提示：正在进行中的长请求）
4. 这两个任务当前是怎么被调度的？（去找找有没有 crontab / 调度器配置。如果没有，说明什么？）

---

## 二、怎么做

### 任务 1 · 手工对账（必做，约 60 分钟）

先制造数据：充值一次（用 D6.4 的 mock 流程）+ 调用两三次 chat。

然后跑这三条 SQL，**每条的结果和你的解读都写进笔记**。

#### SQL 1：快照一致性（最强的一致性检查）

```sql
-- 每个钱包的「最后一条流水的 balance_after」应该等于当前 balance
-- 有输出 = 有钱包被绕过代码改过，或者有 bug
SELECT w.id, w.owner_type, w.balance AS now_balance,
       t.balance_after AS last_snapshot, t.type, t.created_at
FROM wallets w
LEFT JOIN LATERAL (
    SELECT balance_after, type, created_at
    FROM wallet_transactions
    WHERE wallet_id = w.id
    ORDER BY created_at DESC, id DESC
    LIMIT 1
) t ON true
WHERE w.balance IS DISTINCT FROM t.balance_after;
```

#### SQL 2：现金守恒（「流水求和 == 余额」的正确写法）

```sql
-- 注意：hold/capture/release 会在 balance 和 held 之间搬钱，
-- 所以要校验的是「balance + held」这个总量，不是 balance 单独
-- 阶段 5 只看现金，用 (amount - gift_amount) 剥掉赠金部分
SELECT w.id,
       w.balance + w.held AS cash_total_now,
       COALESCE(SUM(wt.amount - wt.gift_amount), 0) AS cash_net_from_txs
FROM wallets w
LEFT JOIN wallet_transactions wt ON wt.wallet_id = w.id
GROUP BY w.id, w.balance, w.held
HAVING w.balance + w.held <> COALESCE(SUM(wt.amount - wt.gift_amount), 0);
```

**在跑之前先预测结果，再跑**。如果你预测「应该没有输出」而实际有输出，先别怀疑代码，先想想是不是有 `credit_limit` 透支或者阶段 4 演练留下的悬挂 hold。

#### SQL 3：单次调用的资金轨迹

```sql
SELECT type, amount, balance_after, held_after, ref_request_id, created_at
FROM wallet_transactions
WHERE wallet_id = '<你的钱包id>'
ORDER BY created_at DESC
LIMIT 20;
```

对着结果回答：

- [ ] 找出一组 `hold` + `capture`，两个 amount 分别是多少？为什么不一样？
- [ ] 有没有第三条 `release`？它是从哪来的？（`wallet_service.py:443-455`）
- [ ] `recharge` 那条的 `ref_request_id` 是什么格式？

### 任务 2 · 读透一个对账任务并写出异常清单（必做，约 90 分钟）

选 `reconcile_holds.py`（推荐，和阶段 4 联动最紧）。

#### Step 1：制造一笔悬挂的 hold

最简单的办法（复用阶段 4 演练 D 的加分题）：发起流式请求，**在第一个 chunk 到达之前**就掐断。或者更可控的做法——在 `chat.py:413`（`_settle_charge`）打断点，请求命中后**直接停掉调试进程**。

```sql
-- 确认它悬挂了：有 hold，没有 capture 也没有 release
SELECT ref_request_id, type, amount, created_at
FROM wallet_transactions
WHERE ref_request_id = '<request_id>' ORDER BY created_at;
```

#### Step 2：跑 dry-run 看它认不认得

```powershell
cd d:\LLM_TokenHub\backend
python -m app.tasks.reconcile_holds --expire-seconds 1 --dry-run
```

（`--expire-seconds 1` 是为了不用等一小时。**注意这在生产是危险的**，想想为什么。）

#### Step 3：真跑，验证钱回来了

```powershell
python -m app.tasks.reconcile_holds --expire-seconds 1
```

```sql
-- 现在应该多了一条 release（或 capture，取决于有没有 usage_log）
SELECT type, amount, balance_after, held_after FROM wallet_transactions
WHERE ref_request_id = '<request_id>' ORDER BY created_at;
```

#### Step 4：写异常场景清单（本任务真正的产出）

在笔记里列一张表，**每一行必须有代码行号依据**：

| # | 异常场景 | 任务怎么处理 | 代码位置 | 处理后果 |
| --- | --- | --- | --- | --- |
| 1 | 预扣了，服务在 capture 之前崩了，但上游其实成功了 | | | |
| 2 | 预扣了，上游失败，release 之前崩了 | | | |
| 3 | 悬挂 hold 的 `ref_request_id` 是空的 | | | |
| 4 | 补 capture 时组织额度累加失败 | | | |
| 5 | 处理某一条时抛异常 | | | |
| 6 | 任务本身跑到一半崩了，下次再跑 | | | |

第 6 行需要你自己推理：这个任务**可重入**吗？依据是什么？（提示：它调的是 `wallet_service.capture` / `release`，而那两个函数是幂等的）

然后回答一个开放题：**这个任务自己也可能出错。谁来监控它？**（去 grep `logger` 看它输出了什么；再想想需不需要一个告警）

### 任务 3 · 新增 `GET /wallets/me/statement`（必做，约 4 小时）

需求：月度对账单，按月汇总充值和消费。

#### Step 0：开分支

```powershell
cd d:\LLM_TokenHub
git checkout -b learn/stage5-statement
```

#### Step 1：先做五个决定（写进笔记，这一步比写代码重要）

1. **哪些流水算「充值」、哪些算「消费」？**先把 `type` 的取值列全（`wallet_service.py:24-31`）：

| type | 算什么 |
| --- | --- |
| `recharge` | 充值 |
| `capture` | **消费**（amount 是负数） |
| `adjust` | ？正数算充值还是单列一行？ |
| `hold` | ？ |
| `release` | ？ |
| `gift_*` | 阶段 6，先排除 |

   `hold` 和 `release` 是**中间态**——一次调用会产生 hold（-100）然后 capture（-60）+ release（+40）。如果你把它们都算进去，总数会完全错。想清楚只统计哪些 type。
   参考现有做法：`usage_daily_service.py:181-184` 的日聚合**只统计 `type = capture`**，收入取 `-amount`。跟着这个口径走。

2. **按什么时区分月？**这是个真问题。现有代码里：
   - 用户流水查询用 **UTC**（`api/wallets.py:46-47`）
   - 组织报表明确标注 `timezone="UTC"`（`api/orgs.py:158`）
   - 但配置里有 `settings.business_timezone = "Asia/Shanghai"`（`config.py:57`），赠金到期用的是它（`api/admin/wallets.py:280-302`）
   - 工具函数在 `app/core/datetime_utils.py`：`business_day_end_utc`（`:24-35`）、`business_day_bounds_utc`（`:38-48`）

   对账单给中国用户看，1 月 1 日 07:00（北京时间）的消费应该算 1 月还是 2024 年 12 月？**选一个并在响应里标注 `timezone` 字段**（照抄 `orgs.py:158` 的做法）。选 UTC 更省事、选业务时区更符合用户直觉——写下你的选择和理由。

3. **入参怎么设计？**`?months=6` 返回最近 6 个月，还是 `?from=2026-01&to=2026-06`？（提示：看现有接口的风格，`api/wallets.py:42-49` 用的是 `from`/`to`）

4. **要不要限制返回条数？**用户注册 3 年了，不加限制就是 36 行——可以接受吗？如果是日对账单呢？

5. **Schema 放哪？**现有约定：
   - 用户端钱包接口复用 `app/schemas/admin.py` 的 `WalletOut` / `WalletTransactionOut`（`api/wallets.py:11`）
   - 报表类响应在 `app/schemas/report.py`（`api/orgs.py:29`）
   对账单更像报表，建议 `schemas/report.py`。写下你的选择。

#### Step 2：SQL 聚合怎么写

参考现成的两段：

```159:192:backend/app/services/usage_daily_service.py
    agg_stmt = (
        select(
            ...
            func.coalesce(func.sum(-WalletTransaction.amount), 0).label("revenue_amount"),
            ...
        )
        ...
        .where(
            WalletTransaction.type == TX_CAPTURE,
            WalletTransaction.created_at >= day_start,
            WalletTransaction.created_at < day_end,
            ...
        )
        .group_by(...)
    )
```

按月分组用 `func.date_trunc('month', ...)`。如果选了业务时区，就是：

```python
func.date_trunc("month", func.timezone(settings.business_timezone, WalletTransaction.created_at))
```

**一次查询就够**：`GROUP BY 月份, type` 然后在 Python 里把结果整理成每月一行。不要写成「循环 12 个月，每月查一次」——那是 12 次数据库往返。

#### Step 3：放哪一层

`api/wallets.py` 现在的三个路由都很薄，聚合逻辑不该塞进路由。两个选择：

- 加到 `wallet_service.py`（那里已经有 `get_transactions`，`:536-565`）
- 加到 `report_service.py`（那里已经有各种聚合，`:304-314` 有 `_tx_sum` 可以参考）

写下你的选择和理由。（提示：这是「按钱包查流水」还是「出一张报表」？）

#### Step 4：测试

至少四个用例：

```python
async def test_statement_requires_jwt(user_client): ...        # 401

async def test_statement_sums_recharge_and_consume(user_client):
    # 充值 10000 → 调用消费 → 断言当月 recharge_total 和 consume_total
    ...

async def test_statement_excludes_hold_and_release(user_client):
    # 造一笔 hold + capture + release，断言总数只算 capture
    # ← 这条是本练习的核心考点
    ...

async def test_statement_empty_month_returns_zero_or_absent(user_client):
    # 没有流水的月份，返回 0 还是不返回这一行？你在 Step 1 决定的
    ...
```

第三条一定要写。它验证的正是 Step 1 里最容易搞错的判断。

#### Step 5：跑通 + review

```powershell
cd d:\LLM_TokenHub\backend
pytest -q
```

```
请 review 这个 diff。重点看：
1. 月度汇总的 type 口径是否和 usage_daily_service 一致
2. 时区处理是否有边界 bug（月初月末）
3. 是否有 N+1 查询
4. 金额单位是否搞错（最小单位 vs 元）
不要帮我改代码，指出问题即可。
```

```powershell
git checkout main
```

---

## 三、过关自测

1. 一笔钱从「充值」到「被一次调用消耗」，在 `wallet_transactions` 里留下几条记录？每条的 `type` 和 `amount` 正负分别是什么？（D2 / 任务 1）
2. 说出至少**三个**本项目为资金安全做的设计，并各给一处代码位置。（全部）
3. 用户付了钱但回调没到，系统靠什么把钱补上？（D7.2）
4. 预扣了但服务崩了没结算，会发生什么？多久之后被谁收拾？（D7.1）
5. 同一个支付回调发 3 次，为什么只入账一次？说出四层防护。（D6.3）
6. 一个 Key 绑定了组织 A，但这个用户在 A 里没有消费权限。发起调用会怎样？如果是通过请求头指定的 A 呢？（D3.2）
7. `quota_limit = 0` 表示什么？（D4）
8. 只有应用层的余额检查、没有行锁，会出什么事故？画一个时序说明。（D2.3）

---

## 四、这个阶段常见的坑

- **一头扎进赠金逻辑**：本阶段最大的时间黑洞。遇到 `gift_*` 就跳过，阶段 6 专门学。
- **把 `hold` 当成消费**：它是中间态。写对账单时如果把 hold 算进消费，数字会翻倍还带负数。
- **金额单位搞错**：`amount = 9999` 不是 9999 元，是 0.9999 元（`money_scale = 10000`）。展示给用户时要除以 `settings.money_scale`。
- **以为 `adjust` 可以用来扣费**：不行，它不走 hold/capture，也不进用量统计，只是后台调账。
- **在生产用 `--expire-seconds 1` 跑 reconcile_holds**：会把正在进行中的长请求的冻结当成悬挂给释放掉，然后调用成功时 capture 一笔已经被释放的钱。想清楚这个场景。
- **对账 SQL 只查 `balance` 不查 `held`**：hold 期间钱在 `held` 里，只看 `balance` 会以为钱少了。
