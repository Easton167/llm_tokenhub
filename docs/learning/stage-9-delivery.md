# 阶段 9 实施细则：迭代实战——独立交付一个真实功能

> 配套 `docs/learning_plan.md` 的「阶段 9」。前置：阶段 1–8 全部过关。
> 产出物：`docs/learning/notes/stage-9.md` + **一个分支上的完整交付**（设计说明 + 迁移 + 代码 + 测试 + review 记录）。

**本阶段一句话**：前八个阶段是「读懂别人写的」，这一阶段是「**让别人能读懂你写的**」。检验标准不是功能能跑，而是这份 diff 放到真实项目里，能不能通过 code review。

---

## 零、这个阶段和前面八个不一样

前面每个阶段都有「读单 + 任务 + 过关自测」。这一阶段没有读单——**你要读的是规范和别人的交付样本，然后自己动手交付一次。**

一个警告先摆在前面：

> **全程不要让 AI 替你写核心逻辑。**
> 让 AI 写完，你会觉得「我懂了」；一周后你会发现自己什么都记不住。
> AI 在这一阶段的正确用法只有两个：**解释你读不懂的代码**，和**review 你写完的 diff**。

---

## 一、怎么读：读规范，读别人的交付

### D1 · 编码约定（30 分钟）

精读 `docs/CODING_CONVENTIONS.md`，边读边对照你阶段 1–8 见过的代码验证。在笔记里整理一张**你自己的检查清单**（后面 Step 6 要用），至少覆盖：

- [ ] 命名：service / api / schema / task 各自的文件与函数命名习惯
- [ ] 分层：路由里能写什么、不能写什么（回想 `api/admin/reports.py` 那些十几行的薄路由）
- [ ] 事务边界：谁负责 commit（`wallet_service.py:37-38` 的类 docstring 写得最清楚）
- [ ] 错误：什么时候抛哪个 `app/core/errors.py` 里的异常
- [ ] 中文注释与 docstring 的写法（本项目几乎每个文件顶部都有中文 docstring 说明职责）

再读 `docs/MVP_SCOPE.md` 第 6 节的变更控制，理解「什么样的改动需要先改文档」。

### D2 · 逆向读一次完整交付（**90 分钟，本阶段最重要的阅读**）

**赠金专项（阶段 6）是本项目最完整的一次功能交付，把它当成范本逆向拆一遍。**

按真实的开发顺序排列，看每一层各自产出了什么：

| # | 层 | 文件 | 这一层回答什么 |
| --- | --- | --- | --- |
| 1 | 需求 | `docs/prd/wallet.md#赠送余额gift-credit` | 要做什么、为什么这么做 |
| 2 | 口径影响 | `docs/GIFT_CALIBER_CHANGE.md` | **改动波及了哪些既有概念** |
| 3 | 数据 | `alembic/versions/013_gift_credit.py` | 表怎么变、存量数据怎么办、怎么回滚 |
| 4 | 模型 | `app/db/models/wallet.py:121-170` | 约束写在哪 |
| 5 | 领域服务 | `app/services/gift_service.py` | 新概念的核心逻辑 |
| 6 | 改造既有服务 | `app/services/wallet_service.py:120-149`、`288-534` | **怎么在不破坏调用方的前提下扩展** |
| 7 | API | `app/api/admin/wallets.py:274-424` | 权限、风控、幂等、审计 |
| 8 | 周边适配 | `usage_service.py`、`report_service.py` | 新维度怎么进用量和报表 |
| 9 | 任务 | `tasks/expire_gifts.py`、`tasks/reconcile_gifts.py` | 兜底与对账 |
| 10 | 测试 | `tests/test_gift_ledger.py`、`tests/test_gift_billing.py` | 行为契约 |

在笔记里回答三个问题：

1. **第 2 层（口径影响文档）为什么单独存在？** 如果跳过它直接写代码会怎样？
   （提示：`revenue` 的含义变了。不写下来，报表那边的人就会按老口径继续用，直到某天财务发现数字对不上）
