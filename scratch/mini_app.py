from operator import call
from tracemalloc import Snapshot
import uuid

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import _status_codes
from starlette.middleware.base import BaseHTTPMiddleware

# 自定义异常体系

class AppError(Exception):
    status_code = 400
    code = "invalid_request"

    def __init__(self, message):
        self.message = message

class NoMoneyError(AppError):
    status_code = 402
    code = "insufficient_balance"

# 模拟数据库
FAKE_DB = {"balance": 3}

async def get_db():
    print("[B] 依赖：Begin")
    Snapshot = FAKE_DB.copy()
    try:
        yield FAKE_DB
        print("[D] 依赖：Commit")
    except Exception:
        FAKE_DB.update(Snapshot)
        print("[D] 依赖：Rollback(余额已还原)")
        raise

# 中间件
class RequestMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request_id = str(uuid.uuid4())[:8]
        print(f"[A] 中间件~进：request_id={request_id}")
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        print(f"[A] 中间件~出：request_id={request_id}， status={response.status_code}")
        return response

app = FastAPI()
app.add_middleware(RequestMiddleware)

@app.exception_handler(AppError)
async def on_app_error(request: Request, exc: AppError):
    print("[!]异常处理")
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message}}
    )

@app.get("/buy")
async def buy(cost: int = 1, db: dict = Depends(get_db)):
    print(f"  [C] 路由函数：cost={cost} balance={db['balance']}")
    if db['balance'] < cost:
        raise NoMoneyError("余额不足")
    db['balance'] -= cost
    return {"balance": db['balance']}

