# 阶段 0.5 前置补课：读这个项目之前，你必须先会的 8 件事

> 给谁看：**Python 自学半年左右**，语法（变量、函数、类、列表推导、try/except）没问题，但打开 `app/main.py` 会觉得「每一行都认识，连起来不知道在干嘛」的人。
> 学习计划原本假设读者「会 FastAPI 语法」。如果你不满足，直接从阶段 1 开始一定会卡死——**不是你不行，是缺了这一层**。
> 投入：8–12 小时，一两个周末。**这是整份计划里性价比最高的时间**，做完再回阶段 1，速度会快一倍不止。

---

## 零、先诊断：你到底缺什么

下面 10 句话，**不查资料**，看看你能不能用一句人话解释给别人听。在纸上打勾。

| # | 问题 | 不会 → 去看 |
| --- | --- | --- |
| 1 | `@app.get("/health")` 里那个 `@` 到底做了什么？ | §1 装饰器 |
| 2 | 函数前面加 `async`，和不加有什么区别？什么时候必须写 `await`？ | §2 async/await |
| 3 | 函数里有 `yield` 而不是 `return`，这个函数被调用时会发生什么？ | §3 yield |
| 4 | `with open(...) as f:` 里的 `as f` 和 `async with` 的区别？ | §3 yield |
| 5 | `def f(x: int) -> str:` 里的 `int` 和 `str`，Python 运行时会检查吗？ | §4 类型注解 |
| 6 | `db: AsyncSession = Depends(get_db)` 这一行，`get_db` 是谁调用的？ | §5 Depends |
| 7 | `str \| None` 和 `Optional[str]` 是不是一回事？ | §4 类型注解 |
| 8 | pydantic 的 `BaseModel` 和普通的 `class` 差在哪？ | §6 pydantic |
| 9 | `class InvalidApiKeyError(AppError)` 括号里的 `AppError` 是什么意思？ | §7 继承与自定义异常 |
| 10 | `await db.execute(select(User))` 返回的是 User 对象吗？ | §8 SQLAlchemy |

**打勾少于 6 个 → 老老实实把本文从头做一遍。**
打勾 6–8 个 → 只补没打勾的小节，然后直接做 §9 的迷你项目。
打勾 9–10 个 → 跳过本文，去阶段 1。

> 下面每一节的结构都一样：**是什么（一句话）→ 最小例子（你要真的敲进终端跑）→ 项目里在哪一行 → 怎么算懂了**。
> 例子都很短，**必须手敲运行**，看是没用的。先建一个专放练习文件的目录（用完直接删，别提交）：
>
> ```powershell
> cd d:\LLM_TokenHub
> mkdir scratch
> ```
>
> 文件名**不要用数字开头**（`01_xxx.py` 这种）：Python 的模块名不能以数字开头，后面用 uvicorn 启动时会导入失败。

---

## 一、装饰器：`@` 是「把函数交给另一个函数改造一下」

### 是什么

`@foo` 写在 `def bar` 上面，等价于 `bar = foo(bar)`。就这一句话，没有别的魔法。

### 最小例子

新建 `scratch/try_decorator.py`：

```python
def loud(func):
    def wrapper(*args, **kwargs):
        print("调用前")
        result = func(*args, **kwargs)
        print("调用后")
        return result
    return wrapper

@loud
def add(a, b):
    return a + b

print(add(1, 2))
```

跑一下：

```powershell
python scratch/try_decorator.py
```

输出是 `调用前 / 调用后 / 3`。现在把 `@loud` 那行删掉再跑，对比差异。

**关键认知**：`add` 这个名字后来指向的已经不是你写的那个函数了，而是 `wrapper`。

### 变体：带参数的装饰器（项目里到处都是）

```python
def repeat(times):          # 第一层：收装饰器的参数
    def decorator(func):    # 第二层：收被装饰的函数
        def wrapper(*a, **kw):
            for _ in range(times):
                func(*a, **kw)
        return wrapper
    return decorator

@repeat(3)
def hi():
    print("hi")

hi()
```

