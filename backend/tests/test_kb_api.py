"""知识库专项任务 2：存储抽象、知识库/文档 API、配额与类型校验。

验收：
  - 用户 A 访问用户 B 的知识库/文档一律 403
  - 伪造扩展名（如 exe 改名 pdf）被拒绝
  - 超配额（库数/文档数/存储/单文件）明确错误码，且不写库不存文件
  - 删除知识库后原文件与记录全部清理
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.constants import (
    MODEL_TYPE_EMBEDDING,
    STATUS_ENABLED,
    VISIBILITY_PUBLIC,
)
from app.db.models.billing import BillingRule
from app.db.models.kb import KbDocument, KnowledgeBase
from app.db.models.llm_model import LLMModel
from app.db.models.user import Role, UserRole
from app.db.session import get_db
from app.main import app
from app.services.kb_file_validation import detect_and_validate_file_type
from app.services.kb_service import kb_service
from app.storage import get_file_storage
from app.storage.local import LocalFileStorage


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


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


def _auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _register_and_login(client: AsyncClient) -> tuple[str, str]:
    email = f"kb2-{uuid4().hex[:8]}@example.com"
    password = "password123"
    await client.post(
        "/auth/register",
        json={"email": email, "password": password, "nickname": "KbUser"},
    )
    login = await client.post("/auth/login", json={"email": email, "password": password})
    assert login.status_code == 200
    return login.json()["access_token"], email


async def _seed_embedding_with_billing(
    session: AsyncSession, *, alias: str | None = None
) -> LLMModel:
    alias = alias or f"emb-kb-{uuid4().hex[:6]}"
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


def _pdf_bytes(payload: bytes = b"hello") -> bytes:
    return b"%PDF-1.4\n" + payload + b"\n%%EOF\n"


def _exe_bytes() -> bytes:
    return b"MZ" + b"\x00" * 64 + b"This program cannot be run in DOS mode"


def _docx_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/'
            'package/2006/content-types"></Types>',
        )
        zf.writestr("word/document.xml", "<w:document>测试</w:document>")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# unit: file sniff
# ---------------------------------------------------------------------------


def test_sniff_rejects_exe_renamed_as_pdf() -> None:
    with pytest.raises(Exception) as exc:
        detect_and_validate_file_type("malware.pdf", _exe_bytes())
    assert getattr(exc.value, "code", None) == "unsupported_file_type"


def test_sniff_accepts_real_pdf_and_txt_chinese() -> None:
    assert detect_and_validate_file_type("手册.pdf", _pdf_bytes()) == "pdf"
    assert (
        detect_and_validate_file_type("说明.txt", "中文产品说明".encode("utf-8")) == "txt"
    )
    assert detect_and_validate_file_type("报告.docx", _docx_bytes()) == "docx"


def test_local_storage_roundtrip(tmp_path: Path) -> None:
    storage = LocalFileStorage(tmp_path / "root")
    key = storage.save("中文内容".encode("utf-8"))
    assert ".." not in key
    assert storage.exists(key)
    with storage.open(key) as fh:
        assert fh.read().decode("utf-8") == "中文内容"
    storage.delete(key)
    assert not storage.exists(key)


# ---------------------------------------------------------------------------
# API integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kb_crud_and_isolation(
    user_client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = await _seed_embedding_with_billing(db_session)
    monkeypatch.setattr(settings, "kb_default_embedding_model", model.alias)

    token_a, _ = await _register_and_login(user_client)
    token_b, _ = await _register_and_login(user_client)
    ha, hb = _auth_header(token_a), _auth_header(token_b)

    created = await user_client.post(
        "/kb",
        headers=ha,
        json={"name": "产品手册库", "description": "内部制度"},
    )
    assert created.status_code == 201, created.text
    kb = created.json()
    assert kb["name"] == "产品手册库"
    assert kb["embedding_model"] == model.alias
    assert kb["doc_count"] == 0
    kb_id = kb["id"]

    listed = await user_client.get("/kb", headers=ha)
    assert listed.status_code == 200
    assert len(listed.json()) == 1

    # B 不可见 A 的库
    listed_b = await user_client.get("/kb", headers=hb)
    assert listed_b.json() == []

    forbidden = await user_client.get(f"/kb/{kb_id}", headers=hb)
    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "forbidden"

    patched = await user_client.patch(
        f"/kb/{kb_id}",
        headers=ha,
        json={"name": "产品手册库-修订", "top_k": 8},
    )
    assert patched.status_code == 200
    assert patched.json()["name"] == "产品手册库-修订"
    assert patched.json()["top_k"] == 8

    # embedding 不可通过 update 更换（字段不在 schema）
    from app.schemas.kb import KnowledgeBaseUpdate

    assert "embedding_model" not in KnowledgeBaseUpdate.model_fields
    assert "embedding_model_id" not in KnowledgeBaseUpdate.model_fields


@pytest.mark.asyncio
async def test_upload_list_delete_document_and_cascade_files(
    user_client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = await _seed_embedding_with_billing(db_session)
    monkeypatch.setattr(settings, "kb_default_embedding_model", model.alias)
    token, _ = await _register_and_login(user_client)
    headers = _auth_header(token)

    kb = (
        await user_client.post(
            "/kb", headers=headers, json={"name": "上传测试库"}
        )
    ).json()
    kb_id = kb["id"]

    files = [
        ("files", ("手册.txt", "中文正文内容".encode("utf-8"), "text/plain")),
        ("files", ("规格.pdf", _pdf_bytes(b"spec"), "application/pdf")),
    ]
    uploaded = await user_client.post(
        f"/kb/{kb_id}/documents", headers=headers, files=files
    )
    assert uploaded.status_code == 201, uploaded.text
    docs = uploaded.json()
    assert len(docs) == 2
    assert {d["file_type"] for d in docs} == {"txt", "pdf"}
    assert all(d["status"] == "uploaded" for d in docs)

    detail_kb = await user_client.get(f"/kb/{kb_id}", headers=headers)
    assert detail_kb.json()["doc_count"] == 2
    assert detail_kb.json()["storage_bytes"] > 0

    listed = await user_client.get(f"/kb/{kb_id}/documents", headers=headers)
    assert listed.status_code == 200
    assert listed.json()["total"] == 2

    doc_id = docs[0]["id"]
    one = await user_client.get(f"/kb/documents/{doc_id}", headers=headers)
    assert one.status_code == 200
    assert one.json()["file_name"] == docs[0]["file_name"]

    # 存储文件确实存在
    path_by_id = {
        str(row.id): row.storage_path
        for row in (
            await db_session.execute(select(KbDocument).where(KbDocument.kb_id == kb_id))
        )
        .scalars()
        .all()
    }
    assert len(path_by_id) == 2
    for path in path_by_id.values():
        assert kb_service.storage.exists(path)

    deleted_path = path_by_id[doc_id]
    deleted_doc = await user_client.delete(
        f"/kb/documents/{doc_id}", headers=headers
    )
    assert deleted_doc.status_code == 204
    assert not kb_service.storage.exists(deleted_path)

    # 删除整个知识库：剩余文件 + 记录清理
    remaining_paths = [
        p for did, p in path_by_id.items() if did != doc_id
    ]
    deleted_kb = await user_client.delete(f"/kb/{kb_id}", headers=headers)
    assert deleted_kb.status_code == 204
    for path in remaining_paths:
        assert not kb_service.storage.exists(path)
    left = (
        await db_session.execute(
            select(func.count())
            .select_from(KnowledgeBase)
            .where(KnowledgeBase.id == kb_id)
        )
    ).scalar_one()
    assert left == 0
    left_docs = (
        await db_session.execute(
            select(func.count())
            .select_from(KbDocument)
            .where(KbDocument.kb_id == kb_id)
        )
    ).scalar_one()
    assert left_docs == 0


@pytest.mark.asyncio
async def test_cross_user_document_forbidden(
    user_client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = await _seed_embedding_with_billing(db_session)
    monkeypatch.setattr(settings, "kb_default_embedding_model", model.alias)
    token_a, _ = await _register_and_login(user_client)
    token_b, _ = await _register_and_login(user_client)

    kb = (
        await user_client.post(
            "/kb", headers=_auth_header(token_a), json={"name": "A的库"}
        )
    ).json()
    uploaded = await user_client.post(
        f"/kb/{kb['id']}/documents",
        headers=_auth_header(token_a),
        files=[("files", ("a.txt", b"hello", "text/plain"))],
    )
    doc_id = uploaded.json()[0]["id"]

    assert (
        await user_client.get(
            f"/kb/documents/{doc_id}", headers=_auth_header(token_b)
        )
    ).status_code == 403
    assert (
        await user_client.delete(
            f"/kb/documents/{doc_id}", headers=_auth_header(token_b)
        )
    ).status_code == 403
    assert (
        await user_client.post(
            f"/kb/{kb['id']}/documents",
            headers=_auth_header(token_b),
            files=[("files", ("x.txt", b"x", "text/plain"))],
        )
    ).status_code == 403


@pytest.mark.asyncio
async def test_reject_spoofed_extension_no_side_effects(
    user_client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    model = await _seed_embedding_with_billing(db_session)
    monkeypatch.setattr(settings, "kb_default_embedding_model", model.alias)
    token, _ = await _register_and_login(user_client)
    headers = _auth_header(token)
    kb = (
        await user_client.post("/kb", headers=headers, json={"name": "校验库"})
    ).json()

    before_files = list(Path(settings.kb_storage_root).rglob("*")) if Path(settings.kb_storage_root).exists() else []
    resp = await user_client.post(
        f"/kb/{kb['id']}/documents",
        headers=headers,
        files=[("files", ("fake.pdf", _exe_bytes(), "application/pdf"))],
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "unsupported_file_type"

    docs = (
        await db_session.execute(
            select(func.count())
            .select_from(KbDocument)
            .where(KbDocument.kb_id == kb["id"])
        )
    ).scalar_one()
    assert docs == 0
    after_files = [
        p for p in Path(settings.kb_storage_root).rglob("*") if p.is_file()
    ] if Path(settings.kb_storage_root).exists() else []
    assert len(after_files) == len([p for p in before_files if Path(p).is_file()])


@pytest.mark.asyncio
async def test_quota_kb_count_exceeded(
    user_client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = await _seed_embedding_with_billing(db_session)
    monkeypatch.setattr(settings, "kb_default_embedding_model", model.alias)
    monkeypatch.setattr(settings, "kb_default_max_kb_count", 1)

    token, email = await _register_and_login(user_client)
    headers = _auth_header(token)

    first = await user_client.post("/kb", headers=headers, json={"name": "库1"})
    assert first.status_code == 201
    second = await user_client.post("/kb", headers=headers, json={"name": "库2"})
    assert second.status_code == 400
    assert second.json()["error"]["code"] == "kb_quota_exceeded"
    _ = email


@pytest.mark.asyncio
async def test_quota_file_size_and_docs_no_write(
    user_client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = await _seed_embedding_with_billing(db_session)
    monkeypatch.setattr(settings, "kb_default_embedding_model", model.alias)
    monkeypatch.setattr(settings, "kb_default_max_file_size_bytes", 10)
    monkeypatch.setattr(settings, "kb_default_max_docs_per_kb", 1)

    token, _ = await _register_and_login(user_client)
    headers = _auth_header(token)
    kb_id = (
        await user_client.post("/kb", headers=headers, json={"name": "配额库"})
    ).json()["id"]

    too_big = await user_client.post(
        f"/kb/{kb_id}/documents",
        headers=headers,
        files=[("files", ("big.txt", b"0123456789ABCDEF", "text/plain"))],
    )
    assert too_big.status_code == 400
    assert too_big.json()["error"]["code"] == "file_too_large"
    assert (
        await db_session.execute(
            select(func.count())
            .select_from(KbDocument)
            .where(KbDocument.kb_id == kb_id)
        )
    ).scalar_one() == 0
    assert not any(
        p.is_file() for p in Path(settings.kb_storage_root).rglob("*")
    ) if Path(settings.kb_storage_root).exists() else True

    ok = await user_client.post(
        f"/kb/{kb_id}/documents",
        headers=headers,
        files=[("files", ("ok.txt", b"short", "text/plain"))],
    )
    assert ok.status_code == 201

    over_docs = await user_client.post(
        f"/kb/{kb_id}/documents",
        headers=headers,
        files=[("files", ("two.txt", b"x", "text/plain"))],
    )
    assert over_docs.status_code == 400
    assert over_docs.json()["error"]["code"] == "kb_doc_quota_exceeded"
    assert (
        await db_session.execute(
            select(func.count())
            .select_from(KbDocument)
            .where(KbDocument.kb_id == kb_id)
        )
    ).scalar_one() == 1


@pytest.mark.asyncio
async def test_quota_storage_exceeded_via_role(
    user_client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = await _seed_embedding_with_billing(db_session)
    monkeypatch.setattr(settings, "kb_default_embedding_model", model.alias)

    token, email = await _register_and_login(user_client)
    headers = _auth_header(token)

    # 给用户挂一个极小存储配额的角色
    from app.db.models.user import User

    user = (
        await db_session.execute(select(User).where(User.email == email))
    ).scalar_one()
    role = Role(
        name=f"kb-tiny-{uuid4().hex[:6]}",
        kb_max_storage_bytes=20,
        kb_max_file_size_bytes=100,
        kb_max_docs_per_kb=10,
        kb_max_kb_count=5,
    )
    db_session.add(role)
    await db_session.flush()
    db_session.add(UserRole(user_id=user.id, role_id=role.id))
    await db_session.flush()

    kb_id = (
        await user_client.post("/kb", headers=headers, json={"name": "存储配额"})
    ).json()["id"]

    resp = await user_client.post(
        f"/kb/{kb_id}/documents",
        headers=headers,
        files=[("files", ("a.txt", b"x" * 30, "text/plain"))],
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "kb_storage_quota_exceeded"
    assert (
        await db_session.execute(
            select(func.count())
            .select_from(KbDocument)
            .where(KbDocument.kb_id == kb_id)
        )
    ).scalar_one() == 0