2. **第 6 层是怎么做到「5 个调用方零改动」的？**（阶段 6 D5.3 已经答过，这里再确认一次：拆分信息存在流水里，不进方法签名）
3. **第 10 层里，哪一条测试是「安全网」级别的？** 没有它，这次改造敢不敢上线？
   （`test_hold_capture_equals_direct_priority_charge` 那 12 组参数化等价性测试）

**这三个问题的答案，就是你这一阶段要复制的东西。**

### D3 · 看一眼小型交付长什么样（20 分钟）

不是所有功能都有 10 层。找一个小的对照看，比如按 `git log` 找某次只加了一个接口的提交，或者直接读 `app/api/wallets.py`（68 行，三个路由）+ 它依赖的 service 方法。

结论应该是：**层数取决于改动波及面，不取决于代码量**。你选的题如果只加一个只读接口，不需要迁移也不需要口径文档；如果动了金额或状态机，那 10 层一个都不能少。

---

## 二、怎么做：七步交付流水线

**不管选哪个题，都走这七步。** 这就是本阶段要练成的肌肉记忆。

### Step 0 · 开分支（1 分钟）

```powershell
cd d:\LLM_TokenHub
git checkout main
git pull                              # 如果有远端
git checkout -b learn/stage9-<你的题目>
```

### Step 1 · 写设计说明（**1–2 小时，不要跳过**）

在 `docs/learning/notes/stage-9-design.md` 里写，**写完再动第一行代码**。模板：

```markdown
## 1. 要解决什么问题
（一句话。写不出一句话说明你还没想清楚）

## 2. 影响面
- 改哪些表 / 加哪些字段：
- 改哪些接口 / 加哪些接口：
- 影响哪些既有概念的口径：   ← 没有就写"无"，但必须显式确认过
- 影响哪些既有调用方：

## 3. 数据设计
- 字段类型、可空性、默认值：
- 约束（唯一 / Check / 索引）：
- 存量数据怎么处理：
- downgrade 会丢什么：

## 4. 接口设计
- Method / Path / 鉴权 / 入参 / 出参：
- 参考的现有接口是哪个（照抄它的风格）：

## 5. 失败场景清单        ← 这一节最能看出功力
| 场景 | 期望行为 | 谁来兜底 |
| --- | --- | --- |
| 并发同时调用 | | |
| 中途崩溃 | | |
| 重复调用（幂等） | | |
| 权限不足 | | |
| 输入非法 | | |

## 6. 测试计划
- 正常路径：
- 边界：
- 失败路径：
- 幂等/并发：
```

**第 5 节写不满就回去想。** 这一节写得越细，后面写代码越快——因为你已经把所有分支想过一遍了。

### Step 2 · 数据层（如需要）

```powershell
cd d:\LLM_TokenHub\backend
# 1. 先改 model
# 2. 自动生成迁移
alembic revision --autogenerate -m "add xxx"
# 3. 打开生成的文件，逐行检查！autogenerate 经常漏东西或多东西
# 4. 升级
alembic upgrade head
# 5. 立刻验证回滚
alembic downgrade -1
alembic upgrade head
```

**第 3 步必须人工检查**，autogenerate 常见的三个问题：

| 问题 | 表现 |
| --- | --- |
| 漏 server_default | 加非空列到有数据的表 → upgrade 直接失败 |
| 生成多余的 drop | 它把数据库里手工加的东西当成"要删掉的" |
| downgrade 是空的 | 需要你手写 |

对照 `013_gift_credit.py:155-172` 学存量回填的写法，对照 `:175-198` 学 downgrade 的删除顺序。

**第 5 步（先 downgrade 再 upgrade）是交付要求之一，别等到最后才试。**

### Step 3 · Service 层

规矩（都在前八个阶段见过）：

- 业务逻辑放 service，**路由只做参数校验 + 调用 + 返回**
- service **不自行 commit**（`wallet_service.py:37-38`）
- 涉及金额或计数的并发场景，用 `SELECT ... FOR UPDATE`（`wallet_service.py:45-58`）
- 需要幂等的写操作，用「唯一索引 + 命中直接返回」（`wallet_service.py:309-312`）
- 抛 `app/core/errors.py` 里已有的异常，不要自己造

