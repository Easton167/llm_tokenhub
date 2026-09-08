"""用户端 Chat 会话持久化 API。"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.main import app


@pytest_asyncio.fixture
async def user_client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    original_commit = db_session.commit

    async def _fake_commit() -> None:
        await db_session.flush()

    db_session.commit = _fake_commit  # type: ignore[method-assign]

    async def override_get_db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    db_session.commit = original_commit  # type: ignore[method-assign]
    app.dependency_overrides.clear()


def _auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _register_and_login(client: AsyncClient) -> str:
    email = f"chat-{uuid4().hex[:8]}@example.com"
    password = "password123"
    await client.post(
        "/auth/register",
        json={"email": email, "password": password, "nickname": "ChatUser"},
    )
    login = await client.post(
        "/auth/login",
        json={"email": email, "password": password},
    )
    assert login.status_code == 200
    return login.json()["access_token"]


@pytest.mark.asyncio
async def test_chat_session_crud_and_messages(user_client: AsyncClient) -> None:
    token = await _register_and_login(user_client)
    headers = _auth_header(token)

    created = await user_client.post(
        "/chat/sessions",
        headers=headers,
        json={"title": "测试会话", "model": "gpt-4o", "temperature": 0.5},
    )
    assert created.status_code == 201
    session = created.json()
    assert session["title"] == "测试会话"
    assert session["messages"] == []
    session_id = session["id"]

    listed = await user_client.get("/chat/sessions", headers=headers)
    assert listed.status_code == 200
    assert len(listed.json()) == 1
    assert listed.json()[0]["message_count"] == 0

    replaced = await user_client.put(
        f"/chat/sessions/{session_id}/messages",
        headers=headers,
        json={
            "title": "你好世界",
            "messages": [
                {"role": "user", "content": "我叫小明"},
                {"role": "assistant", "content": "你好小明"},
                {"role": "user", "content": "我叫什么？"},
            ],
        },
    )
    assert replaced.status_code == 200
    body = replaced.json()
    assert body["title"] == "你好世界"
    assert len(body["messages"]) == 3
    assert body["messages"][0]["content"] == "我叫小明"
    assert body["messages"][2]["seq"] == 2

    detail = await user_client.get(f"/chat/sessions/{session_id}", headers=headers)
    assert detail.status_code == 200
    assert len(detail.json()["messages"]) == 3

    listed2 = await user_client.get("/chat/sessions", headers=headers)
    assert listed2.json()[0]["message_count"] == 3

    deleted = await user_client.delete(
        f"/chat/sessions/{session_id}", headers=headers
    )
    assert deleted.status_code == 204

    gone = await user_client.get(f"/chat/sessions/{session_id}", headers=headers)
    assert gone.status_code == 404


@pytest.mark.asyncio
async def test_chat_session_isolated_by_user(user_client: AsyncClient) -> None:
    token_a = await _register_and_login(user_client)
    token_b = await _register_and_login(user_client)

    created = await user_client.post(
        "/chat/sessions",
        headers=_auth_header(token_a),
        json={"title": "A的会话"},
    )
    session_id = created.json()["id"]

    other = await user_client.get(
        f"/chat/sessions/{session_id}",
        headers=_auth_header(token_b),
    )
    assert other.status_code == 404

    listed_b = await user_client.get(
        "/chat/sessions", headers=_auth_header(token_b)
    )
    assert listed_b.json() == []


@pytest.mark.asyncio
async def test_chat_sessions_require_auth(user_client: AsyncClient) -> None:
    resp = await user_client.get("/chat/sessions")
    assert resp.status_code == 401