`@app.get("/health")` 就是这个结构：`app.get("/health")` 先执行、返回一个装饰器，再去装饰 `health` 函数。

### 项目里在哪

- `backend/app/main.py:93` `@app.get("/health")` —— FastAPI 用它把函数登记进路由表
- `backend/app/core/config.py:155` `@lru_cache` —— 标准库的缓存装饰器，让 `get_settings()` 只真正执行一次
- `backend/app/core/errors.py:256` `@app.exception_handler(AppError)` —— 登记异常处理器
- `backend/tests/conftest.py:23` `@pytest.fixture` —— 登记测试夹具

### 怎么算懂了

能回答：**`@app.get("/health")` 有没有「调用」`health` 函数？** （没有。它只是把函数存进 FastAPI 的路由表，等 HTTP 请求来了才调用。）

---

## 二、async / await：一句话是「这个函数中途可以先让出去干别的」

### 是什么

- `async def f()` 定义的叫**协程函数**。**调用它不会执行函数体**，只会返回一个「协程对象」。
- 只有 `await` 它，或者交给事件循环跑，函数体才真的开始执行。
- `await` 只能写在 `async def` 里面。
- 一个进程只有一个线程在跑，但遇到 `await`（等网络、等数据库）时会切去处理别的请求——这就是「异步」省的钱。

### 最小例子（这个例子非跑不可）

`scratch/try_async.py`：

```python
import asyncio, time

async def slow(name, sec):
    print(f"{name} 开始")
    await asyncio.sleep(sec)      # 模拟等数据库/等上游 API
    print(f"{name} 结束")
    return name

async def main():
    t = time.perf_counter()
    # 写法 A：一个一个等
    await slow("A", 1)
    await slow("B", 1)
    print("串行耗时", round(time.perf_counter() - t, 2))

    t = time.perf_counter()
    # 写法 B：一起跑
    await asyncio.gather(slow("C", 1), slow("D", 1))
    print("并发耗时", round(time.perf_counter() - t, 2))

asyncio.run(main())
```

串行 2 秒，并发 1 秒。**再做一个实验**：把 `slow` 里的 `await asyncio.sleep(sec)` 换成 `time.sleep(sec)`，重跑——并发也变成 2 秒了。

这个实验解释了本项目最重要的一条纪律：**在 async 函数里绝对不能调用会阻塞的同步 IO**，否则整个服务被你一个人卡住。

### 三个新手必踩的坑

```python
result = slow("A", 1)        # 坑 1：忘了 await，result 是个协程对象，函数体根本没跑
                             #       控制台会警告 "coroutine was never awaited"
def normal():
    await slow("A", 1)       # 坑 2：SyntaxError，普通 def 里不能 await

async def g():
    return 1
print(g())                   # 坑 3：打印出 <coroutine object ...>，不是 1
```

### 项目里在哪

- `backend/app/main.py:94` `async def health()` —— 所有路由函数都是 async
- `backend/app/db/session.py:65` `await session.commit()` —— 数据库操作全是 async，必须 await
- `backend/app/middleware/request_id.py:53` `response = await call_next(request)` —— 中间件把控制权交给下一层

### 怎么算懂了

能回答：**`app/api/deps.py:36` 写的是 `return await auth_service.verify_api_key(db, raw_key)`，如果漏了 `await` 会怎样？**
（返回的是协程对象而不是 `AuthContext`，后面访问 `auth.user_id` 直接 AttributeError，而且大概率报 "coroutine was never awaited"。）

---

## 三、`yield`：搞懂它，就搞懂了本项目一半的「奇怪写法」

`yield` 在项目里有三种面孔，但内核是同一个：**函数执行到 `yield` 会暂停并交出一个值，外面用完了再回来接着执行后半段。**

### 3.1 生成器（基础形态）

`scratch/try_yield.py`：

```python
def gen():
    print("前半段")
    yield 1
    print("后半段")
    yield 2
    print("结束")

g = gen()                 # 注意：这一行什么都没打印！
print("拿到生成器了")
print(next(g))            # 打印「前半段」，然后 1
print(next(g))            # 打印「后半段」，然后 2
```

