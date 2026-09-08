from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from fastapi import Depends
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.deps import get_auth_context
from app.core.config import settings
from app.core.security import create_admin_token
from app.db.session import get_db
from app.main import app
from app.schemas.auth import AuthContext


def admin_auth_headers() -> dict[str, str]:
    """测试用：签发固定 admin JWT 请求头。"""
    token = create_admin_token(username=settings.admin_username)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def client() -> AsyncClient:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        async with session.begin():
            yield session
            await session.rollback()
    await engine.dispose()


@pytest_asyncio.fixture
async def auth_client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    async def override_get_db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    app.dependency_overrides[get_db] = override_get_db

    @app.get("/test/auth", include_in_schema=False)
    async def _test_auth(auth: AuthContext = Depends(get_auth_context)) -> dict[str, str]:
        return {
            "user_id": str(auth.user_id),
            "billing_subject": auth.billing_subject,
        }

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    app.dependency_overrides.clear()
    app.router.routes = [
        route for route in app.router.routes if getattr(route, "path", None) != "/test/auth"
    ]