### Step 4 · Schema + API 层

- Schema 放哪：钱包类复用 `schemas/admin.py`，报表类放 `schemas/report.py`（阶段 5 D5 Step 1.5 讨论过）
- 路由风格照抄最接近的现有接口，包括分页参数名、时区标注、错误码
- 鉴权依赖选对：网关用 API Key（`get_auth_context`），控制台用 JWT（`get_user_context`），后台身份用 `require_admin`。新的 `/admin` 路由**不要**自己声明权限码——在 `ADMIN_RESOURCES` 登记前缀即可，`enforce_admin_permission` 会按方法 + 路径推导；忘了登记 = 只有超管能访问（fail-closed，阶段 3 D7）

### Step 5 · 测试

模板用阶段 7 D6.6 你自己整理的那份。**至少覆盖四类**：

```python
async def test_xxx_requires_auth(...):        # 401/403
async def test_xxx_happy_path(...):           # 正常
async def test_xxx_boundary(...):             # 边界（0、空、超限）
async def test_xxx_idempotent_or_concurrent(...):  # 幂等 或 并发
```

三条提醒（阶段 7 D6 踩过）：

- 不用加 `@pytest.mark.asyncio`（`asyncio_mode = "auto"`）
- 测试连真库、结束回滚
- 接口内部有 commit 时，参考 `test_observability.py:28-46` 把 commit 换成 flush

```powershell
pytest tests/test_你的文件.py -v      # 先跑自己的
pytest -q                             # 再跑全量，必须全绿
```

**全量必须绿。** 如果你改坏了别人的测试，那不是"别人的测试太严"，是你破坏了既有契约。

### Step 6 · 自查清单（用 D1 整理的那份 + 下面这份）

```
[ ] 迁移能 upgrade 也能 downgrade，且我真的跑过两遍
[ ] 金额字段用整数最小单位，没有 float
[ ] 涉及并发的地方有行锁或唯一约束
[ ] 需要幂等的写接口，重复调用不产生副作用
[ ] 新加的 label / 索引没有基数爆炸风险
[ ] 错误码用的是既有体系，不是随手 raise Exception
[ ] 中文 docstring 说清了这个文件/函数的职责
[ ] 日志是结构化的（字段，不是 f-string 拼接）
[ ] 没有把敏感信息（明文 Key、密码）写进日志或返回值
[ ] pytest 全绿
```

### Step 7 · AI Review（**必做**）

```
请 review 这个 diff。我是在学习这个项目，请当成真实的 code review，严格一点。

重点看：
1. 分层是否符合本项目约定（service / api / schema 的职责边界）
2. 并发与幂等：有没有 check-then-act 竞态、重复调用会怎样
3. 迁移：downgrade 是否可用、存量数据是否处理、有没有锁表风险
4. 失败路径：哪些异常场景我没处理
5. 测试：覆盖是否够，有没有"只测了 happy path"
6. 与既有代码的一致性：命名、错误码、时区、金额单位

不要帮我改代码，只指出问题和理由。
```

**把每一条意见都记进笔记，并写下你的回应**：接受（怎么改）/ 不接受（为什么）。

「不接受」也是合格的回应——但你得说得出理由。**能有理有据地反驳 review 意见，是本阶段真正的毕业标志。**

最后：

```powershell
git checkout main       # 练习分支留着，方便以后回看
```

---

## 三、七个选题的具体指引

选 1–2 个。**第一次做，强烈建议从 A、A2 或 B 开始**（一天能收工，能完整走一遍七步流水线），有余力再挑一个进阶的。

### A · 入门：让 `last_used_at` 真的被更新

> ⚠️ 学习计划原来的入门题是「API Key 过期时间」，**这个功能已经实现了**（`models/api_key.py:29-31` 有字段，`auth_service.py:52-53` 有校验，`api/keys.py:44`、`:100-101` 有接口）。
> 花 20 分钟把这三处读一遍当热身，然后做下面这个——它是同一块代码里**真实存在的缺口**。