**关键认知**：调用带 `yield` 的函数**不执行函数体**，只造出一个生成器。这和 §2 的 `async def` 是同一种「延迟执行」的思路。

### 3.2 上下文管理器：`with` 的本质就是「前半段 + 后半段」

```python
from contextlib import contextmanager

@contextmanager
def transaction():
    print("BEGIN")
    try:
        yield "会话对象"
        print("COMMIT")
    except Exception:
        print("ROLLBACK")
        raise

with transaction() as sess:
    print("干活中，拿到了", sess)

print("---- 下面故意报错 ----")
try:
    with transaction() as sess:
        raise ValueError("炸了")
except ValueError:
    print("异常被外面接住了")
```

跑一遍，把输出顺序抄下来。**你刚刚手写的，就是 `app/db/session.py:54-68` 的 `get_db()`**——一模一样的结构，只是它是 async 版本。

### 3.3 async 版本

```python
import asyncio
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan():
    print("启动：配置日志")
    yield
    print("关闭：释放连接池")

async def main():
    async with lifespan():
        print("服务运行中……")

asyncio.run(main())
```

这就是 `app/main.py:41-50` 的 `lifespan`，逐字对应。

### 项目里在哪

| 形态 | 位置 |
| --- | --- |
| `@asynccontextmanager` 生命周期 | `app/main.py:41-50` |
| `async with` 用会话 | `app/db/session.py:62` |
| 带 yield 的 FastAPI 依赖 | `app/db/session.py:54-68` |
| 测试 fixture 的 yield | `tests/conftest.py:27` `tests/conftest.py:36` |

### 怎么算懂了

能回答：**`get_db()` 里 `yield session` 后面那句 `await session.commit()`，是在路由函数返回之前执行还是之后？**（之后。整个路由函数就是「with 语句块里的内容」。）

---

## 四、类型注解：在 FastAPI 里它不是注释，是**声明**

### 是什么

纯 Python 里，`def f(x: int) -> str` 的注解**运行时完全不检查**，传字符串照样跑。

```python
def f(x: int) -> str:
    return x + 1
print(f(10))     # 11，不报错，注解只是给人和编辑器看的
```

**但是** FastAPI 和 pydantic 会去**读**这些注解，用它们决定：参数从哪来（路径/查询/请求体/请求头）、怎么校验、怎么生成 Swagger 文档。所以在这个项目里，改一个注解 = 改行为。

### 你会在项目里见到的 5 种写法

```python
str | None                                  # 「字符串或者 None」，等价于旧写法 Optional[str]
list[str]                                   # 字符串列表，等价于旧写法 List[str]
dict[str, str]                              # app/main.py:94 的返回注解
Annotated[str | None, Header()]             # app/api/deps.py:29 「这个参数从 HTTP 请求头里取」
AsyncGenerator[AsyncSession, None]          # app/db/session.py:54 「这是个 async 生成器，产出 AsyncSession」
```

`from __future__ import annotations`（见 `app/middleware/request_id.py:11`）的作用：让所有注解变成纯字符串、不在导入时求值，可以放心用还没定义的类名。**看到它就忽略它**，不影响你读逻辑。

### 一个能立刻体会到差别的实验

`scratch/try_types.py`：

```python
from fastapi import FastAPI

app = FastAPI()

@app.get("/items/{item_id}")
async def read(item_id: int, q: str | None = None):
    return {"item_id": item_id, "type": str(type(item_id)), "q": q}
```

```powershell
python -m uvicorn scratch.try_types:app --port 8001
# 换个终端
curl.exe "http://localhost:8001/items/42?q=hello"
curl.exe "http://localhost:8001/items/abc"
```

第一个返回 `item_id` 是 **int**（URL 里明明是字符串，FastAPI 按注解转的）；第二个直接返回 422 校验错误。**注解就是这样变成行为的。**

### 怎么算懂了

能回答：**`app/api/deps.py:29` 的 `authorization: Annotated[str | None, Header()] = None`，为什么 FastAPI 知道要去 HTTP 头里找它，而不是当成查询参数？**

