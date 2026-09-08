# 阶段 7 实施细则：周边能力——可观测性、报表、后台任务

> 配套 `docs/learning_plan.md` 的「阶段 7」。阅读模式字母见 [README](./README.md#二四种读代码的方式后面每条读单都会标)。
> 前置：阶段 6 过关（报表的现金口径依赖赠金拆分）。
> 产出物：`docs/learning/notes/stage-7.md`，含 3 个指标的解读 + 1 条完整日志追踪链 + 1 份排障 SOP + 1 个能在 `/metrics` 里看到的自定义指标。

**本阶段一句话**：前面六个阶段学的是「怎么把功能写对」，这一阶段学的是「**功能上线之后你怎么知道它是对的**」。这是从「能写功能」到「能维护系统」的分界线。

---

## 零、先想清楚这一阶段在解决什么问题

凌晨两点，用户发工单说「我这次调用扣多了」。你手上只有一个时间点和一个用户 ID。

你需要在**十分钟内**回答三个问题：

```
1. 这次调用发生了什么？          ← 日志（request_id 串联）
2. 系统整体现在健康吗？          ← 指标（/metrics）
3. 这笔钱到底怎么算出来的？      ← 报表 + 用量表 + 钱包流水
```

本阶段读的每一个文件，都是为了在那十分钟里派上用场。读的时候一直带着这个场景。

---

## 一、怎么读

### 读单总览

| # | 内容 | 模式 | 预算 | 核心问题 |
| --- | --- | --- | --- | --- |
| D1 | `core/metrics.py` | S+Q | 45 min | 有哪些指标、谁在打点 |
| D2 | `core/logging.py` + `middleware/request_id.py` | Q | 40 min | request_id 怎么串起一次请求 |
| D3 | `tests/test_observability.py` | T | 30 min | 可观测性的行为契约 |
| D4 | `usage_daily_service.py` + 聚合任务 | Q | 50 min | 为什么要日聚合表 |
| D5 | `report_service.py` + `api/admin/reports.py` | S+Q | 40 min | 报表怎么算 |
| D6 | `tests/conftest.py` | **Q 精读** | 40 min | **你以后写测试的模板** |

---

### D1 · Prometheus 指标（模式 S + Q，45 分钟）

#### 1.1 先扫一遍指标清单（S 模式，15 分钟）

`app/core/metrics.py` 整个文件就是一张指标表。用 `Ctrl+Shift+O` 列符号，或者直接看这张表：

| 变量 | Prometheus 名称 | 类型 | Labels | 行号 |
| --- | --- | --- | --- | --- |
| `HTTP_REQUESTS` | `tokenhub_http_requests_total` | Counter | method / path / status | `14-18` |
| `HTTP_LATENCY` | `tokenhub_http_request_duration_seconds` | Histogram | method / path | `19-24` |
| `CHAT_REQUESTS` | `tokenhub_chat_requests_total` | Counter | model / status / stream | `27-31` |
| `CHAT_LATENCY` | `tokenhub_chat_request_duration_seconds` | Histogram | model / stream | `32-37` |
| `EMBEDDING_REQUESTS` / `_LATENCY` | `tokenhub_embedding_*` | Counter / Histogram | model | `38-48` |
| `BILLING_OPS` | `tokenhub_billing_ops_total` | Counter | op / result | `49-53` |
| `HOLD_AMOUNT` / `CAPTURE_AMOUNT` | `tokenhub_{hold,capture}_amount_total` | Counter | model | `54-63` |
| `RECONCILE_OPS` | `tokenhub_hold_reconcile_total` | Counter | action | `64-68` |
| `GIFT_OPS` | `tokenhub_gift_ops_total` | Counter | op / result | `69-73` |
| `GIFT_BALANCE_TOTAL` | `tokenhub_gift_balance_total` | **Gauge** | 无 | `74-77` |
| `PAYMENT_RECONCILE_OPS` | `tokenhub_payment_reconcile_total` | Counter | action | `78-82` |
| `KB_INGEST_OPS` | `tokenhub_kb_ingest_total` | Counter | action | `83-87` |
| `KB_DOC_PROCESS_DURATION` / `_OPS` | `tokenhub_kb_doc_process_*` | Histogram / Counter | job_type / status | `89-99` |
| `KB_EMBED_TOKENS` | `tokenhub_kb_embed_tokens_total` | Counter | source | `101-105` |
| `KB_RETRIEVAL_LATENCY` | `tokenhub_kb_retrieval_duration_seconds` | Histogram | 无 | `107-111` |
| `KB_RAG_HIT` | `tokenhub_kb_rag_hit_total` | Counter | result | `113-117` |

#### 1.2 三种指标类型的选择（Q 模式，本节重点）

**这是本阶段最该内化的一件事：什么时候用哪种类型。**

| 类型 | 语义 | 只能怎么变 | 本项目的例子 |
| --- | --- | --- | --- |
| Counter | 累计发生次数/总量 | **只增不减** | `HTTP_REQUESTS`、`CAPTURE_AMOUNT` |
| Histogram | 分布（P50/P95/P99） | 观测值落桶 | `HTTP_LATENCY` |
| Gauge | 当前瞬时值 | 可增可减、可 set | `GIFT_BALANCE_TOTAL` |

问题（写进笔记）：

1. 为什么「全平台赠金敞口」是 Gauge 而不是 Counter？（它会因为过期回收而**减少**）
2. 为什么「延迟」必须用 Histogram 而不是「平均值 Gauge」？（平均值会被少数超长请求带偏，而且**平均延迟正常不代表没有用户在超时**。你需要的是 P99）
3. `CAPTURE_AMOUNT` 是 Counter，累加的是金额。这有什么用？（对时间求导 = 每秒收入速率。Counter 的正确用法是**配合 `rate()` 看趋势**，而不是看绝对值）

#### 1.3 cardinality 陷阱：`normalize_path`（`:125-135`）

```python
def normalize_path(path): ...   # 把 UUID / 长数字段折叠成 {id}
```

**这个函数是本文件最值得学的一段。** 想想没有它会怎样：

```
/admin/users/3f2a.../transactions
/admin/users/9b81.../transactions
...每个用户 ID 都会创建一条独立的时间序列
```

十万个用户 = 十万条时间序列 = **Prometheus 直接被打爆**。这叫 cardinality explosion（基数爆炸），是监控系统最常见的事故。

在笔记里记下这条规则：**label 的取值范围必须是有限且可枚举的**。`method`（7 种）、`status`（几十种）可以；`user_id`、`request_id` 绝对不行。

#### 1.4 打点位置（grep 一遍，10 分钟）

| 指标 | 打点位置 |
| --- | --- |
| HTTP 两个 | `middleware/request_id.py:48-51`（异常路径）、`:62-65`（正常路径） |
| CHAT | `api/v1/chat.py:66-67` |
| EMBEDDING | `api/v1/embeddings.py:51-52`、`services/embedding_gateway.py:353-354` |
| BILLING + 金额 | `wallet_service.py:349-350`（hold）、`:459-460`（capture）、`:509`（release） |
| GIFT | `gift_service.py:70/79/116/118/122/147/149/166/168/196/214`、`tasks/expire_gifts.py:82` |
| RECONCILE | `tasks/reconcile_holds.py:221/243/255` |

**注意 HTTP 指标打在中间件里，成功和异常两条路径都要打**（`:48-51` 和 `:62-65`）。漏了异常路径，你的错误率指标就永远是 0——这是新手最常犯的监控错误。

`/metrics` 路由本身在 `app/main.py:75-78`，就三行。

---

### D2 · 结构化日志与 request_id 串联（模式 Q，40 分钟）

#### 2.1 为什么是 JSON 不是人话

`app/core/logging.py:34-38` 用了 `JSONRenderer()`——日志输出是 JSON，不是 `2026-08-25 14:30:01 INFO 用户登录成功`。

想清楚这个取舍：JSON 对人眼不友好，但它可以被 Loki / ELK **按字段检索**。你要的是 `request_id="abc"` 一秒钟捞出全部相关日志，而不是 grep 一堆文本。

**结构化日志的核心原则：不要把变量拼进消息字符串。**

```python
logger.info("request_completed", latency_ms=12.3, status_code=200)   # 对
logger.info(f"请求完成，耗时 {latency}ms")                             # 错，没法按字段查
```

processors 链在 `:17-27`，十个处理器按顺序跑。只需要认得三个：

| processor | 行号 | 作用 |
| --- | --- | --- |
| `merge_contextvars` | `18` | **把 request_id 自动带进每条日志**（关键） |
| `TimeStamper(fmt="iso")` | `23` | 时间戳 |
| `format_exc_info` | `25` | 异常栈变成字段 |

#### 2.2 request_id 是怎么「自动」出现在每条日志里的

这是本节的核心问题。追这条链：

| 步骤 | 位置 | 做什么 |
| --- | --- | --- |
| 1 | `middleware/request_id.py:20-25` | 优先取 `X-Request-ID` 头，没有就生成 UUID |
| 2 | `:33` | 写进 `request.state.request_id` |
| 3 | **`:34-38`** | **`bind_request_context(request_id=..., method=..., path=...)`** |
| 4 | `core/logging.py:53-55` | 内部是 `structlog.contextvars.bind_contextvars` |
| 5 | `core/logging.py:18` | `merge_contextvars` 把它合并进每条日志 |
| 6 | `:66` | 响应头回写 `X-Request-ID` |
| 7 | `:57`、`:72` | **`clear_request_context()`** |

**第 3 步和第 7 步是这个机制的全部魔法**：绑定一次，这个请求内**任何地方**打的日志都自动带上 request_id，服务层不需要层层传参。

问题（认真想，这是 async 编程的重要知识点）：

1. `contextvars` 和普通全局变量的区别是什么？（提示：这是 async 应用。100 个请求在**同一个线程**里并发跑，普通全局变量会被互相覆盖。`contextvars` 是「每个协程上下文一份」）
2. 第 7 步 `clear_request_context()` 如果漏掉会怎样？（上一个请求的 request_id 泄漏到下一个请求的日志里，排障时你会追到完全无关的请求上）
3. 第 1 步为什么要优先用客户端传来的 `X-Request-ID`？（提示：微服务链路追踪。上游网关生成一次，整条链路复用同一个 ID）

#### 2.3 每条请求日志固定有哪些字段

| 来源 | 字段 |
| --- | --- |
| 中间件绑定 | `request_id`、`method`、`path` |
| structlog 自动加 | `logger`、`level`、`timestamp` |
| 请求完成事件 | `latency_ms`、`status_code`（`request_id.py:67-71`） |
| 异常事件 | 完整 traceback（`:52-56`，用的是 `logger.exception`） |

---

### D3 · 可观测性测试（模式 T，30 分钟）

`backend/tests/test_observability.py` 值得单独读，因为它同时是本阶段的行为契约**和** D6 测试模板的活样例。

| 测试 | 行号 | 断言什么 |
| --- | --- | --- |
| `test_health_returns_request_id_header` | `80-83` | 客户端传的 ID 被**原样回传** |
| `test_health_generates_request_id_when_missing` | `87-90` | 没传就生成 |
| `test_metrics_endpoint` | `93-100` | 打过 `/health` 后，`/metrics` 里能看到 `tokenhub_http_requests_total` |
| `test_admin_usage_query_apis` | `104-162` | 三个查询接口都能查到刚写入的日志 |
| `test_admin_request_logs_allow_null_api_key_id` | `166-196` | **`api_key_id=None` 不会 500** |
| `test_user_usage_api_scoped_to_self` | `200-240` | 用户只能查自己的 |
| `test_user_usage_filter_by_source` | `244-314` | 按 `source=kb_ingest` 过滤 |
| `test_reconcile_releases_stale_hold` | `318-367` | 悬挂 hold 被 release |
| `test_reconcile_captures_when_usage_exists` | `371-441` | 有用量则补 capture |

`test_admin_request_logs_allow_null_api_key_id`（`:166`）单独想一下：为什么会有 `api_key_id` 为空的请求日志？（知识库的内部调用没有用户 API Key——阶段 8 会看到。**一个新功能引入了旧接口没考虑过的数据形态，于是旧接口 500 了**。这是真实项目里最常见的回归 bug 类型，这条测试就是它的墓碑）

---

### D4 · 日聚合：为什么不实时算（模式 Q，50 分钟）

#### 4.1 先回答「为什么」

管理后台要看「最近 30 天每天的收入趋势」。两种实现：

| 方案 | 代价 |
| --- | --- |
| 实时算：每次查询扫 `wallet_transactions` + JOIN 两张日志表 | 数据量上千万后，一次查询几十秒；而且**每个人打开页面都扫一遍** |
| 日聚合：每天凌晨把昨天的数据算好存进 `usage_daily_summary` | 查询变成扫几百行；代价是当天数据有延迟 |

这是经典的**空间换时间 + 预计算**。本项目选了后者，并且给「当天数据有延迟」这个缺陷打了个补丁——见 4.4。

#### 4.2 聚合逻辑（`app/services/usage_daily_service.py:148-275`）

数据来源与 JOIN（`:159-192`）：

```
WalletTransaction (type = capture)        ← 主表，钱是真实扣掉的才算
  JOIN RequestLog  ON request_id = ref_request_id
  JOIN UsageLog    ON request_id
WHERE created_at ∈ [day_start, day_end)   ← UTC 日界
  AND ref_request_id IS NOT NULL
GROUP BY model_id, user_id, org_id, channel_id
```

**「只统计 `type = capture`」（`:181`）是最关键的一行**。阶段 5 任务 3 你已经踩过这个口径：`hold` 和 `release` 是中间态，算进去数字会完全错。

统计字段（`:165-172`）：

| 字段 | 算法 |
| --- | --- |
| `revenue_amount` | `-sum(amount)` —— capture 是负数，取反 |
| `gift_revenue_amount` | `-sum(gift_amount)` —— 阶段 6 学的赠金部分 |
| `input/output/cached_tokens` | 从 UsageLog 求和 |

成本侧（`_resolve_cost`，`:119-145`）：按 `channel_costs` 的生效时间 + 上游模型名匹配，按 1M token 单价**向上取整**（`calc_token_cost`，`:60-66`）。

问题：成本为什么向上取整而不是四舍五入？（提示：成本估少了会让毛利虚高。**报表宁可保守**——这和阶段 6 D7.1 「对不上就当全现金」是同一个价值观）

#### 4.3 幂等：先删后插（`:222-226`）

```python
delete(UsageDailySummary).where(summary_date == summary_date)   # 先删当天全部
...然后逐行 insert
```

**这是批处理任务最简单也最可靠的幂等姿势**：不去做「哪些变了、哪些没变」的增量判断，而是**整个分区重算**。

跟阶段 5 / 6 的幂等对比一下，你会发现本项目有两套幂等模式：

| 模式 | 用在哪 | 怎么做 |
| --- | --- | --- |
| 幂等键 | 资金操作（hold/capture/recharge/grant） | 唯一索引 + 命中直接返回 |
| **分区重算** | 批处理聚合 | 先删该分区，再全量插入 |

想清楚为什么资金操作不能用「先删后插」。（钱是**事件**不是**状态**，删掉流水就是销毁证据；而聚合表是**派生数据**，随时能从源数据重建）

#### 4.4 任务入口（`app/tasks/aggregate_usage_daily.py`）

```powershell
python -m app.tasks.aggregate_usage_daily --yesterday
python -m app.tasks.aggregate_usage_daily --date 2026-08-20
python -m app.tasks.aggregate_usage_daily --yesterday --dry-run
```

参数在 `:53-70`，主流程 `:24-49`，`--dry-run` 时不 commit（`:38-41`）。

**当天数据的补丁**在 `report_service._refresh_today_if_in_range`（`:52-63`）：查询窗口如果包含 UTC 当天，就现场重跑一次当天的聚合。注释（`:59`）还写了它顺带解决的问题——**cron 没跑导致的历史缺口**。

---

### D5 · 报表（模式 S + Q，40 分钟）

#### 5.1 路由表（S 模式）

router 前缀 `/reports`，挂在 `/admin` 下并统一走 `enforce_admin_permission`（`app/api/admin/router.py:23/30/43`）。所需权限码是 `report:view`（GET），由 URL 前缀推导，阶段 3 D7 讲过。

| Method | Path | 行号 | Service |
| --- | --- | --- | --- |
| GET | `/admin/reports/trend` | `44-64` | `trend_by_day` |
| GET | `/admin/reports/top` | `67-96` | `top_by_dimension` |
| GET | `/admin/reports/channel-share` | `99-120` | `channel_cost_share` |
| GET | `/admin/reports/gifts` | `123-138` | `gift_dashboard` |
| GET | `/admin/reports/reconcile` | `141-156` | `reconcile_revenue` |

路由层都很薄（十几行），全部委托给 service——**这是本项目的分层约定，你阶段 9 写新接口时照抄这个结构**。

#### 5.2 三个口径（Q 模式，本节唯一重点）

阶段 6 已经讲过，这里只做确认：

| 字段 | 含义 | 位置 |
| --- | --- | --- |
| `revenue_amount` | 计费总额，**含赠金**，不是真实收入 | docstring `:6-9` |
| `cash_revenue_amount` | 平台真实收入 | 同上 |
| `gross_profit` | `cash_revenue - cost` | `:136` |

`top_by_dimension` 排序用的是 `cash_revenue_amount`（`:186`）。

**如果你在阶段 6 没搞懂这三个口径，现在回去重读 D7.2，不要往下走。** 报表是给老板做决策用的，口径错了比没有报表更糟。

---

### D6 · `tests/conftest.py` 精读（模式 Q，40 分钟）

**这一节的产出是「你以后写测试的模板」，不是「读懂这个文件」。**

#### 6.1 全文只有三个 fixture 和一个 helper

| 名称 | 行号 | 作用 |
| --- | --- | --- |
| `admin_auth_headers()` | `17-20` | 生成 admin 的 `Authorization` 头（普通函数，不是 fixture） |
| `client` | `23-27` | 裸 `AsyncClient`，**不覆盖 DB** |
| `db_session` | `30-38` | 真实 DB session，**测试结束回滚** |
| `auth_client` | `41-62` | 带 DB 覆盖 + 临时 auth 探针路由的 client |

**注意：这里没有造用户/Key/Channel/Model 的 fixture。** 每个测试文件自己写本地 helper（见 6.4）。这是个刻意的选择——想想为什么不做成全局 fixture。（提示：不同测试需要的数据形态差别很大；共享 fixture 会变成一个谁都不敢改的巨型工厂）

#### 6.2 `db_session`：为什么用真库还要回滚（`:30-38`）

```python
engine = create_async_engine(settings.database_url, pool_pre_ping=True)   # :32
session_factory = async_sessionmaker(..., expire_on_commit=False)        # :33
async with session.begin():
    yield session
    await session.rollback()                                             # :35-37
await engine.dispose()                                                   # :38
```

三个设计决定，逐个想：

1. **用真实 Postgres 而不是 sqlite 内存库**。代价是跑测试前必须起 Postgres；收益是 pgvector、`FOR UPDATE SKIP LOCKED`、`date_trunc` 这些 Postgres 专有特性**能被真实测到**。这个项目大量依赖它们，用 sqlite 等于测了个寂寞。
2. **每个测试跑完 rollback**。测试之间天然隔离，不需要写清理代码。代价见 6.5。
3. **`expire_on_commit=False`**。commit 之后对象属性仍可读，不会触发懒加载再查一次库。async 场景下这几乎是必须的（懒加载在 async 里会抛 `MissingGreenlet`）。

#### 6.3 `auth_client`：依赖覆盖的标准姿势（`:41-62`）

```python
async def override_get_db():           # :43-44
    yield db_session                   # ← 让 API 用测试的同一个 session
app.dependency_overrides[get_db] = override_get_db          # :46
...
app.dependency_overrides.clear()                            # :59（teardown）
```

**`app.dependency_overrides` 是 FastAPI 测试的核心机制**，把它抄进笔记：不用 mock、不用打桩，直接替换依赖注入的实现。

`:48-53` 还有个小技巧：动态注册一个 `include_in_schema=False` 的探针路由 `GET /test/auth`，teardown 时再从 `app.router.routes` 里摘掉（`:60-62`）。**只想测一个依赖、不想为它造一个真实业务接口时，这个手法很好用。**

#### 6.4 一个坑：为什么 `test_observability.py` 要把 `commit` 换成 `flush`

看 `test_observability.py:28-46` 的 `obs_client`：

```python
db_session.commit = db_session.flush     # 大意如此
```

想清楚这里发生了什么：测试用的 session 外面套着一个「结束时 rollback」的事务。如果 API 层真的 `commit` 了，这个外层事务就结束了，后续断言查不到数据、rollback 也失效。**把 commit 降级成 flush**，数据在事务内可见，测试结束照样回滚。

这个 trick 在你阶段 9 写「涉及多次 commit 的接口测试」时会救你一命。

#### 6.5 三条必须记住的事实

- **测试连真库**（`:32`），跑之前 Postgres 必须能连。
- **`db_session` 会回滚**（`:35-37`），所以想**手工看数据**必须发真实 HTTP 请求，而不是跑测试。
- **`asyncio_mode = "auto"`**（`pyproject.toml:47`），async 测试**不需要**加 `@pytest.mark.asyncio`。

#### 6.6 产出：把模板写进笔记

在 `notes/stage-7.md` 里写下你自己的测试骨架，以后照抄：

```python
async def _seed_user(session, suffix=""):
    """本地造数 helper，参考 test_observability.py:49-57"""
    ...

async def test_xxx(auth_client, db_session):
    user = await _seed_user(db_session)
    await db_session.flush()              # 让 API 能看到
    resp = await auth_client.get("/xxx", headers=...)
    assert resp.status_code == 200
    # 结束自动 rollback，不用清理
```

---

## 二、怎么做

### 任务 1 · 读懂三个指标（必做，约 40 分钟）

#### Step 1：制造流量

```powershell
# 起服务后，发几次调用（成功的、失败的、流式的各来几次）
curl.exe http://localhost:8000/health
curl.exe -X POST http://localhost:8000/v1/chat/completions -H "Authorization: Bearer sk-xxx" ...
```

#### Step 2：抓 `/metrics`

```powershell
curl.exe http://localhost:8000/metrics
```

#### Step 3：挑三个指标写解读（笔记产出）

必须包含**一个 Counter、一个 Histogram、一个 Gauge**。每个写清四件事：

| 指标名 | 类型 | 它在回答什么运维问题 | 打点位置（文件:行号） |
| --- | --- | --- | --- |
| | | | |

推荐组合：`tokenhub_billing_ops_total`（Counter，带 `result` label，能看失败率）、`tokenhub_http_request_duration_seconds`（Histogram，注意看它输出的 `_bucket` / `_sum` / `_count` 三组）、`tokenhub_gift_balance_total`（Gauge，需要先跑一次 `reconcile_gifts` 才有值）。

**Histogram 那条要特别看输出格式**：Prometheus 的 Histogram 在文本里会展开成一堆 `_bucket{le="0.1"}`。想清楚 P95 是怎么从这些桶里估算出来的（答案：找到累计计数跨过 95% 的那个桶，桶内线性插值）。

#### Step 4：一道判断题

跑一次 `python -m app.tasks.reconcile_gifts`，然后再抓一次 `/metrics`，观察 `tokenhub_gift_balance_total` 的变化。

问题：**这个 Gauge 只有在跑对账任务时才更新。** 如果 cron 挂了三天，你在监控面板上看到的是什么？（一条平的线——**看起来一切正常**。这是 Gauge 类指标最阴险的失效模式：陈旧数据和健康数据长得一模一样）

在笔记里写下：怎么发现这种「指标不再更新」的情况？（提示：Prometheus 的 `time() - last_update` 或者给任务本身加一个「上次成功运行时间」的 Gauge）

### 任务 2 · 只靠 request_id 追一次报错（必做，约 50 分钟）

#### Step 1：制造一个报错

三选一（推荐第一个，最接近真实故障）：

```
a. 把某个渠道的上游地址改成不存在的域名，发起调用
b. 把钱包余额和赠送余额都清零，发起调用（402）
c. 用一个已禁用的 API Key 调用（401）
```

#### Step 2：从响应里拿 request_id

```powershell
curl.exe -i -X POST http://localhost:8000/v1/chat/completions ... 
# 看响应头 X-Request-ID
```

**这一步就是真实排障的起点**：用户能提供的通常只有这个 ID（或者一个时间点）。

#### Step 3：串出完整链路

日志是 JSON，Windows 下可以这样过滤（把服务输出重定向到文件再筛）：

```powershell
Select-String -Path .\server.log -Pattern '<你的request_id>'
```

按顺序把这五处的记录都找出来，填进笔记：

| # | 数据源 | 查什么 | 你查到了什么 |
| --- | --- | --- | --- |
| 1 | 应用日志 | `request_id` 的全部日志行 | |
| 2 | `request_logs` | `WHERE request_id = ...` | |
| 3 | `usage_logs` | 同上，注意 `gift_charge_amount` / `cash_charge_amount` | |
| 4 | `wallet_transactions` | `WHERE ref_request_id = ...` | |
| 5 | 日志里的上游返回 | 上游到底说了什么 | |

```sql
-- 一条 SQL 拉齐三张表
SELECT 'request' AS src, r.status_code::text, r.created_at
FROM request_logs r WHERE r.request_id = '<id>'
UNION ALL
SELECT 'usage', u.charge_amount::text, u.created_at
FROM usage_logs u WHERE u.request_id = '<id>'
UNION ALL
SELECT 'wallet:' || t.type, t.amount::text, t.created_at
FROM wallet_transactions t WHERE t.ref_request_id = '<id>'
ORDER BY created_at;
```

#### Step 4：产出一份排障 SOP（本任务真正的价值）

这是**过关标准直接考的东西**。在笔记里写一份可以直接给同事用的清单：

```
场景：用户反馈「某次调用扣费异常」

0. 问用户要：时间点 / 模型 / 大概金额（拿不到 request_id 时用这三个反查 request_logs）
1. request_id  →  ______
2. request_logs：状态码? 耗时? 走了哪个渠道?
3. usage_logs：token 数? charge_amount? gift/cash 各多少?
4. wallet_transactions：hold / capture / release 三条齐吗? 金额对得上吗?
5. 判定分支：
   - 有 hold 无 capture 无 release  →  ______（阶段 5 D7.1）
   - capture 金额 ≠ usage.charge_amount  →  ______
   - 用户说"我明明有余额"  →  ______（阶段 6：是不是赠金过期了？查 gift_grants）
6. 结论 + 处理动作
```

**第 5 步的三个空是本任务的考点**，不要跳过。

### 任务 3 · 加一个自定义指标（必做，约 2.5 小时）

需求：按模型统计 token 消耗的 Counter，在 `/metrics` 里能看到。

#### Step 0：开分支

```powershell
git checkout -b learn/stage7-token-metric
```

#### Step 1：先做三个决定（写进笔记）

1. **指标名叫什么？** 现有命名是 `tokenhub_<domain>_<thing>_total`。Counter 必须以 `_total` 结尾（Prometheus 约定）。
2. **labels 选什么？** 候选：`model`、`token_type`（input/output/cached）、`user_id`、`source`。
   **`user_id` 是陷阱**——回到 D1.3 想想 cardinality。`model` 有多少个？`token_type` 有几种？
3. **在哪打点？** 候选：
   - `usage_service.write_usage_log`（`:112-150`）——**所有**用量都会经过这里，包括知识库的
   - `api/v1/chat.py`——只有 chat
   现有的 `KB_EMBED_TOKENS`（`metrics.py:101-105`）打在 `embedding_gateway.py:437-440`，可以参考它的取舍。写下你的选择和理由。

#### Step 2：写代码

在 `metrics.py` 里照抄现有 Counter 的写法定义，然后在选定的位置 `.labels(...).inc(token_count)`。

**一共不该超过 10 行。** 如果你写多了，说明选错打点位置了。

#### Step 3：测试

参考 `test_observability.py:93-100` 的 `test_metrics_endpoint` 写法：

```python
async def test_token_metric_exposed(obs_client, db_session):
    # 1. 造一次带 token 的用量
    # 2. GET /metrics
    # 3. assert "tokenhub_xxx_tokens_total" in resp.text
    ...
```

**加分**：再写一条断言具体数值的测试。想想为什么这条更难写——（Counter 是进程级全局累加的，前面的测试可能已经加过了。所以要么先读一次基线再比差值，要么只断言「包含且 > 0」。**这是指标测试的通用难点**，值得在笔记里记一笔）

#### Step 4：验证 + review

```powershell
cd d:\LLM_TokenHub\backend
pytest -q
uvicorn app.main:app --reload
curl.exe http://localhost:8000/metrics | Select-String "tokens_total"
```

```
请 review 这个 diff。重点看：
1. 指标命名是否符合 Prometheus 约定和本项目现有风格
2. label 是否有 cardinality 风险
3. 打点位置是否会漏掉某些调用路径（比如知识库的内部调用）
4. 异常路径下会不会重复计数或漏计数
不要帮我改代码，指出问题即可。
```

```powershell
git checkout main
```

### 任务 4 · 摸清定时任务的调度现状（必做，约 20 分钟）

这个任务只有一个动作：**去 grep 一遍，看看这五个任务到底是谁在调度它们。**

```powershell
# 找 APScheduler / celery / cron 配置
rg -i "apscheduler|celery|crontab|schedule" backend/app docs
```

你会发现：**代码库里没有任何调度器**。应用进程（uvicorn）不会自动跑任何定时任务，全部是 CLI 脚本，靠系统 cron / systemd timer 调度，配置示例在 `docs/DEPLOYMENT.md:235-259`：

| 任务 | 建议频率 |
| --- | --- |
| `reconcile_holds` | 每 10 分钟 |
| `reconcile_payments` | 每 5 分钟 |
| `aggregate_usage_daily --yesterday` | 每日 00:30 UTC |
| `expire_gifts` | 每小时 |
| `reconcile_gifts` | 每日 02:15 |
| `kb_worker --loop` | **常驻进程**，不是 cron |

在笔记里回答：

1. 「用系统 cron 而不是内嵌 APScheduler」这个选择，好处和代价分别是什么？（提示：好处是应用进程无状态、可任意水平扩容；代价是部署多一步、而且**没人保证 cron 真的在跑**）
2. `TECH_DESIGN.md:103` 提过要用 APScheduler 起步，现在没落地。如果让你补上，你会选哪种？为什么？
3. 承接任务 1 Step 4：**怎么监控「cron 本身没跑」这件事？** 写下你的方案。

---

## 三、过关自测

1. 给你一个「用户反馈某次调用扣费异常」的场景，说出完整排查步骤。（任务 2，**本阶段主考题**）
2. Counter / Histogram / Gauge 各举一个本项目的例子，说清为什么用这个类型。（D1.2）
3. `normalize_path` 是干什么的？没有它会发生什么事故？（D1.3）
4. request_id 是怎么做到「绑定一次，全请求内的日志都带上」的？为什么必须 clear？（D2.2）
5. 为什么要日聚合表而不是每次实时算？它的代价是什么、项目怎么补的？（D4.1 / D4.4）
6. 日聚合的幂等和资金操作的幂等，用的是两种不同策略。分别是什么、为什么不能互换？（D4.3）
7. `revenue_amount` 和 `cash_revenue_amount` 有什么区别？毛利用的是哪个？（D5.2）
8. `db_session` fixture 为什么用真库还要回滚？想手工看数据该怎么办？（D6.2 / D6.5）
9. `app.dependency_overrides` 解决了什么问题？写出把 `get_db` 换成测试 session 的三行代码。（D6.3）
10. 这五个定时任务是谁在调度？如果 cron 挂了三天，哪些数据会出问题？（任务 4）

---

## 四、这个阶段常见的坑

- **给指标加 `user_id` / `request_id` 这类 label**：基数爆炸，监控系统会先于业务系统挂掉。
- **只在成功路径打点**：错误率永远是 0，看起来岁月静好。中间件里成功/异常两条路径都要打（`request_id.py:48-51` 和 `:62-65`）。
- **把变量拼进日志消息字符串**：`logger.info(f"耗时 {ms}ms")` 让结构化日志退化成文本日志，没法按字段检索。
- **拿 Gauge 的平稳曲线当健康证明**：更新它的任务挂了，曲线也是平的。
- **在测试里期待数据被持久化**：`db_session` 会 rollback。要手工查数据就发真实 HTTP 请求。
- **忘了 `commit → flush` 那个 trick**：写涉及 commit 的接口测试时，断言会莫名其妙查不到数据（D6.4）。
- **把 `revenue_amount` 当平台收入**：它含赠金。阶段 6 讲过一次，这里再踩一次就说明阶段 6 没过关。
- **以为定时任务会自动跑**：本项目应用进程不带调度器。本地学习时所有任务都得你手动执行。
