"""异步数据库会话管理：懒加载引擎/会话工厂，供依赖注入与生命周期钩子使用。

设计要点：
- 引擎与 sessionmaker 是进程级单例。uvicorn 多 worker 时每个进程各自一份连接池。
- 懒加载：首次 ``get_engine()`` 才 ``create_async_engine``，测试可在 patch settings 后再取。
- ``get_db`` 是 FastAPI 依赖：一个请求一个 AsyncSession。
  成功路径在 yield 之后 commit；任意异常 rollback 再抛出。
- Service 层禁止自行 commit（钱包行锁必须与调用方事务同生共死）。
  例外：chat 网关在预扣后会提前 ``db.commit()``，把冻结落盘，避免上游调用期间事务过长。
- ``expire_on_commit=False``：commit 后仍可读 ORM 属性，避免异步上下文里懒加载触发 MissingGreenlet。
"""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings

# 进程级单例，避免重复创建连接池（每个引擎自带 QueuePool）
_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    """返回进程内唯一 AsyncEngine；首次调用时按 ``settings.database_url`` 创建。"""
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            settings.database_url,
            echo=settings.debug,
            pool_pre_ping=True,  # 借出连接前探测存活，避免使用已断开的连接
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """返回绑定到 ``get_engine()`` 的 sessionmaker。流式结算兜底路径会直接用它开短会话。"""
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            get_engine(),
            class_=AsyncSession,
            # commit 后对象属性仍可访问，便于在事务提交后继续读取 ORM 字段
            expire_on_commit=False,
        )
    return _session_factory


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI 依赖：每个请求独占一个会话，成功时提交，异常时回滚。

    注意：依赖在路由 return 之后才执行 yield 后的 commit。
    流式响应（StreamingResponse）要等生成器结束才会走到这里；
    chat 流式路径因此在 ``_finalize_stream`` 里自行 commit，避免连接被客户端掐断后丢结算。
    """
    session_factory = get_session_factory()
    async with session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def close_db() -> None:
    """关闭连接池并清空单例，供应用 shutdown 与测试隔离。"""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None