---

## 五、`Depends`：FastAPI 帮你调函数、传结果、管清理

### 是什么

`def route(db = Depends(get_db))` 的意思是：**别人（FastAPI）在调用 `route` 之前，会先帮我调用 `get_db()`，把结果作为 `db` 参数塞进来。** 你自己永远不写 `get_db()`。

三条必须记住的规则：

1. **依赖可以套依赖**。`get_auth_context` 自己也声明了 `Depends(get_db)`（`app/api/deps.py:30`），FastAPI 会递归解决。
2. **同一个请求里，同一个依赖只执行一次**（依赖缓存）。所以一个请求里三个地方都 `Depends(get_db)`，拿到的是**同一个** session、同一个事务。
3. **带 yield 的依赖**，yield 前的代码在路由函数前跑，yield 后的代码在路由函数返回后跑（见 §3）。

### 最小例子（跑起来看顺序）

`scratch/try_depends.py`：

```python
from fastapi import Depends, FastAPI

app = FastAPI()
counter = {"n": 0}

async def get_conn():
    counter["n"] += 1
    print(f"  [依赖] 开连接 #{counter['n']}")
    yield f"conn-{counter['n']}"
    print(f"  [依赖] 关连接 #{counter['n']}")

async def get_user(conn=Depends(get_conn)):
    print(f"  [依赖] get_user 用的是 {conn}")
    return "alice"

@app.get("/who")
async def who(user=Depends(get_user), conn=Depends(get_conn)):
    print(f"  [路由] user={user}, conn={conn}")
    return {"user": user, "conn": conn}
```

```powershell
python -m uvicorn scratch.try_depends:app --port 8001
curl.exe http://localhost:8001/who
```

**观察 `[依赖] 开连接` 打了几次**。答案是一次——尽管 `get_conn` 被声明了两处。这就是依赖缓存，也是本项目「一个请求一个事务」能成立的前提。

### 项目里在哪

- `app/api/deps.py` 整个文件 —— 全项目依赖的范本
- `tests/conftest.py:46` `app.dependency_overrides[get_db] = override_get_db` —— **测试时把依赖换成假的**，这是 `Depends` 最大的价值

### 怎么算懂了

能回答：**为什么有了 `Depends`，写测试时不用真的连生产数据库？**

---

## 六、pydantic：会自己校验和转换的「数据类」

### 是什么

`BaseModel` 的子类，在**创建实例时**按注解校验并转换数据；不合法直接抛 `ValidationError`。项目用它做三件事：请求体校验、响应体序列化、读配置。

`scratch/try_pydantic.py`：

```python
from pydantic import BaseModel, Field, ValidationError

class ChatRequest(BaseModel):
    model: str
    max_tokens: int = 100
    temperature: float = Field(default=1.0, ge=0, le=2)

print(ChatRequest(model="gpt-4o", max_tokens="256"))   # 字符串 "256" 被自动转成 int 256
try:
    
    ChatRequest(model="gpt-4o", temperature=99)
except ValidationError as e:
    print(e)                                            # temperature 超范围，报错
```

### `BaseSettings`：同一套机制，数据源换成环境变量

`app/core/config.py` 的 `Settings` 继承的是 `BaseSettings`，它会自动去环境变量 / `.env` 里找同名的值。`Field(validation_alias="DATABASE_URL")`（`config.py:46-49`）就是在说「这个字段去读名为 `DATABASE_URL` 的环境变量」。

**动手 2 分钟**：

```powershell
cd d:\LLM_TokenHub\backend
python -c "from app.core.config import settings; print(settings.database_url); print(settings.money_scale)"
```

### 怎么算懂了

能回答：**普通 `class` 和 `BaseModel` 最大的区别是什么？**（前者你传什么存什么，后者会校验+转换+能一键 `.model_dump()` 成 dict。）

---

## 七、继承与自定义异常：为什么项目有 20 多个 Error 类

### 是什么

