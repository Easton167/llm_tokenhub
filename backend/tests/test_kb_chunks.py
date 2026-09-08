"""知识库专项任务 7：分块工作台 API。

验收：
  - 编辑分块后产生一次增量 embedding 扣费，检索按新内容命中
  - 禁用分块立即从命中测试消失，启用后恢复
  - rechunk 未带 confirm=true 时拒绝；执行后旧分块（含人工编辑）全部清理并重新计费
  - 手动新增的分块可被检索命中
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.constants import (
    KB_CHUNK_EMBED_SUCCESS,
    KB_DOC_STATUS_PARSING,
    KB_DOC_STATUS_READY,
    KB_JOB_STATUS_SUCCESS,
    KB_JOB_TYPE_EMBED,
    KB_JOB_TYPE_PARSE,
    KB_JOB_TYPE_RECHUNK,
    MODEL_TYPE_EMBEDDING,
    STATUS_ENABLED,
    USAGE_SOURCE_KB_INGEST,
    VISIBILITY_PUBLIC,
)
from app.core.channel_key_crypto import encrypt_channel_key
from app.db.models.billing import BillingRule
from app.db.models.channel import Channel, ChannelKey, Provider
from app.db.models.kb import KbChunk, KbDocument, KbIngestJob, KnowledgeBase
from app.db.models.llm_model import LLMModel, ModelChannelMap
from app.db.models.log import RequestLog
from app.db.models.user import User
from app.db.models.wallet import Wallet
from app.db.session import get_db
from app.main import app
from app.services.embedding_gateway import reset_embed_semaphore_for_tests
from app.services.kb_ingest_service import kb_ingest_service
from app.services.kb_service import kb_service
from app.services.wallet_service import wallet_service
from app.storage import get_file_storage
from app.storage.local import LocalFileStorage

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "kb"


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
    monkeypatch.setattr(settings, "kb_embedding_batch_size", 4)
    monkeypatch.setattr(settings, "kb_embedding_max_concurrent", 2)
    reset_embed_semaphore_for_tests()
    get_file_storage.cache_clear()
    storage = LocalFileStorage(storage_root)
    kb_service.storage = storage
    kb_ingest_service.storage = storage

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
    email = f"kb7-{uuid4().hex[:8]}@example.com"
    password = "password123"
    await client.post(
        "/auth/register",
        json={"email": email, "password": password, "nickname": "KbChunks"},
    )
    login = await client.post("/auth/login", json={"email": email, "password": password})
    assert login.status_code == 200
    return login.json()["access_token"], email


async def _user_by_email(session: AsyncSession, email: str) -> User:
    return (await session.execute(select(User).where(User.email == email))).scalar_one()


async def _seed_embedding_stack(session: AsyncSession) -> LLMModel:
    model = LLMModel(
        alias=f"emb-kb7-{uuid4().hex[:6]}",
        display_name="emb",
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
            price_input=1_000_000,
            price_output=0,
            price_cached_input=0,
            price_per_call=0,
            cost_input=0,
            cost_output=0,
            min_charge_unit=1,
            multiplier=1,
            effective_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
            status=0,
        )
    )
    await session.flush()

    provider = Provider(
        name=f"prov-{uuid4().hex[:6]}",
        adapter_type="openai_compatible",
        default_base_url="https://api.openai.com/v1",
    )
    session.add(provider)
    await session.flush()
    channel = Channel(
        provider_id=provider.id,
        name=f"chan-{uuid4().hex[:6]}",
        base_url="https://api.openai.com/v1",
        adapter_type="openai_compatible",
        priority=0,
        weight=1,
        status=STATUS_ENABLED,
        health_status=0,
        timeout_ms=30_000,
        max_retries=2,
    )
    session.add(channel)
    await session.flush()
    channel_key = ChannelKey(
        channel_id=channel.id,
        api_key_encrypted=encrypt_channel_key("sk-upstream-kb7"),
        key_masked="****kb7",
        weight=1,
        status=STATUS_ENABLED,
        health_status=0,
    )
    session.add(channel_key)
    await session.flush()
    session.add(
        ModelChannelMap(
            model_id=model.id,
            channel_id=channel.id,
            upstream_model_name="text-embedding-3-small",
            weight=1,
            status=STATUS_ENABLED,
        )
    )
    await session.flush()
    return model


def _fake_embedding(dim: int, seed: int = 0) -> list[float]:
    return [((seed + i) % 97) / 97.0 for i in range(dim)]


def _make_upstream_response(texts: list[str], *, dim: int) -> MagicMock:
    data = []
    for i, t in enumerate(texts):
        data.append(
            {
                "object": "embedding",
                "index": i,
                "embedding": _fake_embedding(dim, seed=hash(t) % 1000),
            }
        )
    prompt_tokens = max(1, sum(len(t) for t in texts) // 4)
    body = {
        "object": "list",
        "data": data,
        "model": "text-embedding-3-small",
        "usage": {"prompt_tokens": prompt_tokens, "total_tokens": prompt_tokens},
    }
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json = MagicMock(return_value=body)
    return mock_resp


def _patch_embedding_httpx(handler):
    async def _post(url, *, json=None, headers=None, timeout=None):
        return handler(json)

    mock_client = MagicMock()
    mock_client.post = AsyncMock(side_effect=_post)
    mock_client.aclose = AsyncMock()
    mock_client_cls = MagicMock()
    mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
    return patch(
        "app.services.embedding_gateway.httpx.AsyncClient", mock_client_cls
    )


def _load(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


async def _fund_wallet(
    session: AsyncSession, user_id, amount: int = 1_000_000
) -> Wallet:
    wallet = await wallet_service.get_or_create_personal_wallet(session, user_id)
    await wallet_service.recharge(
        session, wallet_id=wallet.id, amount=amount, remark="test fund"
    )
    await session.flush()
    return wallet


async def _run_parse_then_embed(session: AsyncSession) -> None:
    job = await kb_ingest_service.process_one(session, max_concurrent=10)
    assert job is not None and job.job_type == KB_JOB_TYPE_PARSE
    job2 = await kb_ingest_service.process_one(session, max_concurrent=10)
    assert job2 is not None and job2.job_type == KB_JOB_TYPE_EMBED


async def _ready_doc(
    client: AsyncClient,
    session: AsyncSession,
    headers: dict[str, str],
    *,
    chunk_size: int = 200,
) -> tuple[str, str]:
    """建库上传 sample.txt 并跑到 ready，返回 (kb_id, doc_id)。"""
    kb_id = (
        await client.post(
            "/kb",
            headers=headers,
            json={
                "name": "分块工作台库",
                "chunk_size": chunk_size,
                "chunk_overlap": 20,
                "top_k": 5,
                "score_threshold": 0.0,
            },
        )
    ).json()["id"]
    uploaded = await client.post(
        f"/kb/{kb_id}/documents",
        headers=headers,
        files=[("files", ("sample.txt", _load("sample.txt"), "text/plain"))],
    )
    assert uploaded.status_code == 201
    doc_id = uploaded.json()[0]["id"]
    dim = settings.kb_embedding_dim

    def handler(payload):
        texts = payload["input"]
        if isinstance(texts, str):
            texts = [texts]
        return _make_upstream_response(texts, dim=dim)

    with _patch_embedding_httpx(handler):
        await _run_parse_then_embed(session)

    detail = await client.get(f"/kb/documents/{doc_id}", headers=headers)
    assert detail.json()["status"] == KB_DOC_STATUS_READY
    return kb_id, doc_id


# ---------------------------------------------------------------------------
# 列表 / 搜索 / 归属
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_chunks_pagination_and_keyword(
    user_client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = await _seed_embedding_stack(db_session)
    monkeypatch.setattr(settings, "kb_default_embedding_model", model.alias)
    token, email = await _register_and_login(user_client)
    user = await _user_by_email(db_session, email)
    await _fund_wallet(db_session, user.id)
    headers = _auth_header(token)

    _kb_id, doc_id = await _ready_doc(user_client, db_session, headers)

    listed = await user_client.get(
        f"/kb/documents/{doc_id}/chunks", headers=headers
    )
    assert listed.status_code == 200
    body = listed.json()
    assert body["total"] >= 1
    assert body["items"]
    assert "欢迎" in body["items"][0]["content"] or any(
        "欢迎" in it["content"] for it in body["items"]
    )
    assert "??" not in body["items"][0]["content"]

    filtered = await user_client.get(
        f"/kb/documents/{doc_id}/chunks",
        headers=headers,
        params={"q": "UTF-8"},
    )
    assert filtered.status_code == 200
    assert filtered.json()["total"] >= 1
    assert all("UTF-8" in it["content"] for it in filtered.json()["items"])

    miss = await user_client.get(
        f"/kb/documents/{doc_id}/chunks",
        headers=headers,
        params={"q": "不存在的关键词XYZ"},
    )
    assert miss.json()["total"] == 0


@pytest.mark.asyncio
async def test_chunk_ownership_forbidden(
    user_client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = await _seed_embedding_stack(db_session)
    monkeypatch.setattr(settings, "kb_default_embedding_model", model.alias)

    token_a, email_a = await _register_and_login(user_client)
    user_a = await _user_by_email(db_session, email_a)
    await _fund_wallet(db_session, user_a.id)
    headers_a = _auth_header(token_a)
    _kb_id, doc_id = await _ready_doc(user_client, db_session, headers_a)

    chunks = (
        await user_client.get(f"/kb/documents/{doc_id}/chunks", headers=headers_a)
    ).json()["items"]
    chunk_id = chunks[0]["id"]

    token_b, _ = await _register_and_login(user_client)
    headers_b = _auth_header(token_b)

    assert (
        await user_client.get(f"/kb/documents/{doc_id}/chunks", headers=headers_b)
    ).status_code == 403
    assert (
        await user_client.patch(
            f"/kb/chunks/{chunk_id}",
            headers=headers_b,
            json={"is_enabled": False},
        )
    ).status_code == 403


# ---------------------------------------------------------------------------
# 编辑重嵌入计费 + 检索命中新内容
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_edit_chunk_reembed_billing_and_hit(
    user_client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = await _seed_embedding_stack(db_session)
    monkeypatch.setattr(settings, "kb_default_embedding_model", model.alias)
    token, email = await _register_and_login(user_client)
    user = await _user_by_email(db_session, email)
    wallet = await _fund_wallet(db_session, user.id)
    headers = _auth_header(token)

    kb_id, doc_id = await _ready_doc(user_client, db_session, headers)
    chunks = (
        await user_client.get(f"/kb/documents/{doc_id}/chunks", headers=headers)
    ).json()["items"]
    chunk_id = chunks[0]["id"]
    new_content = "人工编辑后的独有关键词星云协议，用于命中测试验证。"

    await db_session.refresh(wallet)
    bal_before = wallet.balance
    dim = settings.kb_embedding_dim

    def handler(payload):
        texts = payload["input"]
        if isinstance(texts, str):
            texts = [texts]
        return _make_upstream_response(texts, dim=dim)

    with _patch_embedding_httpx(handler):
        patched = await user_client.patch(
            f"/kb/chunks/{chunk_id}",
            headers=headers,
            json={"content": new_content},
        )
    assert patched.status_code == 200, patched.text
    body = patched.json()
    assert body["content"] == new_content
    assert body["is_manually_edited"] is True
    assert body["embedding_status"] == KB_CHUNK_EMBED_SUCCESS
    assert "??" not in body["content"]

    await db_session.refresh(wallet)
    assert wallet.balance < bal_before

    reembed_reqs = list(
        (
            await db_session.execute(
                select(RequestLog).where(
                    RequestLog.source == USAGE_SOURCE_KB_INGEST,
                    RequestLog.request_id.like("kb-reembed:%"),
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(reembed_reqs) == 1

    with _patch_embedding_httpx(handler):
        hit = await user_client.post(
            f"/kb/{kb_id}/hit-test",
            headers=headers,
            json={"query": "星云协议", "top_k": 5, "score_threshold": 0.0},
        )
    assert hit.status_code == 200
    hit_ids = {h["chunk_id"] for h in hit.json()["hits"]}
    assert chunk_id in hit_ids
    matched = next(h for h in hit.json()["hits"] if h["chunk_id"] == chunk_id)
    assert "星云协议" in matched["content"]


# ---------------------------------------------------------------------------
# 启禁：立即影响命中测试
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disable_enable_affects_hit_test(
    user_client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = await _seed_embedding_stack(db_session)
    monkeypatch.setattr(settings, "kb_default_embedding_model", model.alias)
    token, email = await _register_and_login(user_client)
    user = await _user_by_email(db_session, email)
    await _fund_wallet(db_session, user.id)
    headers = _auth_header(token)

    kb_id, doc_id = await _ready_doc(user_client, db_session, headers)
    chunks = (
        await user_client.get(f"/kb/documents/{doc_id}/chunks", headers=headers)
    ).json()["items"]
    # 禁用全部，再只启用一个，验证启禁开关不触发重嵌入
    target = chunks[0]
    chunk_id = target["id"]
    dim = settings.kb_embedding_dim

    def handler(payload):
        texts = payload["input"]
        if isinstance(texts, str):
            texts = [texts]
        return _make_upstream_response(texts, dim=dim)

    # 禁用不触发 reembed（不应新增 kb-reembed 请求）
    reembed_before = (
        await db_session.execute(
            select(func.count())
            .select_from(RequestLog)
            .where(RequestLog.request_id.like("kb-reembed:%"))
        )
    ).scalar_one()

    disabled = await user_client.patch(
        f"/kb/chunks/{chunk_id}",
        headers=headers,
        json={"is_enabled": False},
    )
    assert disabled.status_code == 200
    assert disabled.json()["is_enabled"] is False
    assert disabled.json()["is_manually_edited"] is False

    reembed_after = (
        await db_session.execute(
            select(func.count())
            .select_from(RequestLog)
            .where(RequestLog.request_id.like("kb-reembed:%"))
        )
    ).scalar_one()
    assert reembed_after == reembed_before

    with _patch_embedding_httpx(handler):
        hit = await user_client.post(
            f"/kb/{kb_id}/hit-test",
            headers=headers,
            json={
                "query": target["content"][:20],
                "top_k": 10,
                "score_threshold": 0.0,
            },
        )
    assert chunk_id not in {h["chunk_id"] for h in hit.json()["hits"]}

    enabled = await user_client.patch(
        f"/kb/chunks/{chunk_id}",
        headers=headers,
        json={"is_enabled": True},
    )
    assert enabled.json()["is_enabled"] is True

    with _patch_embedding_httpx(handler):
        hit2 = await user_client.post(
            f"/kb/{kb_id}/hit-test",
            headers=headers,
            json={
                "query": target["content"][:20],
                "top_k": 10,
                "score_threshold": 0.0,
            },
        )
    assert chunk_id in {h["chunk_id"] for h in hit2.json()["hits"]}


# ---------------------------------------------------------------------------
# 手动新增 + 序号顺延 + 可检索
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manual_create_chunk_shift_seq_and_hit(
    user_client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = await _seed_embedding_stack(db_session)
    monkeypatch.setattr(settings, "kb_default_embedding_model", model.alias)
    token, email = await _register_and_login(user_client)
    user = await _user_by_email(db_session, email)
    await _fund_wallet(db_session, user.id)
    headers = _auth_header(token)

    kb_id, doc_id = await _ready_doc(user_client, db_session, headers)
    before = (
        await user_client.get(f"/kb/documents/{doc_id}/chunks", headers=headers)
    ).json()["items"]
    assert before
    old_first_id = before[0]["id"]
    old_first_seq = before[0]["seq"]

    faq = "常见问题：如何重置密码？请前往账户设置页面。"
    dim = settings.kb_embedding_dim

    def handler(payload):
        texts = payload["input"]
        if isinstance(texts, str):
            texts = [texts]
        return _make_upstream_response(texts, dim=dim)

    with _patch_embedding_httpx(handler):
        created = await user_client.post(
            f"/kb/documents/{doc_id}/chunks",
            headers=headers,
            json={"content": faq, "seq": old_first_seq},
        )
    assert created.status_code == 201, created.text
    new_chunk = created.json()
    assert new_chunk["seq"] == old_first_seq
    assert new_chunk["is_manually_edited"] is True
    assert new_chunk["embedding_status"] == KB_CHUNK_EMBED_SUCCESS
    assert "重置密码" in new_chunk["content"]

    after = (
        await user_client.get(f"/kb/documents/{doc_id}/chunks", headers=headers)
    ).json()["items"]
    shifted = next(c for c in after if c["id"] == old_first_id)
    assert shifted["seq"] == old_first_seq + 1

    kb = (
        await db_session.execute(select(KnowledgeBase).where(KnowledgeBase.id == kb_id))
    ).scalar_one()
    assert kb.chunk_count == len(after)

    with _patch_embedding_httpx(handler):
        hit = await user_client.post(
            f"/kb/{kb_id}/hit-test",
            headers=headers,
            json={"query": "重置密码", "top_k": 5, "score_threshold": 0.0},
        )
    assert new_chunk["id"] in {h["chunk_id"] for h in hit.json()["hits"]}


# ---------------------------------------------------------------------------
# 批量启禁 / 删除
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_batch_disable_and_delete(
    user_client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = await _seed_embedding_stack(db_session)
    monkeypatch.setattr(settings, "kb_default_embedding_model", model.alias)
    token, email = await _register_and_login(user_client)
    user = await _user_by_email(db_session, email)
    await _fund_wallet(db_session, user.id)
    headers = _auth_header(token)

    kb_id, doc_id = await _ready_doc(user_client, db_session, headers)
    items = (
        await user_client.get(f"/kb/documents/{doc_id}/chunks", headers=headers)
    ).json()["items"]
    assert len(items) >= 1
    ids = [items[0]["id"]]

    batch_disable = await user_client.post(
        "/kb/chunks/batch",
        headers=headers,
        json={"chunk_ids": ids, "action": "disable"},
    )
    assert batch_disable.status_code == 200
    assert batch_disable.json()["affected"] == 1

    listed = (
        await user_client.get(f"/kb/documents/{doc_id}/chunks", headers=headers)
    ).json()["items"]
    assert next(c for c in listed if c["id"] == ids[0])["is_enabled"] is False

    kb_before = (
        await db_session.execute(select(KnowledgeBase).where(KnowledgeBase.id == kb_id))
    ).scalar_one()
    count_before = kb_before.chunk_count

    batch_del = await user_client.post(
        "/kb/chunks/batch",
        headers=headers,
        json={"chunk_ids": ids, "action": "delete"},
    )
    assert batch_del.status_code == 200
    assert batch_del.json()["affected"] == 1

    await db_session.refresh(kb_before)
    assert kb_before.chunk_count == count_before - 1
    remaining = (
        await user_client.get(f"/kb/documents/{doc_id}/chunks", headers=headers)
    ).json()
    assert ids[0] not in {c["id"] for c in remaining["items"]}


# ---------------------------------------------------------------------------
# rechunk：confirm 语义 + 覆盖人工编辑 + 重新计费
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rechunk_requires_confirm_and_clears_manual_edits(
    user_client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = await _seed_embedding_stack(db_session)
    monkeypatch.setattr(settings, "kb_default_embedding_model", model.alias)
    token, email = await _register_and_login(user_client)
    user = await _user_by_email(db_session, email)
    wallet = await _fund_wallet(db_session, user.id)
    headers = _auth_header(token)

    kb_id, doc_id = await _ready_doc(
        user_client, db_session, headers, chunk_size=200
    )
    chunks = (
        await user_client.get(f"/kb/documents/{doc_id}/chunks", headers=headers)
    ).json()["items"]
    old_ids = {c["id"] for c in chunks}
    chunk_id = chunks[0]["id"]
    dim = settings.kb_embedding_dim

    def handler(payload):
        texts = payload["input"]
        if isinstance(texts, str):
            texts = [texts]
        return _make_upstream_response(texts, dim=dim)

    with _patch_embedding_httpx(handler):
        edited = await user_client.patch(
            f"/kb/chunks/{chunk_id}",
            headers=headers,
            json={"content": "这段人工编辑内容在 rechunk 后必须消失。"},
        )
    assert edited.json()["is_manually_edited"] is True

    deny = await user_client.post(
        f"/kb/documents/{doc_id}/rechunk",
        headers=headers,
        json={"confirm": False, "chunk_size": 120, "chunk_overlap": 10},
    )
    assert deny.status_code == 400

    await db_session.refresh(wallet)
    bal_before = wallet.balance

    start = await user_client.post(
        f"/kb/documents/{doc_id}/rechunk",
        headers=headers,
        json={"confirm": True, "chunk_size": 120, "chunk_overlap": 10},
    )
    assert start.status_code == 200, start.text
    assert start.json()["status"] == KB_DOC_STATUS_PARSING
    assert start.json()["chunk_size"] == 120

    with _patch_embedding_httpx(handler):
        job1 = await kb_ingest_service.process_one(db_session, max_concurrent=10)
        assert job1 is not None
        assert job1.job_type == KB_JOB_TYPE_RECHUNK
        assert job1.status == KB_JOB_STATUS_SUCCESS
        job2 = await kb_ingest_service.process_one(db_session, max_concurrent=10)
        assert job2 is not None
        assert job2.job_type == KB_JOB_TYPE_EMBED
        assert job2.status == KB_JOB_STATUS_SUCCESS

    detail = await user_client.get(f"/kb/documents/{doc_id}", headers=headers)
    assert detail.json()["status"] == KB_DOC_STATUS_READY
    assert detail.json()["chunk_size"] == 120

    after = (
        await user_client.get(f"/kb/documents/{doc_id}/chunks", headers=headers)
    ).json()
    new_ids = {c["id"] for c in after["items"]}
    assert old_ids.isdisjoint(new_ids)
    assert all(c["is_manually_edited"] is False for c in after["items"])
    assert not any("人工编辑内容" in c["content"] for c in after["items"])

    # 重新向量化应产生扣费
    await db_session.refresh(wallet)
    assert wallet.balance < bal_before

    rechunk_jobs = list(
        (
            await db_session.execute(
                select(KbIngestJob).where(
                    KbIngestJob.document_id == doc_id,
                    KbIngestJob.job_type == KB_JOB_TYPE_RECHUNK,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rechunk_jobs) == 1


@pytest.mark.asyncio
async def test_delete_single_chunk_updates_stats(
    user_client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = await _seed_embedding_stack(db_session)
    monkeypatch.setattr(settings, "kb_default_embedding_model", model.alias)
    token, email = await _register_and_login(user_client)
    user = await _user_by_email(db_session, email)
    await _fund_wallet(db_session, user.id)
    headers = _auth_header(token)

    kb_id, doc_id = await _ready_doc(user_client, db_session, headers)
    items = (
        await user_client.get(f"/kb/documents/{doc_id}/chunks", headers=headers)
    ).json()["items"]
    target = items[0]

    kb = (
        await db_session.execute(select(KnowledgeBase).where(KnowledgeBase.id == kb_id))
    ).scalar_one()
    doc = (
        await db_session.execute(select(KbDocument).where(KbDocument.id == doc_id))
    ).scalar_one()
    count_before = kb.chunk_count
    tokens_before = doc.total_tokens

    resp = await user_client.delete(
        f"/kb/chunks/{target['id']}", headers=headers
    )
    assert resp.status_code == 204

    await db_session.refresh(kb)
    await db_session.refresh(doc)
    assert kb.chunk_count == count_before - 1
    assert doc.total_tokens == tokens_before - target["token_count"]

    gone = await db_session.get(KbChunk, target["id"])
    assert gone is None