**现状**：`api_keys.last_used_at` 字段定义了（`models/api_key.py:33-35`），schema 也暴露了（`schemas/auth.py:123`），但**全项目没有任何一处在写它**。用户在控制台看到的永远是 `null`。

**要做**：鉴权成功时更新它。

| 步骤 | 提示 |
| --- | --- |
| 迁移 | **不需要**，字段已存在。你的迁移练习在 Step 2 里换成「跑一次 autogenerate 确认它什么都不生成」 |
| 改哪里 | `auth_service.verify_api_key`（`:32-60`）成功返回之前 |
| 测试 | 调用后 `last_used_at` 非空；**再调一次，验证节流逻辑** |

**这道题的真正考点是性能**，别小看它：

> 每次鉴权都 UPDATE 一行，意味着**每个 API 请求都多一次数据库写**。QPS 1000 就是每秒 1000 次写，而且都打在同一张表上。

所以你必须做**节流**。在设计说明里回答：

- [ ] 节流阈值取多少？（比如「距上次更新超过 60 秒才写」）依据是什么？
- [ ] 阈值配在哪？（硬编码常量 / `settings` / 表字段？照抄本项目已有配置项的风格）
- [ ] 这个更新要不要和主请求同一个事务？失败了要不要影响主请求？
      （提示：**绝对不能**。`last_used_at` 是统计字段，它写失败了不该让用户的 chat 请求失败）
- [ ] 并发下两个请求同时更新会怎样？需要行锁吗？
      （提示：这是**幂等的覆盖写**，不是增量。想清楚为什么这种场景不需要锁）

**加分**：给它加一个 `ponytail:` 注释，说明「节流 = 牺牲精度换写入量」这个取舍和升级路径（比如以后改成 Redis 缓冲批量落库）。参考 `expire_gifts.py:77-78` 的写法。

### A2 · 入门：给后台加一个只读资源

阶段 3 D7 学过：新的 `/admin` 路由**不在每个函数上声明权限**，而是在 `ADMIN_RESOURCES` 登记前缀，由 `resolve_required_permission` 推导。漏配 = fail-closed = 只有超管能访问。

**要做**：加一个只读接口（例如 `GET /admin/health-board` 返回渠道健康汇总，数据从现有 service 拼即可），并让它出现在权限目录和侧边栏候选项里。

| 步骤 | 提示 |
| --- | --- |
| 登记资源 | `app/core/admin_permissions.py` 的 `ADMIN_RESOURCES` 加一项，`actions=(ACTION_VIEW,)`，`api_prefixes` 对上你的路径 |
| 路由 | 挂到 `app/api/admin/router.py`，和别的业务路由一样走 `_admin_deps`，**不要**自己再写一遍权限检查 |
| 测试 | 给 `test_resolve_required_permission` 加一行参数；跑 `test_every_admin_route_resolves_to_a_grantable_permission` 必须仍绿 |
| 验权 | 造一个只有 `channel:view`、没有你这个新码的管理员，确认新接口 403；超管 200 |

**这道题的真正考点是 fail-closed**：先故意不登记前缀，跑那条「所有路由都必须能解析」的测试，看它怎么红；再补上登记，看它怎么绿。这个红→绿比接口本身更值钱。

### B · 入门+：`GET /keys/{id}/usage-summary`

如果 A 做完还有时间，或者你想练一个「纯新增只读接口」：给单个 Key 加一个用量摘要（最近 N 天的请求数、token 数、消费额）。

| 步骤 | 提示 |
| --- | --- |
| 参考 | `app/api/orgs.py:141-163` 的日报表接口，风格直接抄 |
| 归属校验 | 只能查自己的 Key，参考 `kb_service.require_owned_*`（阶段 8 D5.2） |
| 聚合 | 一次 SQL `GROUP BY` 搞定，**不要循环查 N 天**（阶段 5 任务 3 Step 2 强调过） |
| 口径 | 消费额用 `usage_logs.charge_amount` 还是钱包 capture 流水？两者什么关系？想清楚再写 |
| 时区 | 显式选一个并在响应里标注（阶段 5 任务 3 Step 1.2） |
| 坑 | 赠金拆分要不要返回？（`gift_charge_amount` / `cash_charge_amount`，阶段 6） |