`class InvalidApiKeyError(AppError)` 表示「它**是一种** AppError」，自动拥有父类的一切。好处是：捕获时写 `except AppError` 就能一网打尽所有子类。

`scratch/try_errors.py`：

```python
class AppError(Exception):
    status_code = 500
    code = "internal_error"
    def __init__(self, message="出错了"):
        self.message = message
        super().__init__(message)

class NoMoneyError(AppError):
    status_code = 402
    code = "insufficient_balance"

class ForbiddenError(AppError):
    status_code = 403
    code = "forbidden"

for exc in (NoMoneyError("余额不足"), ForbiddenError("没权限")):
    try:
        raise exc
    except AppError as e:                   # 一个 except 接住所有子类
        print(type(e).__name__, e.status_code, e.code, e.message)
    print(isinstance(exc, AppError))        # True
```

### 项目里在哪

`app/core/errors.py:29` 是基类 `AppError`，`:49-234` 是一长串子类，每个只改三个值（`code` / `error_type` / `status_code`）。`:253-270` 注册了两个处理器：一个接 `AppError`，一个兜底接所有 `Exception`。

**这就是为什么业务代码里可以随手 `raise InsufficientBalanceError()`**——不用管 HTTP，转成 402 JSON 是框架层的事。

### 怎么算懂了

能回答：**如果 `errors.py:260` 那个兜底的 `@app.exception_handler(Exception)` 不存在，一个 `KeyError` 会给客户端返回什么？有什么风险？**

---

## 八、SQLAlchemy 异步查询三板斧

项目里 99% 的数据库读操作都是这三行的排列组合，**先背下形状，细节留到阶段 2**：

```python
from sqlalchemy import select

# 1. 拼一个 select 语句（这一步不碰数据库）
stmt = select(User).where(User.email == "a@b.com")

# 2. 执行（这一步才真的发 SQL，必须 await）
result = await db.execute(stmt)

# 3. 从结果里取对象
user = result.scalar_one_or_none()   # 0 或 1 条：拿到 User 或 None
users = result.scalars().all()       # 多条：拿到 list[User]
```

三个新手必知：

- `await db.execute(...)` 返回的是**结果包装对象**，不是 User，必须再 `.scalar_one_or_none()` / `.scalars().all()` 剥一层。
- 写操作：`db.add(obj)` 是同步的（只是放进内存待提交），`await db.flush()` 才发 SQL，`await db.commit()` 才落库。
- 本项目 **service 层不许自己 commit**（`app/db/session.py:8` 的注释），统一由 `get_db()` 在请求结束时提交。

### 怎么算懂了

能回答：**`db.add(user)` 之后立刻用 psql 去查，能查到吗？**（不能。要等 commit。）

---

## 九、毕业作业：60 行手搓一个「本项目的骨架」

**这是本文最重要的一节。** 把上面 8 个概念全用一遍，做完你再看 `app/main.py` 会有「哦，就这」的感觉。

要求：**不许复制粘贴，一行一行手敲。** 敲完跑起来，观察打印顺序。

`scratch/mini_app.py`：

```python
"""一个 1% 版本的 LLM_TokenHub：中间件 + 依赖 + 事务 + 自定义异常。"""
import uuid

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware


# ---- 1. 自定义异常体系（对应 app/core/errors.py）----
class AppError(Exception):
    status_code, code = 400, "invalid_request"

    def __init__(self, message):
        self.message = message


class NoMoneyError(AppError):
    status_code, code = 402, "insufficient_balance"


# ---- 2. 假数据库 + 带 yield 的依赖（对应 app/db/session.py）----
FAKE_DB = {"balance": 3}


async def get_db():
    print("  [B] 依赖：BEGIN")
    snapshot = FAKE_DB.copy()
    try:
        yield FAKE_DB
        print("  [D] 依赖：COMMIT")
    except Exception:
        FAKE_DB.update(snapshot)
        print("  [D] 依赖：ROLLBACK（余额已还原）")
        raise


# ---- 3. 中间件（对应 app/middleware/request_id.py）----
class RequestIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        rid = str(uuid.uuid4())[:8]
        print(f"[A] 中间件·进 rid={rid}")
        response = await call_next(request)
        response.headers["X-Request-ID"] = rid
        print(f"[E] 中间件·出 rid={rid} status={response.status_code}")
        return response


# ---- 4. 组装（对应 app/main.py 的 create_app）----
app = FastAPI()
app.add_middleware(RequestIdMiddleware)


@app.exception_handler(AppError)
async def on_app_error(request: Request, exc: AppError):
    print("  [!] 异常处理器")
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"message": exc.message, "code": exc.code}},
    )


@app.get("/buy")
async def buy(cost: int = 1, db: dict = Depends(get_db)):
    print(f"  [C] 路由函数：cost={cost} balance={db['balance']}")
    if db["balance"] < cost:
        raise NoMoneyError("余额不足")
    db["balance"] -= cost
    return {"balance": db["balance"]}
```

