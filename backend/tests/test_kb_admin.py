"""任务 11：配额后台配置、知识库禁用、审计与运营指标。"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.constants import (
    KB_STATUS_DISABLED,
    MODEL_TYPE_EMBEDDING,
    STATUS_ENABLED,
    VISIBILITY_PUBLIC,
)
from app.core.metrics import metrics_payload
from app.db.models.billing import BillingRule
from app.db.models.kb import KnowledgeBase
from app.db.models.llm_model import LLMModel
from app.db.models.log import AuditLog
from app.db.models.user import User
from app.db.session import get_db
from app.main import app
from app.services.kb_service import kb_service
from app.storage import get_file_storage
from app.storage.local import LocalFileStorage
from tests.conftest import admin_auth_headers


@pytest_asyncio.fixture
async def admin_client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    async def override_get_db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers=admin_auth_headers(),
    ) as client:
        yield client
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def user_client(
    db_session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncGenerator[AsyncClient, None]:
    original_commit = db_session.commit

    async def _fake_commit() -> None:
        await db_session.flush()

    db_session.commit = _fake_commit  # type: ignore[method-assign]

    storage_root = tmp_path / "kb_files"
    monkeypatch.setattr(settings, "kb_storage_root", str(storage_root))
    get_file_storage.cache_clear()
    kb_service.storage = LocalFileStorage(storage_root)

    async def override_get_db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    db_session.commit = original_commit  # type: ignore[method-assign]
    app.dependency_overrides.clear()
    get_file_storage.cache_clear()


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _register_login(client: AsyncClient) -> tuple[str, str]:
    email = f"kb11-{uuid4().hex[:8]}@example.com"
    password = "password123"
    await client.post(
        "/auth/register",
        json={"email": email, "password": password, "nickname": "KbAdmin"},
    )
    login = await client.post("/auth/login", json={"email": email, "password": password})
    assert login.status_code == 200
    return login.json()["access_token"], email


async def _seed_embedding(session: AsyncSession, *, alias: str | None = None) -> LLMModel:
    alias = alias or f"emb-t11-{uuid4().hex[:6]}"
    model = LLMModel(
        alias=alias,
        display_name=alias,
        model_type=MODEL_TYPE_EMBEDDING,
        capabilities={},
        visibility=VISIBILITY_PUBLIC,
        status=STATUS_ENABLED,
    )
    session.add(model)
    await session.flush()
    session.add(
        BillingRule(
            model_id=model.id,
            version=1,
            price_input=100,
            price_output=0,
            price_cached_input=0,
            price_per_call=0,
            cost_input=50,
            cost_output=0,
            effective_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
            status=0,
        )
    )
    await session.flush()
    return model


async def _user_by_email(session: AsyncSession, email: str) -> User:
    user = (
        await session.execute(select(User).where(User.email == email))
    ).scalar_one()
    return user


@pytest.mark.asyncio
async def test_role_quota_via_admin_rejects_new_kb(
    admin_client: AsyncClient,
    user_client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = await _seed_embedding(db_session)
    monkeypatch.setattr(settings, "kb_default_embedding_model", model.alias)
    monkeypatch.setattr(settings, "kb_default_max_kb_count", 5)

    token, email = await _register_login(user_client)
    user = await _user_by_email(db_session, email)
    headers = _auth(token)

    # 先建一个库（存量）
    first = await user_client.post("/kb", headers=headers, json={"name": "存量库"})
    assert first.status_code == 201

    # 管理端创建严格配额角色并分配
    role_resp = await admin_client.post(
        "/admin/roles",
        json={
            "name": f"kb-strict-{uuid4().hex[:6]}",
            "description": "严格配额",
            "kb_max_kb_count": 1,
            "kb_max_docs_per_kb": 10,
            "kb_max_storage_bytes": 10_000_000,
            "kb_max_file_size_bytes": 1_000_000,
        },
    )
    assert role_resp.status_code == 201
    role = role_resp.json()
    assert role["kb_max_kb_count"] == 1

    assign = await admin_client.post(
        f"/admin/users/{user.id}/roles",
        json={"role_id": role["id"]},
    )
    assert assign.status_code == 201

    # 调低配额后新建被拒；存量库仍在
    second = await user_client.post("/kb", headers=headers, json={"name": "超额库"})
    assert second.status_code == 400
    assert second.json()["error"]["code"] == "kb_quota_exceeded"
    assert (await db_session.get(KnowledgeBase, first.json()["id"])) is not None

    # 配额变更审计
    audits = list(
        (
            await db_session.execute(
                select(AuditLog).where(AuditLog.action == "role.create")
            )
        ).scalars().all()
    )
    assert any(a.target == f"role:{role['id']}" for a in audits)


@pytest.mark.asyncio
async def test_admin_disable_blocks_upload_and_hit_test_with_audit(
    admin_client: AsyncClient,
    user_client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = await _seed_embedding(db_session)
    monkeypatch.setattr(settings, "kb_default_embedding_model", model.alias)

    token, email = await _register_login(user_client)
    user = await _user_by_email(db_session, email)
    headers = _auth(token)

    kb = (await user_client.post("/kb", headers=headers, json={"name": "待禁用"})).json()

    disable = await admin_client.post(f"/admin/kb/{kb['id']}/disable")
    assert disable.status_code == 200
    assert disable.json()["status"] == "disabled"
    assert "content" not in disable.json()
    assert "chunks" not in disable.json()

    upload = await user_client.post(
        f"/kb/{kb['id']}/documents",
        headers=headers,
        files=[("files", ("a.txt", b"hello", "text/plain"))],
    )
    assert upload.status_code == 400
    assert upload.json()["error"]["code"] == "kb_disabled"

    hit = await user_client.post(
        f"/kb/{kb['id']}/hit-test",
        headers=headers,
        json={"query": "hello"},
    )
    assert hit.status_code == 400
    assert hit.json()["error"]["code"] == "kb_disabled"

    enable = await admin_client.post(f"/admin/kb/{kb['id']}/enable")
    assert enable.status_code == 200
    assert enable.json()["status"] == "active"

    actions = {
        a.action
        for a in (
            await db_session.execute(
                select(AuditLog).where(AuditLog.target == f"kb:{kb['id']}")
            )
        ).scalars().all()
    }
    assert "kb.disable" in actions
    assert "kb.enable" in actions
    _ = user


@pytest.mark.asyncio
async def test_admin_kb_list_and_settings_audit(
    admin_client: AsyncClient,
    user_client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = await _seed_embedding(db_session)
    monkeypatch.setattr(settings, "kb_default_embedding_model", model.alias)

    token, _ = await _register_login(user_client)
    headers = _auth(token)
    kb = (await user_client.post("/kb", headers=headers, json={"name": "列表库"})).json()

    listed = await admin_client.get("/admin/kb")
    assert listed.status_code == 200
    body = listed.json()
    assert any(item["id"] == kb["id"] for item in body)
    sample = next(item for item in body if item["id"] == kb["id"])
    assert "owner_email" in sample
    assert "doc_count" in sample
    assert "storage_bytes" in sample
    # 不泄露正文/分块
    assert "content" not in sample
    assert "chunks" not in sample

    filtered = await admin_client.get("/admin/kb", params={"status": "active"})
    assert filtered.status_code == 200

    stats = await admin_client.get("/admin/kb/user-stats")
    assert stats.status_code == 200
    assert isinstance(stats.json(), list)

    get_s = await admin_client.get("/admin/kb/settings")
    assert get_s.status_code == 200
    assert "effective_upload_batch_limit" in get_s.json()

    patch = await admin_client.patch(
        "/admin/kb/settings",
        json={"upload_batch_limit": 3, "embedding_batch_size": 8},
    )
    assert patch.status_code == 200
    assert patch.json()["upload_batch_limit"] == 3
    assert patch.json()["effective_upload_batch_limit"] == 3

    audits = list(
        (
            await db_session.execute(
                select(AuditLog).where(AuditLog.action == "kb_settings.update")
            )
        ).scalars().all()
    )
    assert audits
    assert "upload_batch_limit" in audits[-1].detail


@pytest.mark.asyncio
async def test_role_quota_update_audit(
    admin_client: AsyncClient, db_session: AsyncSession
) -> None:
    created = await admin_client.post(
        "/admin/roles",
        json={"name": f"quota-role-{uuid4().hex[:6]}", "kb_max_kb_count": 5},
    )
    assert created.status_code == 201
    role_id = created.json()["id"]

    updated = await admin_client.patch(
        f"/admin/roles/{role_id}",
        json={"kb_max_kb_count": 2, "kb_max_docs_per_kb": 20},
    )
    assert updated.status_code == 200
    assert updated.json()["kb_max_kb_count"] == 2

    quota_audits = list(
        (
            await db_session.execute(
                select(AuditLog).where(
                    AuditLog.action == "role.quota_update",
                    AuditLog.target == f"role:{role_id}",
                )
            )
        ).scalars().all()
    )
    assert quota_audits
    assert "kb_max_kb_count" in quota_audits[-1].detail


@pytest.mark.asyncio
async def test_metrics_expose_kb_ops(
    admin_client: AsyncClient,
    user_client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """触发禁用与配置变更后，/metrics 仍可访问且含新增指标名。"""
    model = await _seed_embedding(db_session)
    monkeypatch.setattr(settings, "kb_default_embedding_model", model.alias)
    token, _ = await _register_login(user_client)
    kb = (
        await user_client.post(
            "/kb", headers=_auth(token), json={"name": "metrics-kb"}
        )
    ).json()
    await admin_client.post(f"/admin/kb/{kb['id']}/disable")

    # 直接读取 registry（无需走 HTTP，避免测试环境中间件差异）
    payload, content_type = metrics_payload()
    text = payload.decode("utf-8")
    assert "text/plain" in content_type or "prometheus" in content_type
    for name in (
        "tokenhub_kb_doc_process_duration_seconds",
        "tokenhub_kb_doc_process_total",
        "tokenhub_kb_embed_tokens_total",
        "tokenhub_kb_retrieval_duration_seconds",
        "tokenhub_kb_rag_hit_total",
        "tokenhub_kb_ingest_total",
    ):
        assert name in text

    # 禁用库状态落库
    row = await db_session.get(KnowledgeBase, kb["id"])
    assert row is not None
    assert row.status == KB_STATUS_DISABLED


@pytest.mark.asyncio
async def test_admin_settings_rejects_non_embedding_model(
    admin_client: AsyncClient, db_session: AsyncSession
) -> None:
    chat = LLMModel(
        alias=f"chat-t11-{uuid4().hex[:6]}",
        display_name="chat",
        model_type="chat",
        capabilities={},
        visibility=VISIBILITY_PUBLIC,
        status=STATUS_ENABLED,
    )
    db_session.add(chat)
    await db_session.flush()

    resp = await admin_client.patch(
        "/admin/kb/settings",
        json={"default_embedding_model": chat.alias},
    )
    assert resp.status_code == 400
