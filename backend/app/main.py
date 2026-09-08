"""FastAPI 应用入口：组装生命周期、异常处理与路由。

进程启动后唯一对外的 ASGI 对象是模块级 ``app``（uvicorn 用 ``app.main:app``）。
``create_app()`` 可被测试复用以拿到干净实例；生产只创建一次。

中间件顺序（Starlette 后添加的在外层先执行）：
  1. CORSMiddleware（若配置了 cors_origins）
  2. RequestIdMiddleware —— 必须覆盖全部请求，保证日志与响应头都有 X-Request-ID

路由分层：
  - ``/v1/*``：OpenAI 兼容网关（API Key Bearer）
  - ``/admin/*``：管理后台（独立 admin JWT）
  - 其余：前台用户 REST（用户 JWT）
  - ``/health`` ``/metrics``：探活与 Prometheus，故意不鉴权
"""

from contextlib import asynccontextmanager
from sqlalchemy import text

from fastapi import FastAPI, Response, Depends
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import AsyncSession
import redis.asyncio as redis_asyncio

from app.api.auth import router as auth_router
from app.api.admin.router import admin_router
from app.api.chat_rag import router as chat_rag_router
from app.api.chat_sessions import router as chat_sessions_router
from app.api.keys import router as keys_router
from app.api.kb import router as kb_router
from app.api.orgs import router as orgs_router
from app.api.payments import router as payments_router
from app.api.usage import router as usage_router
from app.api.v1 import v1_router
from app.api.wallets import router as wallets_router
from app.core.config import settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging
from app.core.metrics import metrics_payload
from app.db.session import get_db
from app.db.session import close_db
from app.middleware.request_id import RequestIdMiddleware


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """应用生命周期：启动时配日志，关闭时释放数据库连接池。

    引擎是懒加载的（见 ``db.session``），启动阶段不强连数据库，
    避免配置错误时进程起不来。关闭必须 dispose，否则 asyncpg 连接泄漏。
    """
    configure_logging(debug=settings.debug)
    yield
    await close_db()


def create_app() -> FastAPI:
    """构造并返回 FastAPI 实例；不在 import 时产生副作用以外的全局状态。"""
    # lifespan 里也会调一次；此处保证即便不走 lifespan（部分测试）日志也可用
    configure_logging(debug=settings.debug)
    app = FastAPI(
        title=settings.app_name,
        debug=settings.debug,
        lifespan=lifespan,
    )
    # 先注册中间件，确保所有请求都有 request_id（含 404 / 未匹配路由）
    app.add_middleware(RequestIdMiddleware)

    # 逗号分隔来源；空字符串 = 不挂 CORS（纯 API 部署 / 同域反代场景）
    origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    register_exception_handlers(app)

    # OpenAI 兼容对外接口（/v1/models、/v1/chat/completions、/v1/embeddings）
    app.include_router(v1_router)
    # 管理后台接口
    app.include_router(admin_router)
    # 用户端：认证 / Key / 钱包 / 用量 / 组织 / 支付 / Chat 会话 / RAG / 知识库
    app.include_router(auth_router)
    app.include_router(keys_router)
    app.include_router(wallets_router)
    app.include_router(usage_router)
    app.include_router(orgs_router)
    app.include_router(payments_router)
    app.include_router(chat_sessions_router)
    app.include_router(chat_rag_router)
    app.include_router(kb_router)

    @app.get("/health")
    async def health() -> dict[str, str]:
        """进程探活。不查库、不查 Redis，避免把依赖故障误报成进程挂了。"""
        return {"status": "ok"}
    
    @app.get("/health/db")
    async def health_db(db: AsyncSession = Depends(get_db)) -> dict[str, str]:
        """数据库探活。"""
        try:
            await db.execute(text("SELECT 1"))
            return {"status": "ok"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    @app.get("/health/redis")
    async def health_redis() -> dict[str, str]:
        """Redis探活。"""
        try:
            client = redis_asyncio.from_url(settings.redis_url, socket_connect_timeout=0.5, socket_timeout=0.5)
            await client.ping()
            return {"status": "ok"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    @app.get("/metrics")
    async def metrics() -> Response:
        """Prometheus 文本格式。路径里的 UUID 已在中间件侧折叠，避免高基数。"""
        body, content_type = metrics_payload()
        return Response(content=body, media_type=content_type)

    return app


app = create_app()
