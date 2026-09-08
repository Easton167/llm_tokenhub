# 阶段 1 实施细则：框架基座——应用是怎么组装起来的

> 配套 `docs/learning_plan.md` 的「阶段 1」。阅读模式字母（S/Q/T/B）含义见 [README](./README.md#二四种读代码的方式后面每条读单都会标)。
> 前置：阶段 0 已跑通服务和测试。**如果 `@app.get(...)`、`async def`、`yield`、`Depends` 这四样里有你说不清的，先做 [阶段 0.5 前置补课](./stage-0-prerequisites.md)**，否则本阶段会寸步难行。
> 产出物：`docs/learning/notes/stage-1.md`，含 4 份读书笔记 + 1 张请求生命周期图 + 1 个可跑的新接口。
> 行号说明：下面标的 `文件:行号` 以 2026-08 的代码为准，代码变动后会漂移。**对不上就按函数名搜（`Ctrl+Shift+O`），行号只当粗略指路。**

**本阶段一句话**：这一层的代码不做任何业务，它只回答一个问题——「一个 HTTP 请求进来，在到达业务代码之前和之后，框架替你做了什么」。

---

## 一、怎么读

### 读单总览

| # | 文件 | 模式 | 预算 | 核心问题 |
| --- | --- | --- | --- | --- |
| D1 | `app/main.py` | Q | 30 min | 应用是怎么被组装出来的 |
| D2 | `app/core/config.py` | Q | 25 min | 配置从哪来、什么时候固化 |
| D3 | `app/middleware/request_id.py` | Q+B | 40 min | 中间件怎么包住整个请求 |
| D4 | `app/core/errors.py` | Q | 30 min | 异常怎么变成统一 JSON |
| D5 | `app/core/logging.py` | S | 15 min | 日志怎么自动带上 request_id |
| D6 | `app/db/session.py` | Q | 30 min | 事务边界在哪 |
| D7 | `app/api/deps.py` | Q | 20 min | `Depends` 在本项目怎么用 |
| D8 | `tests/conftest.py` | Q | 30 min | 测试怎么替换掉真实依赖 |

总计约 3.5 小时纯阅读，建议分 2–3 次。

---

### D1 · `backend/app/main.py`（108 行，模式 Q）

这是全项目唯一的入口，**从上到下读完，不要跳**。108 行，值得逐行。

**先读 `:1-15` 的 docstring**——它已经把中间件顺序和路由分层写清楚了，等于给了你一张答案卡，后面读代码是去验证它。

带着这 4 个问题读：

1. `create_app()`（`main.py:53-104`）里的几个步骤，**顺序能不能换**？特别是 `add_middleware`（`:63`）、`register_exception_handlers`（`:76`）、`include_router`（`:79-91`）三者。
2. `lifespan`（`main.py:41-50`）里 `yield` 之前和之后分别在什么时刻执行？为什么关闭时要 `await close_db()`（`:50`）？
3. `configure_logging` 被调了**两次**（`:48` 和 `:56`），为什么？去掉哪一次会出问题？（`:55` 的注释给了答案，先自己想再看）
4. `app = create_app()` 在模块级（`main.py:107`）执行意味着什么？和 `uvicorn app.main:app` 这个命令怎么对应上？

读的时候顺手记下路由分组（这是你的模块地图）：

- `/v1/*` —— OpenAI 兼容网关（`:79`，prefix 在 `app/api/v1/__init__.py`）
- `/admin/*` —— 管理后台（`:81`，prefix 在 `app/api/admin/router.py:22`）
- 用户端 REST —— `auth / keys / wallets / usage / orgs / payments / chat_sessions / chat_rag / kb`（`:83-91`），prefix 各自写在自己的 router 文件里
- `/health`、`/metrics` —— 直接挂在 app 上（`:93-102`）

**验证你读懂了**：不看代码，说出「为什么 `/health` 不需要任何鉴权，而 `/admin/providers` 需要」。答案在 `app/api/admin/router.py:30` 的 `_admin_deps = [Depends(enforce_admin_permission)]`——admin 的业务子路由都挂了它（登录接口故意不挂），而 `/health`（`main.py:93`）一个 `Depends` 都没有。权限码怎么从 URL 推出来，阶段 3 再看。

---

### D2 · `backend/app/core/config.py`（模式 Q）

这个文件 162 行，**其中 110 行是字段清单，那是查阅用的，现在不要通读**。只读这 4 个位置：

| 位置 | 看什么 |
| --- | --- |
| `config.py:20-27` | `load_dotenv(...)` 在**模块导入时**就执行了，读的是 `backend/.env`；下面几行把拆分的 `postgresql_*` 拼成连接串 |
| `config.py:36-41` | `model_config`：`env_file`、`case_sensitive=False`、`extra="ignore"` 各是什么效果 |
| `config.py:46-49` | 一个字段的写法范例：`Field(default=..., validation_alias="DATABASE_URL")`，环境变量名和 Python 字段名的映射关系 |
| `config.py:155-162` | `@lru_cache` 的 `get_settings()` + 模块级 `settings = get_settings()` |

问题：

1. `settings` 这个对象**在什么时刻被创建**？（提示：任何一个文件 `from app.core.config import settings` 时）
2. 承上：**改了 `.env` 为什么必须重启服务**？`@lru_cache` 在这里起的是什么作用，去掉它会怎样？
3. `extra="ignore"`（`:40`）意味着 `.env` 里写了一个 Settings 没定义的键会怎样？这是好事还是坏事？（`:33-34` 的注释解释了为什么这里必须设）
4. 扫一眼字段分组，记住这几个后面阶段要用的：`money_scale`（`:69`）、`hold_expire_seconds`（`:67`）、`business_timezone`（`:71`）、`jwt_expire_minutes`（`:104`）。

**动手验证**（2 分钟，比读 10 分钟有用）：

```powershell
cd d:\LLM_TokenHub\backend
python -c "from app.core.config import settings; print(settings.database_url); print(settings.money_scale)"
```

然后把 `.env` 里的 `postgresql_database` 改一个字符，再跑一次上面的命令，观察输出变了；再在**已经启动的 uvicorn** 里访问 `/health`，观察服务还连着老库。改回来。

---

### D3 · `backend/app/middleware/request_id.py`（85 行，模式 Q + B）

**先 Q 后 B**，这是本阶段唯一必须打断点的文件。本节 §Q 的完整读法示范见 [README 的 Q 模式示范](./README.md#完整示范用-q-模式读-appmiddlewarerequest_idpy)。

Q 阶段，读 `dispatch`（`request_id.py:39-84`），回答：

1. `request_id` 的来源有两个（`resolve_request_id`，`:28-33`），分别是什么？为什么要允许客户端传 `X-Request-ID`？
2. `bind_request_context`（`:44-48`）之后，为什么项目里其他文件写 `logger.info("xxx")` 时不用手动传 request_id 也能打出来？（去 `app/core/logging.py:24` 找 `merge_contextvars`）
3. `clear_request_context()` 在成功分支（`:83`）和异常分支（`:68`）**都调用了一次**。如果漏掉异常分支那一次，在 async 服务里会发生什么？
4. `response.headers[REQUEST_ID_HEADER] = request_id`（`:77`）——为什么要回写给客户端？（想想用户来报障时你需要什么）

B 阶段（断点跟读，30 分钟）：

1. 打三个断点：`request_id.py:42`（刚生成 id）、`request_id.py:53`（`call_next` 那一行）、`main.py:96`（health 处理函数内的 return）
2. F5 启动 `FastAPI (backend)` 配置
3. 另开一个终端发请求：

```powershell
curl.exe -i http://localhost:8000/health
curl.exe -i -H "X-Request-ID: my-trace-001" http://localhost:8000/health
```

4. **观察断点命中顺序**，抄进笔记：`42 → 53 →（进入路由）main.py:96 →（返回中间件）77`。
5. 在 Debug Console 里执行 `request.headers.get("x-request-id")`，对比两次请求的值。
6. 看第二次请求的响应头，确认 `X-Request-ID: my-trace-001` 原样返回。

这一步做完，你就真正理解了「中间件是一个包在处理函数外面的洋葱层」，而不是背下来的。

---

### D4 · `backend/app/core/errors.py`（模式 Q）

这个文件的结构非常规整，**先扫结构再精读两处**。

扫（5 分钟）：`errors.py:49-234` 是一长串异常类，每个类只是给基类 `AppError`（`:29`）填三个默认值：`code` / `error_type` / `status_code`。不要一个个读，**只读 `AppError` 基类本身，剩下的看表**：

| 异常类 | HTTP | code |
| --- | --- | --- |
| `InvalidRequestError` | 400 | `invalid_request` |
| `InvalidApiKeyError` | 401 | `invalid_api_key` |
| `UnauthorizedError` | 401 | `unauthorized` |
| `ForbiddenError` | 403 | `forbidden` |
| `InsufficientBalanceError` | **402** | `insufficient_balance` |
| `ModelNotAuthorizedError` | 403 | `model_not_authorized` |
| `ResourceNotFoundError` | 404 | `not_found` |
| `ConflictError` | 409 | `conflict` |
| `RateLimitExceededError` | **429** | `rate_limit_exceeded`（带 `Retry-After` 头） |

（后面阶段 4、5 会反复遇到 402 和 429，现在混个脸熟就行。）

精读两处：

- `error_response(exc)`（`:237-250`）：产出的 JSON 长什么样——`{"error": {"message", "type", "code"}}`。对照 OpenAI 官方错误格式看看，这就是「OpenAI 兼容」的一部分。
- `register_exception_handlers(app)`（`:253-270`）：注册了**两个** handler。第一个吃 `AppError`（`:256`），第二个吃**所有** `Exception`（`:260`），后者把未知异常统一包成 500 `internal_error`。

问题：

1. 业务代码里写 `raise InsufficientBalanceError()`，到客户端收到 402 JSON 之间，经过了哪些函数？（把链路写下来）
2. 为什么要有第二个「兜底 handler」？如果没有它，一个 `KeyError` 会返回什么给客户端，有什么风险？（关键词：堆栈泄露）
3. 项目为什么不直接用 FastAPI 的 `HTTPException`，而自造一套 `AppError`？（提示：看错误响应体的结构要求）

---

### D5 · `backend/app/core/logging.py`（模式 S）

15 分钟扫完，只需拿到三个结论：

1. 输出是 **JSON 到 stdout**（`logging.py:40-47` 的 `JSONRenderer` + `StreamHandler`），不是给人看的彩色日志——因为生产要给日志系统采集。
2. processor 链第一个是 `merge_contextvars`（`:24`），这就是 D3 里 request_id 能自动出现在每条日志里的原因。
3. `bind_request_context` / `clear_request_context`（`:60-66`）是给中间件用的两个门面函数。

不用读懂每个 processor 的作用，用到再查。

---

### D6 · `backend/app/db/session.py`（78 行，模式 Q）

**本阶段第二重要的文件**，后面每个 service 的事务行为都由它决定。逐行读，包括 `:1-11` 的 docstring——那 8 行设计要点是整个项目的事务宪法。

问题：

1. `get_engine()`（`:29-38`）为什么要懒加载单例？`pool_pre_ping=True`（`:36`）解决什么问题？（关键词：数据库连接被中间设备掐断）
2. `expire_on_commit=False`（`:49`）——默认是 `True`，改成 `False` 后，commit 之后再访问 ORM 对象的属性会发生什么变化？为什么异步场景必须这么设？（关键词：commit 后属性懒加载会触发 IO，报 `MissingGreenlet`）
3. **最重要**：`get_db()`（`:54-68`）在 `yield session`（`:64`）之后**自动 commit**（`:65`），异常时 `rollback`（`:66-68`）。这意味着：
   - 路由处理函数里写 `await db.flush()` 而不写 `commit`，数据**也会**落库——什么时候落？
   - 一个请求里改了 A 表又改了 B 表，中途抛异常，A 的改动会不会留下？
   - 那为什么阶段 4 的 `chat.py:224` 还要**显式** `await db.commit()`？（先记下这个问题，阶段 4 会给你答案；`session.py:9` 的注释已经剧透了：预扣必须在调上游之前就落库）

**动手验证**（15 分钟，强烈建议做）：在 `get_db` 的 `:65` 打断点，发一个 `POST /auth/register` 请求，观察断点在处理函数返回**之后**才命中。这就是「依赖注入的 yield 后置逻辑」。

---

### D7 · `backend/app/api/deps.py`（85 行，模式 Q）

全项目的依赖注入范例都在这 85 行里。四个函数：

| 函数 | 行号 | 用在哪 | 认的是什么 |
| --- | --- | --- | --- |
| `get_auth_context` | `28-36` | `/v1/*` 网关 | API Key（`sk-...`） |
| `get_optional_auth_context` | `39-47` | 可选鉴权场景 | API Key，没有就返回 None |
| `get_user_context` | `50-63` | 用户端 REST | JWT |
| `require_model_access` | `66-84` | 需要校验模型权限的路由 | 依赖套依赖 |

问题：

1. 三个鉴权函数都写了 `db: AsyncSession = Depends(get_db)`（如 `:30`）。一个请求里如果有两个依赖都声明了 `Depends(get_db)`，FastAPI 会创建**一个** session 还是两个？（关键词：依赖缓存 / dependency cache；`deps.py:8` 的 docstring 给了答案）
2. `require_model_access`（`:66`）是一个**返回依赖的函数**（依赖工厂）。为什么要用工厂而不是普通依赖？（提示：它需要一个参数 `model_alias`）
3. 阶段 3 会细读这里，现在只需要能回答：`/v1/chat/completions` 和 `/wallets/me` 这两个接口，Bearer 后面跟的东西**不是同一种东西**，分别是什么？

---

### D8 · `backend/tests/conftest.py`（63 行，模式 Q）

这是你以后写每一个测试的模板，**逐行读**。

| Fixture | 行号 | 作用 |
| --- | --- | --- |
| `admin_auth_headers()` | `17-20` | 不是 fixture，是普通函数，签发 admin JWT 头 |
| `client` | `23-27` | 裸 HTTP client，不碰数据库 |
| `db_session` | `30-38` | 独立 session，**用完 rollback**，测试数据不落库 |
| `auth_client` | `41-62` | 关键：`app.dependency_overrides[get_db] = override_get_db`（`:46`） |

问题：

1. `app.dependency_overrides[get_db]`（`:46`）做了什么？为什么这样就能让接口用上测试的 session？
2. `db_session` 里 `async with session.begin(): yield session; await session.rollback()`（`:35-37`）——测试写的数据为什么不会污染数据库？那你手工用 psql 去查会查到吗？
3. `auth_client` 临时注册了一个 `/test/auth` 路由（`:48-53`），teardown 时又把它从 `app.router.routes` 里摘掉（`:60-62`）。为什么必须摘？（提示：`app` 是模块级单例，所有测试共享）
4. 测试**连的是真实 Postgres**（`:32` 用 `settings.database_url`）。这带来什么代价？跑测试前必须准备什么？

---

## 二、怎么做

### 任务 1 · 断点跟踪一次完整请求（必做，约 40 分钟）

见 D3 的 B 阶段。做完在笔记里画一张图（手画拍照或 mermaid 都行），标出下面每一步发生在哪个文件哪一行：

```
客户端
  → CORSMiddleware（如果 .env 配了 CORS_ORIGINS）
  → RequestIdMiddleware.dispatch 前半段   [request_id.py:42-48]
  → 路由匹配
  → 依赖注入：get_db 建 session          [session.py:61-64]
  → 依赖注入：get_auth_context / get_user_context（若有）
  → 路由处理函数
  → get_db 后置：commit 或 rollback       [session.py:65-68]
  → （若抛异常）异常处理器                [errors.py:253-270]
  → RequestIdMiddleware.dispatch 后半段   [request_id.py:70-84]
  → 客户端
```

**这张图后面每个阶段都会用到，画仔细一点。**

### 任务 2 · 新增 `GET /health/detail`（必做，约 2–3 小时）

需求：返回数据库和 Redis 的连通状态。

#### Step 0：开分支

```powershell
cd d:\LLM_TokenHub
git checkout -b learn/stage1-health-detail
```

#### Step 1：先想清楚三个设计问题（写进笔记再动手）

1. **放哪个文件？** 现有 `/health` 内联写在 `main.py:93-96`。你有两个选择：跟着写在 `main.py` 里，或者新建一个 `app/api/health.py` router。写下你选哪个和理由。（提示：`main.py` 现在只有 108 行，很干净；一个探活接口值不值得开新文件？）
2. **依赖注入怎么写？** DB 检查需要 session。用 `db: AsyncSession = Depends(get_db)`——但注意 `get_db` 在退出时会 `commit`，探活接口 commit 一个空事务有没有问题？
3. **失败时返回什么状态码？** 依赖挂了应该返回 200 带 `{"database": "down"}`，还是返回 503？两种都合理，写下你的选择和理由（提示：K8s 的 liveness probe 期望什么）。

#### Step 2：DB 探活怎么写

最省事且正确的做法是执行一条最轻的 SQL：

```python
from sqlalchemy import text

try:
    await db.execute(text("SELECT 1"))
    db_status = "ok"
except Exception:
    db_status = "down"
```

思考：为什么用 `SELECT 1` 而不是 `select(User).limit(1)`？（提示：探活不应该依赖任何业务表存在）

#### Step 3：Redis 探活怎么写

先去看项目里唯一用 Redis 的地方：`app/services/rate_limit_service.py:208-236` 的 `_ensure_backend()`。读完你会发现两个事实：

- 项目**没有**全局 Redis 客户端，连接是 `rate_limit_service` 内部懒建的
- 这个方法返回 `"redis"` 或 `"memory"`，而且**结果会被缓存在 `self._backend`**（`:209-210`）

于是你要做一个判断：

- 复用 `rate_limit_service._ensure_backend()`？—— 一行搞定，但它是私有方法（下划线开头），而且**有缓存**，Redis 后来挂了它还会返回 `"redis"`，探活就失真了。
- 自己 `from_url(...).ping()`？—— 多几行，但每次都是真实探测。

**推荐后者**，探活的价值就在于反映此刻的真实状态：

```python
import redis.asyncio as redis_async

client = redis_async.from_url(settings.redis_url, socket_connect_timeout=0.5, socket_timeout=0.5)
try:
    await client.ping()
    redis_status = "ok"
except Exception:
    redis_status = "down"
finally:
    await client.aclose()
```

注意 `socket_connect_timeout=0.5` 是抄 `rate_limit_service.py:226-227` 的——**探活接口绝对不能因为依赖挂了就把自己卡死**，这是这个练习真正要教你的点。

#### Step 4：写测试

在 `backend/tests/test_health.py` 里追加（先读一遍现有的 6 行，照抄风格）：

```python
async def test_health_detail_reports_dependencies(client):
    resp = await client.get("/health/detail")
    assert resp.status_code == 200
    body = resp.json()
    assert body["database"] in {"ok", "down"}
    assert body["redis"] in {"ok", "down"}
```

三个要点：

- 用 `client` fixture 而不是 `auth_client`——探活不需要鉴权，也不需要覆盖 `get_db`
- **不要**写 `@pytest.mark.asyncio`，`pyproject.toml:47` 已经设了 `asyncio_mode = "auto"`
- 断言写成 `in {"ok", "down"}` 而不是 `== "ok"`：本机 Redis 没起也应该能跑过，测试断的是「接口能正确报告状态」，不是「依赖必须活着」

进阶（可选，能做出来说明你 D8 读透了）：再写一个测试，用 `monkeypatch` 让 DB 探活抛异常，断言返回的是 `"down"` 而不是 500。

#### Step 5：跑起来验证

```powershell
cd d:\LLM_TokenHub\backend
pytest tests/test_health.py -v
uvicorn app.main:app --reload
# 另一个终端
curl.exe http://localhost:8000/health/detail
```

再做一次**故障验证**（这一步不要跳）：把 Redis 停掉（或把 `.env` 的 `REDIS_URL` 改成一个不存在的端口，重启服务），再请求一次，确认：

- 接口**没有卡住**（响应在 1 秒内返回，因为你设了 0.5s 超时）
- 返回 `redis: "down"` 而 `database: "ok"`

#### Step 6：让 AI review，然后收工

```
请 review 这个 diff。重点看：探活接口的超时设置是否合理、异常捕获是否过宽、
返回的状态码语义是否符合探活接口惯例。不要帮我改代码，指出问题即可。
```

理解并回应每一条意见后：

```powershell
git checkout main
```

---

## 三、过关自测

不看代码回答，答不上来就回去重读括号里标的读单条目。写进 `notes/stage-1.md`。

1. 一个 HTTP 请求从进入到返回，依次经过哪些层？每一层对应哪个文件？（D1/D3/D6/D7）
2. `settings` 是什么时候被实例化的？改 `.env` 后为什么要重启？（D2）
3. 我在某个 service 里写 `logger.info("charge_captured", amount=100)`，最终打出的 JSON 里为什么会有 `request_id`？（D3/D5）
4. 一个路由函数里连续改了两张表，中间抛了异常，数据库里会留下什么？代码依据在哪一行？（D6）
5. 业务代码 `raise ForbiddenError("xxx")` 后，客户端收到的 HTTP 状态码和 JSON body 分别是什么？（D4）
6. 写一个需要登录态的新接口，我应该 `Depends` 哪个函数？如果是 `/v1` 下的网关接口呢？（D7）
7. 我写的测试往数据库插了 10 条数据，跑完测试后用 psql 去查，能查到吗？为什么？（D8）

---

## 四、这个阶段常见的坑

- **断点打不上**：去掉 `--reload`；确认 `justMyCode: false`；确认调试用的是 `backend/.venv` 的解释器（Cursor 右下角可切换）。
- **`pytest` 报连不上数据库**：Postgres 没起，或 `.env` 里的库名/密码不对。测试不走内存库，这点和很多教程不一样。
- **改了代码接口没变**：`--reload` 只监听 `backend/` 下的 `.py`，改 `.env` 不会触发重载。
- **想给 `/health/detail` 加鉴权**：不要加。探活接口加鉴权会让监控系统没法用，这是运维层面的常识性错误。