### C · 进阶：Key 级别的每日消费限额

**这是六个题里最能检验阶段 4 + 5 + 6 综合掌握程度的。**

| 决策点 | 两条路 |
| --- | --- |
| 限额存哪 | `api_keys` 加字段 / 单独一张表 |
| **计数存哪** | **Redis（快，会丢）/ 日聚合表（准，有延迟）/ 实时查 usage_logs（准，慢）** |
| 检查点插哪 | 预扣之前？之后？（想清楚：超限了还预不预扣） |
| 累加在哪 | capture 之后（参考 `quota_service.quota_consume` 的位置，`chat.py:240-245`） |
| 跨天怎么重置 | **定时任务 vs 惰性重置**——阶段 5 D4 讲过项目已有的选择，照抄它 |

必答的设计题（写进设计说明）：

- [ ] 限额是按 UTC 天还是业务时区天？（`settings.business_timezone`，阶段 6 D6.2 讲过为什么风控要用业务时区）
- [ ] Redis 挂了怎么办？拒绝所有请求，还是降级放行？（看 `rate_limit_service` 现有的降级策略，照抄）
- [ ] 并发下会不会超限？（回到阶段 5 D2.3：光有应用层检查是不够的）
- [ ] 超限返回什么状态码和错误码？（去 `app/core/errors.py` 找已有的，别造新的）

### D · 进阶：新增 Gemini 原生协议适配器

**最能检验阶段 4。** 现在只有两个适配器：`openai_compatible.py`、`anthropic.py`。

| 步骤 | 提示 |
| --- | --- |
| 读 | `adapters/base.py`（抽象接口）→ `anthropic.py`（**非 OpenAI 协议的完整样例，重点看它怎么抹平差异**）→ `registry.py`（注册） |
| 写 | 照着 `anthropic.py` 的结构写，**先非流式跑通再做流式** |
| 测 | 参考 `tests/test_routing_adapter.py`，用 mock 上游响应，不要真的调 Gemini |

四个必须处理的差异点（在设计说明里逐条写清 Gemini 侧长什么样）：

- [ ] 请求体结构：`messages` → Gemini 的 `contents` / `parts`
- [ ] system prompt：Gemini 放在哪里（和 OpenAI/Anthropic 都不同）
- [ ] 流式格式：SSE 的事件形态、结束标记
- [ ] **usage 字段**：token 数从响应的哪个字段取——**这个取错了就是计费错误**，是本题最容易翻车的地方

**加分**：写完后翻回阶段 4 你画的时序图，确认新适配器在图上的位置一个字都不用改。如果需要改图，说明你的适配器把抽象层的边界越界了。

### E · 进阶：赠金到期提醒

`docs/prd/wallet.md` 明确列为二期。扫描 3 天内到期的批次，产出站内提醒记录。

| 步骤 | 提示 |
| --- | --- |
| 扫描 | 照抄 `gift_service.find_expired_grant_ids`（`:266-286`），把条件改成 `expires_at BETWEEN now AND now+3d` |
| 任务骨架 | 照抄 `tasks/expire_gifts.py` 的整个结构：dataclass 结果、`--dry-run`、`--limit`、错误隔离、结构化日志、退出码 |
| 存哪 | 需要一张提醒表？还是复用现有的通知机制？（先 grep 看看有没有） |

**幂等是本题的核心考点**：同一批次同一轮只提醒一次。

- [ ] 幂等键怎么设计？（提示：`{grant_id}:{提醒轮次}`——想想「轮次」怎么定义。按天？按批次的到期日？回去看阶段 8 D7.3 的 generation 是怎么解决同类问题的）
- [ ] 任务跑到一半崩了，重跑会重复提醒吗？
- [ ] 用户已经把赠金花完了，还提醒吗？（`remaining > 0` 这个条件别忘了）

### F · 进阶：批量赠送

