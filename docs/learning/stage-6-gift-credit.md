# 阶段 6 实施细则：赠送余额与双资金池——改造资金主链路

> 配套 `docs/learning_plan.md` 的「阶段 6」。阅读模式字母见 [README](./README.md#二四种读代码的方式后面每条读单都会标)。
> 前置：阶段 4（hold/capture/release）+ 阶段 5（钱包、幂等、对账）都已过关。**没过关不要开始这一阶段**，两池混着读是读不懂的。
> 产出物：`docs/learning/notes/stage-6.md`，含 1 张手算终态表 + 1 份断点四字段轨迹 + 3 条不变式 SQL 结果 + 1 份逆向思考题答案。

**本阶段一句话**：阶段 5 你学的是「钱怎么进出一个池子」，这一阶段是「钱包被劈成两个池子，而且**上层 5 个调用方一行没改**」。这是全项目风险最高的一段代码（写错就是真实资损），也是「如何安全改造一条已上线的资金链路」的最佳样本。

---

## 零、先建立三个前提认知（10 分钟，先读这一节再动代码）

### 0.1 钱包现在有四个金额字段，不是两个

```
现金池：balance（可用）      + held（预扣中）        ← 用户充值来的，可退可提
赠送池：gift_balance（可用） + gift_held（预扣中）   ← 平台送的，不可退不可提
```

字段定义在 `app/db/models/wallet.py:48-56`，四个都有 `>= 0` 的 CheckConstraint（`:40-43`）。

**`balance` 的语义被收窄了**：它以前表示「全部余额」，现在只表示现金池。所有以前写 `wallet.balance` 的地方，含义都悄悄变了——这是本阶段最需要警惕的一件事。

### 0.2 赠金为什么不能只加一条备注流水

这是整个专项的设计起点。假设我们偷懒，只在充值时写一条 `remark="赠送"` 的流水，会同时崩三件事：

| 崩掉的能力 | 为什么 |
| --- | --- |
| 退款上限 | 用户有 500 赠金 + 100 现金，来申请退款。混在一起你根本不知道能退多少 |
| 收入口径 | 赠金消费是**营销费用**不是收入。混在一起，报表里的收入是虚的 |
| 过期能力 | 赠金要能过期回收。混在一起你不知道该收哪一部分 |

所以必须**物理隔离**：钱包上单独两个字段，外加一张 `gift_grants` 批次表。

先精读 `docs/prd/wallet.md#赠送余额gift-credit` 的这三小节：「为什么必须物理隔离」「扣减优先级」「跨池分摊示例」。**读完在笔记里用自己的话写一遍这张表**，不要抄。

### 0.3 一句话记住扣减顺序

```
赠送池 → 现金池 → 授信额度
```

理由很朴素：**先花不能退的**。用户随时可能来退款，平台希望退款时剩下的是现金而不是赠金。

---

## 一、怎么读

### 读单总览

| # | 内容 | 模式 | 预算 | 核心问题 |
| --- | --- | --- | --- | --- |
| D1 | PRD + 口径变更文档 | Q | 50 min | 为什么这么设计 |
| D2 | `test_gift_billing.py` | T | 60 min | 两池的行为契约 |
| D3 | `GiftGrant` 模型 + 迁移 013 | Q | 40 min | 批次表长什么样、存量怎么回填 |
| D4 | `gift_service.py` | Q | 60 min | 批次账本四原语 |
| D5 | `wallet_service` 跨池拆分 | Q | **90 min** | **本阶段核心** |
| D6 | 后台赠送/撤回 API | Q | 40 min | 风控、幂等、审计 |
| D7 | 用量与报表拆分 | Q | 40 min | 收入口径怎么落地 |
| D8 | 两个赠金任务 | Q | 40 min | 过期与对账 |

---

### D1 · PRD 与口径变更（模式 Q，50 分钟）

先读 `docs/GIFT_CALIBER_CHANGE.md` 第 1 节那张对照表，再读 `docs/prd/billing.md` 的「赠送余额抵扣与收入口径」。

带着这三个问题读：

1. **同一笔 100 元，充值进来和赠送进来，在财务上有什么区别？**
   （提示：会计上预付款是**负债**——用户随时能要回去；赠金是**营销费用**——公司花掉的钱。所以赠金消费不能记成收入，它只是「把已经花掉的营销费用兑现掉」）
2. **`revenue_amount` 和 `cash_revenue_amount` 分别是什么？** 哪个才是平台真实收入？
   （答案在 `app/services/report_service.py:6-9` 的模块 docstring，读完抄进笔记）
3. **一笔纯赠金消费，毛利为什么是负数？** 这是 bug 吗？
   （毛利 = 现金收入 − 上游成本。纯赠金消费：现金收入 0，成本照付。所以负数是**预期**——平台确实在这笔调用上净支出。代码依据 `report_service.py:136`）

---

### D2 · 先读测试（模式 T，60 分钟）

`backend/tests/test_gift_billing.py` 是本阶段最好的入口，因为它把两池的行为写成了可执行的契约。

先跑一遍看着它绿：

```powershell
cd d:\LLM_TokenHub\backend
pytest tests/test_gift_billing.py -v
```

然后挑这 6 组精读，把「初始 → 操作 → 终态」抄成表：

| 测试 | 行号 | 教你什么 |
| --- | --- | --- |
| `test_cash_only_hold_capture_release_matches_legacy` | `110` | **纯现金场景行为完全不变**——这是「零改动」承诺的证据 |
| `test_gift_covers_hold_when_cash_is_zero` | `200` | 现金为 0、赠金够，**不返回 402** |
| `test_prd_cross_pool_example` | `273` | **PRD 那个例子的完整断言，本阶段的标准答案** |
| `test_hold_capture_equals_direct_priority_charge` | `349` | **等价性参数化测试，12 组数据，本文件的灵魂** |
| `test_capture_consumes_grants_fifo_and_keeps_invariant` | `524` | FIFO 核销 + 不变式 |
| `test_release_does_not_consume_grants` | `570` | 失败释放**不核销批次** |

#### 重点看 D2 的等价性测试（`:331-386`）

参数表在 `:333-346`，12 组 `(gift, cash, hold_amount, actual)`：

```
(0, 1000, 100, 0)      纯现金，失败
(0, 1000, 100, 110)    纯现金，超预扣
(30, 1000, 100, 60)    ← 这就是 PRD 例子
(50, 0, 50, 30)        只有赠金
(10, 10, 20, 25)       两池都不够，吃授信
(80, 20, 50, 40)       赠金多于预扣
```

它断言的是一件很深刻的事：

> **先 hold 再 capture 的终态，必须等于「直接拿 actual 按赠送→现金优先级扣一次」的终态。**

参照实现是测试里的 `_direct_priority_charge`（一个 10 行的朴素函数）。**这就是重构资金链路的正确姿势**：不去逐行验证新代码，而是写一个「显然正确的朴素实现」当参照物，用参数化测试证明两者等价。

`backend/tests/test_gift_ledger.py` 用 T 模式扫一遍即可，重点标出 4 个：

| 测试 | 行号 | 教你什么 |
| --- | --- | --- |
| `test_grant_idempotent_same_key_no_double_credit` | `160` | 幂等键 |
| `test_grant_concurrent_same_key_one_batch` | `191` | **并发发放只产生一个批次** |
| `test_revoke_only_returns_remaining` | `277` | 撤回只退**没花掉**的 |
| `test_consume_fifo_expires_at_then_permanent` | `388` | FIFO 顺序：先花快过期的 |

---

### D3 · 批次表与迁移（模式 Q，40 分钟）

#### 3.1 `GiftGrant` 模型（`app/db/models/wallet.py:121-170`）

核心字段：

| 字段 | 行号 | 说明 |
| --- | --- | --- |
| `amount` / `remaining` | `151-152` | 发放总额 / 未消耗剩余 |
| `expires_at` | `154-156` | **NULL = 永久有效** |
| `status` | `157` | `active` / `used_up` / `expired` / `revoked` |
| `idempotency_key` | `167` | 幂等键 |
| `granted_by` / `revoked_by` / `revoked_at` | `158-166` | 审计字段 |

三个约束值得单独看：

```python
CheckConstraint("remaining >= 0 AND remaining <= amount")   # :130-133
CheckConstraint("status IN ('active','used_up','expired','revoked')")  # :138-141
UniqueConstraint("wallet_id", "idempotency_key")            # :142-144
```

问题：`remaining <= amount` 这条约束防的是什么事故？（提示：什么代码 bug 会让剩余额度**变多**？）

**最重要的一行是 docstring 里的不变式**（`:125`）：

```
Σ(status=active 的 remaining) == wallet.gift_balance + wallet.gift_held
```

**这一条是整个专项的资金正确性总开关**。把它抄进笔记，任务 3 要用 SQL 验证它。想一分钟：为什么右边要加 `gift_held`？（因为预扣期间钱从 `gift_balance` 搬到了 `gift_held`，但批次还没核销）

#### 3.2 `WalletTransaction` 的四个新字段（`:95-108`）

```python
gift_amount          # :95-97   其中赠送池部分，符号与 amount 一致
gift_balance_after   # :100-102 快照
gift_held_after      # :103-105 快照
gift_grant_id        # :106-108 关联批次（capture 时取 FIFO 第一个）
```

**注意 `gift_amount` 是 `amount` 的「其中之一部分」，不是并列项**。也就是说：

```
现金部分 = amount - gift_amount
```

阶段 5 任务 1 那条 SQL 里的 `SUM(wt.amount - wt.gift_amount)` 就是这么来的。现在你懂它了。

#### 3.3 迁移 `alembic/versions/013_gift_credit.py`

三段结构，逐段看：

| 段 | 行号 | 做了什么 |
| --- | --- | --- |
| 加字段 | `28-46` | wallets 两个新列 + 两个 CheckConstraint |
| 建表 | `47-110` | `gift_grants` + 索引 |
| 扩展流水/用量/日聚合 | `112-153` | 四张表加赠金拆分列 |
| **存量数据回填** | `155-172` | 见下 |

存量回填这三条 UPDATE 是本阶段最该学的迁移技巧（`:155-172`）：

```sql
UPDATE usage_logs         SET gift_charge_amount = 0,  cash_charge_amount = charge_amount
UPDATE usage_daily_summary SET gift_revenue_amount = 0, cash_revenue_amount = revenue_amount
UPDATE wallet_transactions SET gift_amount = 0, gift_balance_after = 0, gift_held_after = 0
```

**翻译成人话：历史数据全部标记为「纯现金」**。这样「gift + cash == total」这条不变式对**历史数据也成立**，报表不需要为新旧数据写两套逻辑。

问题：

1. 新列都用了 `server_default="0"` + `nullable=False`。为什么不能直接 `nullable=False` 不给 default？（提示：表里已经有几万行了）
2. `downgrade`（`:175-198`）是**真的能回滚的**。逐行看它的删除顺序：为什么先删 FK 约束再删列、最后才删表？
3. 如果 downgrade 之后已经有赠金消费发生了，回滚会丢什么数据？这个代价可接受吗？（这是真实迁移里必须回答的问题）

---

### D4 · `gift_service.py` 批次账本四原语（模式 Q，60 分钟）

整个文件只有 395 行，四个公开原语。**先读文件顶部 docstring（`:1-8`）**，它写了两条铁律：

```
1. 所有变更必须在持有对应 wallet 行锁的事务内进行
2. consume 只核销批次 remaining，不改 wallet.gift_balance（由调用方统一维护）
```

第 2 条是理解本文件的钥匙：**批次账本和钱包余额是两套账，分别由两个模块维护，靠不变式对齐。**

#### 4.1 `grant` 发放（`:46-123`）

幂等做了**三层**，逐层看：

| 层 | 行号 | 做什么 |
| --- | --- | --- |
| 1. 加锁前查一次 | `66-71` | 快路径，命中直接返回 |
| 2. **加锁后再查一次** | `73-80` | 拿到行锁后重查 |
| 3. 捕获 `IntegrityError` | `111-119` | 唯一索引兜底，再查一次 |

**第 2 层是重点**。为什么拿了行锁还要再查一次？画一个两个并发请求的时序：A 和 B 同时进第 1 层，都查不到 → A 拿锁写入提交 → B 拿锁。如果 B 不重查，就会写出第二个批次。这就是经典的 **check-then-act 竞态**，`test_grant_concurrent_same_key_one_batch`（`test_gift_ledger.py:191`）验证的正是这个。

第 3 层用了 `session.begin_nested()`（`:83`），也就是 SAVEPOINT。想想为什么这里不能直接让 `IntegrityError` 冒泡——（提示：整个事务会被标记为 aborted，后面什么都干不了）

发放动作本身（`:98-109`）：

```python
wallet.gift_balance += amount                      # 钱包侧
wallet_service._append_transaction(..., TX_GIFT_GRANT, gift_amount=amount,
                                   ref_request_id=f"gift:{idempotency_key}")
```

注意流水的 `amount` 和 `gift_amount` **都是 `amount`**——因为这笔钱 100% 是赠金。

#### 4.2 `revoke` 撤回（`:125-150`）与 `expire` 过期（`:152-169`）

两个都是 `_close_grant`（`:327-391`）的薄封装，区别只有三个参数：

| | `revoke` | `expire` |
| --- | --- | --- |
| 新状态 | `revoked` | `expired` |
| 流水类型 | `TX_GIFT_REVOKE` | `TX_GIFT_EXPIRE` |
| 已是终态时 | 抛 409 `gift_already_consumed` | **幂等返回**（`:350-351`） |

为什么撤回要报错、过期要幂等？（提示：撤回是**人**点的，重复点击应该被告知「已经撤过了」；过期是**定时任务**跑的，重复执行必须安静地不出事）

`_close_grant` 只做一件事，但要看清（`:358-388`）：

```python
remaining = locked.remaining      # 只动「没花掉的」
wallet.gift_balance -= remaining
locked.remaining = 0
locked.status = new_status
_append_transaction(amount=-remaining, gift_amount=-remaining)
```

**「只退未消耗」这四个字就是这两行代码**。已经花掉的部分不在 `remaining` 里，天然拿不回来。

还有一处防御值得学（`:359-370`）：撤回前检查 `wallet.gift_balance >= remaining`，不满足就 `logger.error("gift_ledger_mismatch")` + 抛 409。**账本对不上时宁可拒绝操作，也不要把余额写成负数。**

#### 4.3 `consume` FIFO 核销（`:171-215`）

排序规则在 `_lock_active_grants_fifo`（`:311-325`），只有一行是关键：

```python
.order_by(GiftGrant.expires_at.asc().nulls_last(), GiftGrant.created_at.asc())
```

翻译：**先花快过期的，永久有效的排最后，同期按发放时间**。

为什么？因为快过期的不花就作废了，对用户是净损失。`nulls_last()` 这个细节尤其要记住——如果写成默认的 `nulls_first`，Postgres 会把永久批次排在最前面先花掉，快过期的批次全部白白作废。**一个排序参数就是一次真实的用户损失。**

分配循环（`:203-211`）是纯整数减法，没有任何比例分摊：

```python
take = min(leftover, grant.remaining)
grant.remaining -= take
if grant.remaining == 0:
    grant.status = GRANT_STATUS_USED_UP
```

问题：为什么跨池、跨批次全部用整数加减，不做「按比例取整」？（提示：金额是最小货币单位的整数。任何取整都会产生 1 分钱的舍入误差，而资金账本里**误差是不允许存在的**——它会让不变式永久对不上）

最后看 `:186-199` 的守卫：批次剩余总额 < 要核销的金额时，记 `gift_ledger_mismatch` 错误日志并抛 `ConflictError`。**这就是不变式在运行时的第二道岗哨**（第一道是对账任务）。

---

### D5 · 跨池拆分：本阶段核心（模式 Q，90 分钟）

**这一节值得你花掉本阶段三分之一的时间。** 它回答一个问题：怎么在**不改方法签名**的前提下，把单池改成双池，让 5 个调用方一行都不用动。

打开 `app/services/wallet_service.py`，只看三个方法 + 两个辅助函数。

#### 5.1 拆分器 `_gift_first_split`（`:120-128`）

整个专项最核心的逻辑只有 5 行：

```python
gift_part = min(gift_available, amount)
return gift_part, amount - gift_part
```

「赠送优先」四个字就是这个 `min`。剩下的全部是围绕它的记账。

#### 5.2 `hold` 预扣（`:288-351`）

```python
available = wallet.gift_balance + wallet.balance + wallet.credit_limit   # :316
if available < amount: raise InsufficientBalanceError                    # :317-322

gift_hold, cash_hold = self._gift_first_split(amount, wallet.gift_balance)  # :324
wallet.gift_balance -= gift_hold;  wallet.gift_held += gift_hold            # :325-326
wallet.balance      -= cash_hold;  wallet.held      += cash_hold            # :327-328

_append_transaction(TX_HOLD, amount=-amount, gift_amount=-gift_hold)        # :331-340
```

三个必须注意的点：

1. **`available` 口径变了**（`:316`）。这就是「现金为 0、赠金充足时不返回 402」的直接原因。回头看阶段 5 D2.4 留的那个问号——`quota_service.py:206` 的组织额度检查**不含赠金**，那是有意的还是遗漏？（现在你有能力判断了：组织额度管的是「这个成员能花组织多少钱」，而赠金是平台送给组织的……写下你的结论）
2. **hold 阶段不碰 `gift_grants`**（docstring `:301` 明说）。批次核销推迟到 capture。为什么？（提示：预扣的金额是**估算**的，最终实扣可能更少。提前核销批次，退差额时就要「反核销」，那是一堆很难写对的代码）
3. **拆分结果被写进了流水的 `gift_amount`**（`:336`）。这是唯一的记录——capture 和 release 靠读它把拆分还原出来。

#### 5.3 拆分的还原：`_read_hold_split`（`:130-149`）

```python
hold_tx = await self._find_transaction(session, wallet_id, request_id, TX_HOLD)
if hold_tx is None:
    return 0, hold_amount          # 没有 hold → 退化为纯现金
gift_hold = -int(hold_tx.gift_amount)
```

**这是「零改动」的关键设计**：拆分信息不通过参数传递，而是**存在流水里**。所以 `capture(wallet_id, hold_amount, actual_amount, request_id)` 的签名一个字都没变，调用方（`chat.py`、`embeddings.py`、`kb_ingest_service.py`、`reconcile_holds.py`……）自然无需改动。

在笔记里回答：**如果当初选择「把 gift_hold 作为新参数传给 capture」，会带来什么后果？**（提示：5 个调用方全改；而且 `reconcile_holds` 这种「事后补结算」的场景根本拿不到这个值，它只有 request_id）

#### 5.4 `capture` 实扣：本专项最容易写错的一段（`:353-461`）

先记住整体节奏：

```
1. 幂等检查 → 2. 行锁 → 3. 读回拆分 → 4. 归还两池冻结计数
                                    → 5. 分两种情况算实扣与退还
                                    → 6. FIFO 核销批次 → 7. 写 capture(+release) 流水
```

**情况 A：实扣 ≤ 预扣（绝大多数）**（`:390-399`）

```python
release_amount = hold_amount - actual_amount
cash_release = min(cash_hold, release_amount)      # ← 先退现金
gift_release = release_amount - cash_release       # ← 再退赠送
gift_charge  = gift_hold - gift_release
cash_charge  = cash_hold - cash_release
```

**「按占用逆序退差额」就是 `min(cash_hold, release_amount)` 这一行。**

用 PRD 的例子走一遍（赠送 30、现金 1000，预扣 100、实扣 60）：

| 步骤 | gift_balance | gift_held | balance | held |
| --- | --- | --- | --- | --- |
| 初始 | 30 | 0 | 1000 | 0 |
| hold 100（拆 30 + 70） | 0 | 30 | 930 | 70 |
| capture 60 | 0 | 0 | **970** | 0 |

差额 40 全部从现金退（`cash_release = min(70, 40) = 40`），赠金 30 全额实扣。终态：`gift_charge=30, cash_charge=30`。

**为什么「按占用逆序退」等价于「用实扣金额重新按优先级分摊」？**

这是本阶段最该想明白的一句话。用 60 直接按赠送→现金优先级分摊：gift 取 `min(30, 60) = 30`，cash 取 30。结果一模一样。

原因：赠金优先意味着**赠金应该被尽可能多地花掉**。退钱时如果先退赠金，就等于「少花了赠金」，违反优先级。所以退还必须**从最后占用的池子开始退**，也就是现金。

这个等价性有 12 组参数化测试守着（`test_gift_billing.py:331-386`），**它是这段代码的安全网**。

**情况 B：实扣 > 预扣**（`:400-416`）

超出部分 `extra` 要额外扣，同样走赠送优先：

```python
gift_extra, extra_after_gift = self._gift_first_split(extra, wallet.gift_balance)  # :402
cash_room = max(wallet.balance + wallet.credit_limit, 0)                           # :403-405
cash_extra = min(extra_after_gift, cash_room)                                      # :406
if cash_extra > wallet.balance: cash_extra = wallet.balance                        # :408-409
```

`:408-409` 这两行单独看五分钟：**授信只放宽「可用判定」，不允许把 `balance` 写成负数**。因为数据库有 `balance >= 0` 的 CheckConstraint（`wallet.py:40`），写成负数会直接触发约束异常，整个事务炸掉。

**四个断言（`:418-421`）**：

```python
assert gift_charge >= 0 and cash_charge >= 0
assert gift_charge + cash_charge == charged
assert wallet.gift_balance >= 0 and wallet.gift_held >= 0
assert wallet.balance >= 0 and wallet.held >= 0
```

生产代码里出现 `assert` 通常是坏味道，但**这里是对的**：它们是不变式的运行时守卫，一旦拆分逻辑写错，立刻在开发/测试阶段炸掉，而不是安静地写出一笔错账。这是「资金代码可以比业务代码更偏执」的例子。

最后（`:423-430`）才调 `gift_service.consume` 核销批次，`gift_grant_id` 只记录 FIFO 的**第一个**批次（`:430`）——这是个刻意的简化，一笔消费跨多个批次时流水上只留第一个。想想这个简化损失了什么、以及为什么可以接受（提示：批次侧的 `remaining` 已经准确记录了，流水上的 `gift_grant_id` 只是给人看的线索）。

#### 5.5 `release` 全额释放（`:463-510`）

最简单的一个：读回拆分，原路退回，**不核销任何批次**。

```python
wallet.gift_held -= gift_hold;  wallet.gift_balance += gift_hold   # :492-493
wallet.held      -= cash_hold;  wallet.balance      += cash_hold   # :494-495
```

问题：为什么 release 不需要「逆序」？（因为它是全额退，两池各退各的，不存在分配问题）

#### 5.6 `get_charge_split`（`:512-534`）

从 capture 流水读回 `(gift_charge, cash_charge)`，供用量拆分使用。和 `_read_hold_split` 是同一个套路：**拆分只存一次（在流水里），谁需要谁去读**。

---

### D6 · 后台赠送与撤回 API（模式 Q，40 分钟）

`app/api/admin/wallets.py` 只看赠金那几段。

#### 6.1 路由表

| Method | Path | 行号 | 说明 |
| --- | --- | --- | --- |
| POST | `/admin/wallets/users/{user_id}/gifts` | `366-375` | 给个人钱包赠送 |
| POST | `/admin/wallets/orgs/{org_id}/gifts` | `378-387` | 给组织钱包赠送 |
| GET | `/admin/wallets/{wallet_id}/gifts` | `390-402` | 查批次列表 |
| POST | `/admin/gifts/{grant_id}/revoke` | `405-424` | 撤回 |
| GET | `/wallets/me/gifts` | `api/wallets.py:28-37` | **用户侧**只读自己的批次 |

#### 6.2 三层风控（`_grant_gift`，`:322-363`）

| 层 | 行号 | 做什么 |
| --- | --- | --- |
| 幂等短路 | `329-338` | 同 key 已存在直接返回，不再跑风控 |
| 单笔上限 | `285-296` | `settings.gift_max_amount_per_grant`（`config.py:59-62`） |
| 操作人日累计上限 | `299-319` | `settings.gift_max_amount_per_operator_daily`（`config.py:63-66`） |
| 审计日志 | `352-353` | `action="wallet.gift_grant"`，`target=gift_grant:{id}` |

**日累计上限是按业务时区算的**（`_check_daily_limit` 里用 `settings.business_timezone`）。想想为什么这里必须用业务时区而不是 UTC：运营在北京时间 08:00 发赠金，如果按 UTC 算，那还算「昨天」的额度——风控就被绕过了。

`sum_granted_by_operator`（`gift_service.py:227-242`）统计时排除了 `status = revoked` 的批次（`:240`）。问题：撤回过的批次不算进日额度，会不会被人利用来绕过风控？（发了撤、发了撤……想清楚攻击面，写下你的判断）

#### 6.3 赠送是独立权限资源，不是单独的 Depends

赠送和钱包写操作走同一个 `enforce_admin_permission`，靠 **URL 改判** 成独立的 `gift` 资源（不是再挂一个 Depends）：

- 普通钱包写操作（充值 / 调账）→ `wallet:manage`
- `/admin/wallets/.../gifts` 和 `/admin/gifts/.../revoke` → `gift:manage`（`admin_permissions.py` 的 `_RESOURCE_OVERRIDES`）

有钱包写权限不能顺带送赠金，测试依据是 `test_gift_is_separable_from_wallet`。

`:40-41` 还有一个和阶段 3 D6 对得上的细节：`gift_grants.granted_by` 外键指向前台 `users`，装不下 `admin_users.id`（`get_admin_actor_id` 因此仍返回 `None`）。所以无论是 `.env` 兜底超管还是库里的管理员，发放时都落到系统操作人占位账号 `system-gift-operator@internal.local`。**这是「外键约束遇上另一套用户表」的典型解法**，记下来，以后自己遇到会用。审计真正记到是哪位管理员，要等 `audit_logs` 和后台账号解耦。

---

### D7 · 用量与报表的拆分（模式 Q，40 分钟）

#### 7.1 `usage_service.py`

`_resolve_charge_split`（`:72-109`）：写用量日志前，去 capture 流水读拆分。

**看它的降级策略（`:75-80`、`:98-108`）**：

| 情况 | 处理 |
| --- | --- |
| 读不到 capture 流水 | 回退为全现金 `(0, charge_amount)` |
| 拆分之和 ≠ charge_amount | 记警告日志，回退为全现金 |

为什么「对不上就当全现金」是合理的降级？（提示：赠金记多了会让平台低估自己的收入，记少了会高估。**低估比高估安全**——财务上宁可保守。而且日志会告警，人会来查）

`write_usage_log`（`:112-150`）的 docstring 明写：`gift_charge_amount + cash_charge_amount` 恒等于 `charge_amount`（`:130`）。**这是第二条不变式**，任务 3 要用 SQL 验证。

`FUND_SOURCES = ("gift", "cash", "mixed")`（`:25`）和查询过滤（`:237-248`）：三种资金来源怎么用 SQL 表达？自己先想，再对答案：

```
gift  : gift > 0 AND cash == 0
cash  : cash > 0 AND gift == 0
mixed : gift > 0 AND cash > 0
```

#### 7.2 `report_service.py`

| 概念 | 位置 | 含义 |
| --- | --- | --- |
| `revenue_amount` | docstring `:6-9` | 计费总额（**含赠金**，不是真实收入） |
| `cash_revenue_amount` | 同上 | 平台真实收入 |
| `gross_profit` | `:136` | `cash_revenue - cost`，**基于现金** |
| `gift_dashboard` | `:289-352` | 赠金看板：敞口、发放/撤回/过期、消耗、核销率 |
| `reconcile_revenue` | `:220-286` | 聚合表 vs capture 流水对账 + 赠金拆分校验 |

`_gift_cost`（`:89-99`）做「赠金消费对应的上游成本」的整数比例分摊——**注意这里是允许比例分摊的**。为什么报表能用比例分摊、账本不能？（提示：报表是**统计口径**，差 1 分不影响决策；账本是**资金真相**，差 1 分就是不变式崩了）

`top_by_dimension` 按 `cash_revenue_amount` 降序（`:186`）。想想为什么排行榜要按现金而不是计费总额排——（一个全靠赠金消费的用户排在真实付费用户前面，会误导运营决策）

---

### D8 · 两个赠金任务（模式 Q，40 分钟）

#### 8.1 `app/tasks/expire_gifts.py` 过期回收

```powershell
python -m app.tasks.expire_gifts --dry-run     # 先看不改
python -m app.tasks.expire_gifts --limit 500
```

扫描条件（`gift_service.find_expired_grant_ids`，`:266-286`）：

```sql
status = 'active' AND remaining > 0 AND expires_at IS NOT NULL AND expires_at <= now
ORDER BY expires_at ASC LIMIT :limit
```

**本任务最该学的一段是 `:74-89`**：按钱包分组处理，`gift_held > 0` 就**整个钱包跳过本轮**。

代码里有一条 `ponytail:` 注释（`:77-78`）明确写了这个简化的代价和升级路径：

```
上限：过期最长延迟一个任务周期
升级路径：hold 阶段就核销批次 remaining，过期回收便不再与冻结抢钱
```

想清楚为什么必须跳过：钱包里有 30 赠金正在被一个进行中的请求冻结（`gift_held = 30`）。此时如果过期回收把批次 remaining 清零，这个请求 capture 时调 `gift_service.consume` 就会发现批次没钱了 → `ConflictError` → **一次正常调用被打断**。

跟着追问：**这个简化的代价是什么？** 一个高频调用的用户，钱包几乎永远有 `gift_held > 0`，他的赠金可能一直过期不掉。可接受吗？（提示：赠金对平台是负债，晚回收 = 多送一点，不是资损，只是营销费用略超。所以可接受）

错误隔离（`:113-124`）：CLI 场景按钱包 commit，单个钱包出错 rollback 后继续下一个。和阶段 5 那两个对账任务是同一个模式。

#### 8.2 `app/tasks/reconcile_gifts.py` 账本对账

```powershell
python -m app.tasks.reconcile_gifts    # 没有 --dry-run，因为它本来就不改数
```

**这个任务只告警，绝不自动修数**（docstring `:3`）。它校验两条：

| # | 不变式 | 代码 |
| --- | --- | --- |
| 1 | 按钱包：`Σ(active 批次 remaining) == gift_balance + gift_held` | `_find_wallet_mismatches`，`:120-158` |
| 2 | 全平台：`Σ usage_logs.gift_charge_amount == Σ capture 的 -gift_amount` | `:74-88` |

顺带刷新 `GIFT_BALANCE_TOTAL` 这个 Gauge（`:63-71`）——全平台未消耗赠金敞口，也就是**平台还欠着多少赠金没兑现**。

问题（认真回答，这是本阶段的价值观题）：

1. **为什么资金对账任务不能自动修数？** 阶段 5 的 `reconcile_holds` 是会自动补 capture / release 的，为什么这个不行？
   （提示：`reconcile_holds` 修的是「明确知道该怎么办」的悬挂状态；而账本不平意味着**有一段代码写错了**，你不知道错在哪、错了多少。自动「修」很可能是把证据抹掉）
2. 退出码：`return 0 if result.ok else 1`（`:180`）。这个设计是给谁看的？（cron / 监控系统——非 0 退出就告警）

---

## 二、怎么做

### 任务 1 · 手算再验证（必做，约 60 分钟，本阶段最重要的产出）

**先在纸上算，再跑测试对答案。顺序反了这个任务就白做了。**

#### Step 1：算 PRD 那个例子

初始：赠送池 30、现金池 1000、授信 0。预扣 100，实扣 60。

在笔记里填这张表（**不要看代码，用 D0.3 的优先级和 D5.4 的规则推**）：

| 时刻 | gift_balance | gift_held | balance | held |
| --- | --- | --- | --- | --- |
| 初始 | 30 | 0 | 1000 | 0 |
| hold 100 后 | | | | |
| capture 60 后 | | | | |

再填：`gift_charge = ___`，`cash_charge = ___`，release 流水的 `amount = ___`、`gift_amount = ___`。

#### Step 2：对答案

```powershell
cd d:\LLM_TokenHub\backend
pytest tests/test_gift_billing.py::test_prd_cross_pool_example -v
```

标准答案就在 `tests/test_gift_billing.py:284-317` 的断言里。**算错了不要跳过，回到 D5.4 找出你哪一步的规则用错了，写在笔记里。**

#### Step 3：再手算三组「等价性」参数

从 `:333-346` 的 12 组里挑这三组，它们各代表一类边界：

| 组 | `(gift, cash, hold, actual)` | 考点 |
| --- | --- | --- |
| A | `(50, 0, 50, 30)` | 只有赠金，实扣小于预扣 |
| B | `(10, 10, 20, 25)` | 两池都不够，实扣超预扣 |
| C | `(80, 20, 50, 40)` | 赠金比预扣还多 |

每组算出「终态四字段 + (gift_charge, cash_charge)」，然后：

```powershell
pytest tests/test_gift_billing.py::test_hold_capture_equals_direct_priority_charge -v
```

B 组特别值得琢磨：`gift=10, cash=10`，预扣 20（刚好花光两池），实扣 25 超了 5。这 5 从哪来？（`credit_limit` 是 0 的话……去看 `:403-409` 的 `cash_room` 处理，然后想想最终 charged 是多少）

### 任务 2 · 断点跟踪一次混合支付（必做，约 60 分钟）

#### Step 1：准备一个两池都有钱的钱包

```powershell
# 后台赠送 100（有效期 30 天）。需要 admin token
curl.exe -X POST http://localhost:8000/admin/wallets/users/<user_id>/gifts `
  -H "Authorization: Bearer <admin_token>" -H "Content-Type: application/json" `
  -d '{\"amount\":100,\"reason\":\"promotion\",\"expires_in_days\":30,\"idempotency_key\":\"learn-stage6-001\"}'
```

现金池用阶段 5 D6.4 的 mock 支付流程充。

#### Step 2：打四个断点

| 文件 | 位置 | 看什么 |
| --- | --- | --- |
| `wallet_service.py` | `:324`（拆分后） | `gift_hold` / `cash_hold` |
| `wallet_service.py` | `:390`（capture 分支） | 走 A 还是 B |
| `gift_service.py` | `:203`（FIFO 循环） | `grants` 的顺序、每个 `take` |
| `usage_service.py` | `:94`（读拆分） | `gift_charge` / `cash_charge` |

#### Step 3：发一次真实调用，记四字段轨迹

在笔记里填这张表，**每一行都从 Variables 面板抄真实值**（不是类型，是值）：

| 时刻 | gift_balance | gift_held | balance | held | 说明 |
| --- | --- | --- | --- | --- | --- |
| 请求进来 | | | | | |
| hold 之后 | | | | | 拆分 = ? + ? |
| 上游返回后 | | | | | 还没结算，值应该没变 |
| capture 之后 | | | | | |
| consume 之后 | | | | | 批次 remaining 变了吗 |

关键观察点：**上游调用期间两池的值应该是不变的**。如果你看到它变了，说明你的断点位置理解错了。

### 任务 3 · 用 SQL 验证三条不变式（必做，约 45 分钟）

**每条 SQL 跑之前先预测结果，再跑。** 预测和实际不一致时，先怀疑自己的理解，再怀疑代码。

#### SQL 1：批次账本 vs 钱包赠送池（**最重要的一条**）

```sql
-- Σ(active 批次 remaining) 应该等于 gift_balance + gift_held
-- 有输出 = 账本崩了，这是 P0 级事故
SELECT w.id,
       w.gift_balance,
       w.gift_held,
       w.gift_balance + w.gift_held AS wallet_gift_total,
       COALESCE(g.remaining, 0)     AS grants_remaining,
       COALESCE(g.remaining, 0) - (w.gift_balance + w.gift_held) AS delta
FROM wallets w
LEFT JOIN (
    SELECT wallet_id, SUM(remaining) AS remaining
    FROM gift_grants
    WHERE status = 'active'
    GROUP BY wallet_id
) g ON g.wallet_id = w.id
WHERE COALESCE(g.remaining, 0) <> (w.gift_balance + w.gift_held);
```

这条 SQL 就是 `reconcile_gifts._find_wallet_mismatches`（`:120-158`）的手写版。跑完再跑一次任务本体对照：

```powershell
python -m app.tasks.reconcile_gifts
```

#### SQL 2：用量拆分 vs capture 流水

```sql
SELECT
  (SELECT COALESCE(SUM(gift_charge_amount), 0) FROM usage_logs)                       AS usage_gift,
  (SELECT COALESCE(SUM(-gift_amount), 0) FROM wallet_transactions WHERE type='capture') AS capture_gift;
```

两个数应该相等。不等意味着什么？（`usage_service._resolve_charge_split` 降级成全现金了——去日志里找 `charge_split_mismatch`）

#### SQL 3：单笔调用的完整两池轨迹

```sql
SELECT type, amount, gift_amount,
       amount - gift_amount AS cash_amount,
       balance_after, held_after, gift_balance_after, gift_held_after,
       gift_grant_id
FROM wallet_transactions
WHERE ref_request_id = '<你任务2的 request_id>'
ORDER BY created_at;
```

对着结果回答：

- [ ] hold 那条的 `gift_amount` 是多少？和你断点看到的 `gift_hold` 一致吗？
- [ ] capture 那条的 `gift_amount` 和 hold 的一样吗？不一样说明发生了什么？
- [ ] release 那条的 `gift_amount` 是 0 吗？为什么？（D5.4 情况 A）
- [ ] `gift_grant_id` 填的是哪个批次？（D5.4 最后一段）

### 任务 4 · 全链路手工演练（必做，约 90 分钟）

按顺序走完，**每一步都记录两池四字段的变化**：

| # | 动作 | 观察点 |
| --- | --- | --- |
| 1 | 后台赠送 100，有效期 30 天 | `gift_grants` 多一行；`gift_balance` +100 |
| 2 | **用同一个 idempotency_key 再发一次** | 批次数量不变，余额不变（幂等） |
| 3 | 发起若干次调用，直到赠金花完 | `GET /wallets/me/gifts` 看 `remaining` 递减；批次 `status` 变 `used_up` |
| 4 | 赠金花完后继续调用 | 自动转现金，`usage_logs.gift_charge_amount = 0` |
| 5 | 再赠送两批：一批 30 天到期、一批不填过期时间 | 验证 FIFO：下次消费花的是**哪一批** |
| 6 | 后台撤回其中一批的剩余 | 只退未消耗部分；再撤一次 → **409** |
| 7 | `python -m app.tasks.expire_gifts --dry-run` | 未到期的批次不该出现在输出里 |

第 5 步是本任务的考点：先赠「30 天到期」的，再赠「永久有效」的，然后消费——**花的应该是 30 天那批**（`nulls_last`，D4.3）。用 SQL 验证：

```sql
SELECT id, amount, remaining, expires_at, status
FROM gift_grants WHERE wallet_id = '<你的钱包id>'
ORDER BY expires_at ASC NULLS LAST, created_at ASC;
```

### 任务 5 · 两个故障演练 + 一道逆向思考题（必做，约 60 分钟）

#### 演练 A：现金为 0、赠金充足

把现金池调成 0（后台 `adjust`），保留赠金，发起调用。

**期望：正常返回，不是 402。** 这是 `available` 口径变更（`wallet_service.py:316`）最直接的体现。

如果你看到 402，去检查是不是走了组织主体——`quota_service.py:206` 的口径**不含赠金**（D5.2 问题 1）。

#### 演练 B：赠金正在冻结时跑过期回收

1. 手工把某个批次的 `expires_at` 改成过去时间（SQL 直接改，这是演练）
2. 发起一个流式调用并**在中途停住**（让 `gift_held > 0`）
3. 另开终端跑 `python -m app.tasks.expire_gifts --dry-run`
4. 观察日志里的 `expire_gifts_wallet_skipped`

在笔记里记下：`wallets_skipped` 是几？如果代码没有这个跳过逻辑，那个进行中的请求 capture 时会抛什么异常？（回到 `gift_service.py:186-199`）

#### 逆向思考题（本阶段的收官题）

**如果当初把 `capture` 的差额退还写成「先退赠送再退现金」，会发生什么？**

动手做，不要只想：

```python
# wallet_service.py:392-393，临时改成（改完记得改回来！）
gift_release = min(gift_hold, release_amount)
cash_release = release_amount - gift_release
```

```powershell
pytest tests/test_gift_billing.py -v
```

在笔记里回答：

- [ ] **哪个测试先红？** 断言的期望值和实际值分别是多少？
- [ ] 用 PRD 例子手算这个错误版本的终态：`gift_balance = ___`，`balance = ___`，`gift_charge = ___`
- [ ] 这个 bug 对**平台**是赚了还是亏了？对**用户**呢？
      （错误版本下赠金没花掉、现金多花了。用户的不可退赠金被留着、可退现金被花了——**用户吃亏**，而且退款时平台要多退钱）
- [ ] 这个 bug 在没有等价性测试的情况下，多久会被发现？靠什么发现？

```powershell
git checkout backend/app/services/wallet_service.py    # 改回来！
```

---

## 三、过关自测

1. 一次调用的钱从哪个池扣、按什么顺序、失败时怎么原路退回？说出三处代码位置。（D5）
2. 「按占用逆序退差额」为什么等价于「用实扣金额重新按优先级分摊」？用 PRD 例子说明。（D5.4 / 任务 1）
3. 用户钱包里有 500 赠金 + 100 现金，来申请退款，最多能退多少？依据在哪？（D0.2 / D3.1）
4. 一笔纯赠金消费，报表里的毛利为什么是负数？这是 bug 还是预期？（D1 / D7.2）
5. 赠金到期回收为什么要跳过 `gift_held > 0` 的钱包？这个简化的代价是什么？（D8.1）
6. 写出那条「资金正确性总开关」不变式，并说出它在哪两个地方被校验。（D3.1 / D4.3 / D8.2）
7. `capture` 的拆分信息是通过参数传进来的还是从哪读的？这个设计让多少个调用方免于改动？（D5.3）
8. `_lock_active_grants_fifo` 的排序里 `nulls_last()` 如果写错成 `nulls_first`，会造成什么真实损失？（D4.3）
9. `gift_service` 的 `grant` 为什么在拿到行锁之后还要再查一次幂等键？画时序说明。（D4.1）
10. 为什么 `reconcile_gifts` 只告警不修数，而阶段 5 的 `reconcile_holds` 会自动补 capture？（D8.2）

---

## 四、这个阶段常见的坑

- **以为 `wallet.balance` 还是「总余额」**：它现在只是现金池。所有涉及余额的判断都要问一句「含不含赠金」。
- **在 hold 阶段就想核销批次**：代码刻意没这么做（`wallet_service.py:301`）。想清楚原因再往下读，否则你会觉得 capture 里的 consume 是多余的。
- **把 `gift_amount` 当成和 `amount` 并列的字段**：它是 `amount` 的**其中一部分**。现金部分 = `amount - gift_amount`。这一个误解会让你所有对账 SQL 都算错。
- **只验证 `gift_balance` 不验证 `gift_held`**：预扣期间钱在 `gift_held` 里。不变式右边是 `gift_balance + gift_held`，少一项就永远对不上。
- **在有进行中请求时手工改 `gift_grants`**：会直接把不变式打破，然后 capture 抛 `ConflictError`。演练要改数据就先确认 `gift_held = 0`。
- **跑 `expire_gifts` 不加 `--dry-run` 就上手**：它会真的回收。养成先 dry-run 的肌肉记忆，这个习惯在阶段 5 已经强调过一次了。
- **拿组织额度检查的 `available` 口径去理解 hold**：两处口径确实不同（`quota_service.py:206` vs `wallet_service.py:316`）。别把它当 bug 直接改，先想清楚各自管的是什么事。