跑：

```powershell
cd d:\LLM_TokenHub
python -m uvicorn scratch.mini_app:app --port 8001
# 另一个终端，依次执行
curl.exe -i "http://localhost:8001/buy?cost=1"
curl.exe -i "http://localhost:8001/buy?cost=99"
```

### 必须完成的观察（写进笔记）

1. 成功请求时，`[A] [B] [C] [D] [E]` 的**实际打印顺序**是什么？和你预想的一样吗？
2. `cost=99` 失败时，顺序变成了什么？`[!]` 出现在 `[D]` 前面还是后面？（**不要猜，跑出来抄下来**——真实框架的行为和直觉常常不一样，这正是要跑一遍的原因。）
3. 失败那次，`FAKE_DB["balance"]` 有没有被改？为什么？
4. 两次响应头里都有 `X-Request-ID` 吗？包括 402 那次吗？为什么中间件要写在最外层？
5. 把 `cost: int = 1` 改成 `cost: str = "1"`，重跑 `?cost=99`，会发生什么？（提示：`"99" < "3"` 在字符串比较里是 True）

### 自检清单

做完对照，全部答得上来才算过：

- [ ] 我能说出 `@app.get("/buy")` 做了什么，以及它**没有**做什么
- [ ] 我能说出 `Depends(get_db)` 里 `get_db` 是谁在什么时候调用的
- [ ] 我能说出 `yield` 后面那行代码的执行时机
- [ ] 我能说出为什么 `raise NoMoneyError(...)` 不用自己写 402
- [ ] 我能说出中间件为什么能拿到「所有请求」，包括报错的和 404 的

---

## 十、过关之后

把 `scratch/mini_app.py` 和 `backend/app/main.py` 并排放着看一遍。你会发现真实项目只是多了：更多路由、更多依赖、真的数据库。**骨架是一模一样的。**

然后按这个顺序走：

1. 回到 [`docs/learning/README.md`](./README.md)，读「怎么读代码」和「卡住了怎么办」两节
2. 做阶段 0（`docs/learning_plan.md` 的阶段 0）：把项目跑起来
3. 进 [`stage-1-framework.md`](./stage-1-framework.md)

**时间预算提醒**：README 和各阶段细则里标的时长，是按「3 个月 Python + 会 FastAPI」估的。你补完这一课之后，前三个阶段仍然建议**按 1.5 倍计划时间**，慢不是问题，跳过才是。

### 遇到卡壳时的求助模板（照抄，把 xxx 换掉）

```
我在读 LLM_TokenHub 项目的 backend/app/xxx.py 第 NN 行：
<把那几行代码贴上>
我的理解是：<写出你的猜测，哪怕是错的>
但我不明白 <具体到某个符号/某个词>。
请用一个不依赖 FastAPI 的最小 Python 例子解释这个语法点，
然后再告诉我这个项目为什么要这么写、不这么写会怎样。不要给我改代码。
```

**关键是「写出你的猜测」**。不带猜测地问「这段什么意思」，你得到的是一篇看完就忘的说明书；带着猜测问，你得到的是对你脑子里那张错图的精准修正。

说明：本文件我已经学完了