同样是 PRD 二期。后台按用户列表一次发放。

**本题唯一的考点是：部分失败怎么办。**

在设计说明里必须选一个并说明理由：

| 策略 | 含义 | 代价 |
| --- | --- | --- |
| 全成功或全失败 | 一个失败就整批回滚 | 1000 个用户里有 1 个钱包不存在，999 个都发不了 |
| 尽力而为 + 报告 | 逐个处理，失败的记下来返回给操作人 | 需要清晰的结果报告结构 |

- [ ] 复用 `gift_service.grant` 的幂等键（`:46-123`）——批量场景下每个用户的 key 怎么生成？
- [ ] 事务边界画在哪？（每个用户一个事务？参考 `expire_gifts.py:113-124` 的按钱包 commit + 错误隔离）
- [ ] 风控上限怎么算？（`_check_daily_limit` 是按操作人日累计的，`admin/wallets.py:299-319`。批量 1000 个用户会不会一次打满？**中途打满了怎么办**——这是本题最难的一问）
- [ ] 审计日志写几条？（一条批量记录 vs 每个用户一条。想想事后要怎么查）

### G · 综合：渠道成本 vs 用户收入毛利报表

> 注意：`report_service` 里**已经有** `trend_by_day`（含 `gross_profit`，`:140-154`）和 `channel_cost_share`（`:192-217`）。
> 所以这道题的真实内容是：**在已有报表体系上新增一个维度组合**，而不是从零做毛利。先花 30 分钟读懂现有的，再定义你要加什么（比如「按渠道 × 模型的毛利矩阵」或「毛利率最低的 Top N 渠道」）。

| 步骤 | 提示 |
| --- | --- |
| 读 | `report_service.py:102-137`（指标列定义 + 行转换）、`:156-190`（Top 查询的写法） |
| 口径 | **毛利 = `cash_revenue_amount` − `cost_amount`**，不是 `revenue_amount`（阶段 6 / 7 反复强调） |
| 数据源 | `usage_daily_summary` 聚合表，不要直接扫明细 |
| 当天数据 | 记得 `_prepare_range`（`:66-70`）会刷新当日 |
| 成本缺失 | `cost_price_missing` 这个标记怎么处理？报表上要不要提示"部分数据成本未配置"？ |

---

## 四、过关自测

**只有一条**：

> 独立完成至少一个功能的全流程交付，全程没有让 AI 代写核心逻辑。

「完成」的定义（缺一不可）：

- [ ] 设计说明在动代码之前写完，且失败场景清单不少于 5 条
- [ ] 迁移能 upgrade 也能 downgrade，且你真的跑过两遍（如果这道题没有迁移，跑一次 autogenerate 确认它不生成东西）
- [ ] 测试覆盖四类场景，`pytest` 全量绿
- [ ] AI review 的每一条意见都有你的书面回应（接受怎么改 / 不接受为什么）
- [ ] 你能对着自己的 diff，像本手册讲别人代码那样，讲清每一处设计取舍

最后一条是真正的毕业标准。

---

## 五、这个阶段常见的坑

- **跳过设计说明直接写代码**：写到一半发现表结构不对，推倒重来。这一阶段最大的时间黑洞。
- **让 AI 写核心逻辑**：这一阶段就白学了。AI 只用来解释和 review。
- **autogenerate 出来的迁移不看就用**：漏 `server_default`、多余的 drop、空 downgrade，三个坑轮流踩。
- **只测 happy path**：设计说明里的失败场景清单写了，测试里却一条没覆盖。
- **在路由里写业务逻辑**：本项目所有路由都很薄，你的一写就三十行，review 第一条就会被打回来。
- **自己造错误码**：`app/core/errors.py` 里已经有一套，先去找。
- **忘了金额单位**：整数最小单位，`money_scale = 10000`。展示层才除。
- **只顾自己的测试，不跑全量**：改坏了别人的契约却不自知。
- **改了口径不更新文档**：赠金专项专门为此写了 `GIFT_CALIBER_CHANGE.md`。你改了口径也要留下这样一份，哪怕只有半页。
