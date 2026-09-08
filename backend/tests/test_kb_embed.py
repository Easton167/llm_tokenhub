"""知识库专项任务 5：向量化与计费串联。

验收：
  - 文档向量化完成后 ready，source=kb_ingest 的 usage + hold/capture 流水
  - 余额不足 → insufficient_balance，无扣费；充值后 retry 可继续
  - 同一文档重复向量化不重复扣费、不重复写向量
  - partial 可只重试失败分块
  - re_embed_chunk 增量计费
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
    KB_CHUNK_EMBED_FAILED,
    KB_CHUNK_EMBED_SUCCESS,
    KB_DOC_STATUS_INSUFFICIENT_BALANCE,
    KB_DOC_STATUS_PARTIAL,
    KB_DOC_STATUS_PENDING_EMBEDDING,
    KB_DOC_STATUS_READY,
    KB_JOB_STATUS_PENDING,
    KB_JOB_STATUS_SUCCESS,
    KB_JOB_TYPE_EMBED,
    KB_JOB_TYPE_PARSE,
    MODEL_TYPE_EMBEDDING,
    STATUS_ENABLED,
    USAGE_SOURCE_KB_INGEST,
    VISIBILITY_PUBLIC,
)
from app.core.channel_key_crypto import encrypt_channel_key
from app.db.models.billing import BillingRule
from app.db.models.channel import Channel, ChannelKey, Provider
from app.db.models.kb import KbChunk, KbDocument, KbIngestJob
from app.db.models.llm_model import LLMModel, ModelChannelMap
from app.db.models.log import RequestLog, UsageLog
from app.db.models.user import User
from app.db.models.wallet import Wallet, WalletTransaction
from app.db.session import get_db
from app.main import app
from app.services.billing_service import billing_service
from app.services.embedding_gateway import reset_embed_semaphore_for_tests
from app.services.kb_ingest_service import kb_ingest_service
from app.services.kb_service import kb_service
from app.services.wallet_service import TX_CAPTURE, TX_HOLD, TX_RELEASE, wallet_service
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
    monkeypatch.setattr(settings, "kb_embedding_batch_size", 2)
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


async def _register_and_login(client: AsyncClient) -> tuple[str, User]:
    email = f"kb5-{uuid4().hex[:8]}@example.com"
    password = "password123"
    await client.post(
        "/auth/register",
        json={"email": email, "password": password, "nickname": "KbEmbed"},
    )
    login = await client.post("/auth/login", json={"email": email, "password": password})
    assert login.status_code == 200
    # 从 DB 取 user 需由调用方查；此处返回 token + email，再查
    return login.json()["access_token"], email  # type: ignore[return-value]


async def _user_by_email(session: AsyncSession, email: str) -> User:
    return (
        await session.execute(select(User).where(User.email == email))
    ).scalar_one()


async def _seed_embedding_stack(
    session: AsyncSession, *, dim: int | None = None
) -> LLMModel:
    dim = dim or settings.kb_embedding_dim
    model = LLMModel(
        alias=f"emb-kb5-{uuid4().hex[:6]}",
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
            price_input=1_000_000,  # 1 token = 1 最小单位，便于对账
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
        api_key_encrypted=encrypt_channel_key("sk-upstream-kb"),
        key_masked="****kb",
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
    # 用量按字符估算，与 gateway 一致口径便于对账
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
    """handler(json_body) -> MagicMock response。"""

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


async def _fund_wallet(session: AsyncSession, user_id, amount: int = 1_000_000) -> Wallet:
    wallet = await wallet_service.get_or_create_personal_wallet(session, user_id)
    await wallet_service.recharge(
        session, wallet_id=wallet.id, amount=amount, remark="test fund"
    )
    await session.flush()
    return wallet


async def _run_parse_then_embed(session: AsyncSession) -> None:
    """处理一个 parse + 随后一个 embed（若存在）。"""
    job = await kb_ingest_service.process_one(session, max_concurrent=10)
    assert job is not None and job.job_type == KB_JOB_TYPE_PARSE
    job2 = await kb_ingest_service.process_one(session, max_concurrent=10)
    assert job2 is not None and job2.job_type == KB_JOB_TYPE_EMBED


# ---------------------------------------------------------------------------
# 成功路径
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_embed_success_ready_usage_and_wallet(
    user_client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = await _seed_embedding_stack(db_session)
    monkeypatch.setattr(settings, "kb_default_embedding_model", model.alias)
    token, email = await _register_and_login(user_client)
    user = await _user_by_email(db_session, email)
    wallet = await _fund_wallet(db_session, user.id)
    headers = _auth_header(token)

    kb_id = (
        await user_client.post(
            "/kb",
            headers=headers,
            json={"name": "向量化库", "chunk_size": 200, "chunk_overlap": 20},
        )
    ).json()["id"]
    uploaded = await user_client.post(
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
        await _run_parse_then_embed(db_session)

    detail = await user_client.get(f"/kb/documents/{doc_id}", headers=headers)
    assert detail.json()["status"] == KB_DOC_STATUS_READY

    chunks = list(
        (
            await db_session.execute(
                select(KbChunk)
                .where(KbChunk.document_id == doc_id)
                .order_by(KbChunk.seq)
            )
        )
        .scalars()
        .all()
    )
    assert chunks
    assert all(c.embedding_status == KB_CHUNK_EMBED_SUCCESS for c in chunks)
    assert all(c.embedding is not None and len(c.embedding) == dim for c in chunks)

    usage_rows = list(
        (
            await db_session.execute(
                select(UsageLog).where(UsageLog.source == USAGE_SOURCE_KB_INGEST)
            )
        )
        .scalars()
        .all()
    )
    assert usage_rows
    assert all(u.source == USAGE_SOURCE_KB_INGEST for u in usage_rows)

    req_rows = list(
        (
            await db_session.execute(
                select(RequestLog).where(RequestLog.source == USAGE_SOURCE_KB_INGEST)
            )
        )
        .scalars()
        .all()
    )
    assert req_rows
    assert all(r.api_key_id is None for r in req_rows)

    hold_txs = list(
        (
            await db_session.execute(
                select(WalletTransaction).where(
                    WalletTransaction.wallet_id == wallet.id,
                    WalletTransaction.type == TX_HOLD,
                )
            )
        )
        .scalars()
        .all()
    )
    capture_txs = list(
        (
            await db_session.execute(
                select(WalletTransaction).where(
                    WalletTransaction.wallet_id == wallet.id,
                    WalletTransaction.type == TX_CAPTURE,
                )
            )
        )
        .scalars()
        .all()
    )
    assert hold_txs
    assert capture_txs

    # 文档级 capture 金额应等于成功 token × 单价（本测试 1 token = 1）
    doc = (
        await db_session.execute(select(KbDocument).where(KbDocument.id == doc_id))
    ).scalar_one()
    embed_job = (
        await db_session.execute(
            select(KbIngestJob)
            .where(
                KbIngestJob.document_id == doc.id,
                KbIngestJob.job_type == KB_JOB_TYPE_EMBED,
            )
            .order_by(KbIngestJob.created_at.desc())
            .limit(1)
        )
    ).scalar_one()
    doc_capture = (
        await db_session.execute(
            select(WalletTransaction).where(
                WalletTransaction.wallet_id == wallet.id,
                WalletTransaction.type == TX_CAPTURE,
                WalletTransaction.ref_request_id == embed_job.hold_ref_request_id,
            )
        )
    ).scalar_one()
    # 只汇总当前 embed job 的 batch request_id 对应的 usage（避免跨测试 DB 数据污染）
    embed_req_prefix = embed_job.hold_ref_request_id + ":"
    doc_usage_rows = list(
        (
            await db_session.execute(
                select(UsageLog).where(
                    UsageLog.source == USAGE_SOURCE_KB_INGEST,
                    UsageLog.request_id.like(embed_req_prefix + "%"),
                )
            )
        )
        .scalars()
        .all()
    )
    total_prompt = sum(u.input_tokens for u in doc_usage_rows)
    rule = await billing_service.require_active_rule(db_session, model_id=model.id)
    expected = billing_service.calculate_charge_amount(rule, total_prompt, 0, 0)
    assert abs(doc_capture.amount) == expected


@pytest.mark.asyncio
async def test_insufficient_balance_no_charge_then_retry(
    user_client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = await _seed_embedding_stack(db_session)
    monkeypatch.setattr(settings, "kb_default_embedding_model", model.alias)
    token, email = await _register_and_login(user_client)
    user = await _user_by_email(db_session, email)
    # 不充值：余额 0
    wallet = await wallet_service.get_or_create_personal_wallet(db_session, user.id)
    headers = _auth_header(token)

    kb_id = (
        await user_client.post("/kb", headers=headers, json={"name": "余额不足库"})
    ).json()["id"]
    uploaded = await user_client.post(
        f"/kb/{kb_id}/documents",
        headers=headers,
        files=[("files", ("sample.txt", _load("sample.txt"), "text/plain"))],
    )
    doc_id = uploaded.json()[0]["id"]

    # parse
    await kb_ingest_service.process_one(db_session, max_concurrent=10)
    # embed → insufficient
    embed_job = await kb_ingest_service.process_one(db_session, max_concurrent=10)
    assert embed_job is not None
    assert embed_job.job_type == KB_JOB_TYPE_EMBED

    detail = await user_client.get(f"/kb/documents/{doc_id}", headers=headers)
    assert detail.json()["status"] == KB_DOC_STATUS_INSUFFICIENT_BALANCE

    txs = list(
        (
            await db_session.execute(
                select(WalletTransaction).where(
                    WalletTransaction.wallet_id == wallet.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert txs == []

    # 充值后 retry
    await _fund_wallet(db_session, user.id, amount=1_000_000)
    dim = settings.kb_embedding_dim

    def handler(payload):
        texts = payload["input"]
        if isinstance(texts, str):
            texts = [texts]
        return _make_upstream_response(texts, dim=dim)

    retried = await user_client.post(f"/kb/documents/{doc_id}/retry", headers=headers)
    assert retried.status_code == 200
    assert retried.json()["status"] == KB_DOC_STATUS_PENDING_EMBEDDING

    with _patch_embedding_httpx(handler):
        job = await kb_ingest_service.process_one(db_session, max_concurrent=10)
        assert job is not None and job.job_type == KB_JOB_TYPE_EMBED
        assert job.status == KB_JOB_STATUS_SUCCESS

    final = await user_client.get(f"/kb/documents/{doc_id}", headers=headers)
    assert final.json()["status"] == KB_DOC_STATUS_READY


@pytest.mark.asyncio
async def test_partial_retry_only_failed_chunks(
    user_client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = await _seed_embedding_stack(db_session)
    monkeypatch.setattr(settings, "kb_default_embedding_model", model.alias)
    monkeypatch.setattr(settings, "kb_embedding_batch_size", 1)
    reset_embed_semaphore_for_tests()
    token, email = await _register_and_login(user_client)
    user = await _user_by_email(db_session, email)
    await _fund_wallet(db_session, user.id)
    headers = _auth_header(token)

    kb_id = (
        await user_client.post(
            "/kb",
            headers=headers,
            json={"name": "部分失败库", "chunk_size": 100, "chunk_overlap": 0},
        )
    ).json()["id"]
    # 足够长以产生多分块
    content = ("中文分块内容段落。" * 300).encode("utf-8")
    uploaded = await user_client.post(
        f"/kb/{kb_id}/documents",
        headers=headers,
        files=[("files", ("long.txt", content, "text/plain"))],
    )
    doc_id = uploaded.json()[0]["id"]

    await kb_ingest_service.process_one(db_session, max_concurrent=10)
    chunks_before = list(
        (
            await db_session.execute(
                select(KbChunk)
                .where(KbChunk.document_id == doc_id)
                .order_by(KbChunk.seq)
            )
        )
        .scalars()
        .all()
    )
    assert len(chunks_before) >= 2

    dim = settings.kb_embedding_dim
    call_count = {"n": 0}

    def handler(payload):
        call_count["n"] += 1
        texts = payload["input"]
        if isinstance(texts, str):
            texts = [texts]
        # 第一批失败（400 不可重试），其余成功
        if call_count["n"] == 1:
            mock_resp = MagicMock()
            mock_resp.status_code = 400
            mock_resp.json = MagicMock(return_value={"error": {"message": "boom"}})
            return mock_resp
        return _make_upstream_response(texts, dim=dim)

    with _patch_embedding_httpx(handler):
        embed_job = await kb_ingest_service.process_one(db_session, max_concurrent=10)
        assert embed_job is not None

    detail = await user_client.get(f"/kb/documents/{doc_id}", headers=headers)
    assert detail.json()["status"] == KB_DOC_STATUS_PARTIAL

    chunks = list(
        (
            await db_session.execute(
                select(KbChunk)
                .where(KbChunk.document_id == doc_id)
                .order_by(KbChunk.seq)
            )
        )
        .scalars()
        .all()
    )
    success_ids = {c.id for c in chunks if c.embedding_status == KB_CHUNK_EMBED_SUCCESS}
    failed_ids = {c.id for c in chunks if c.embedding_status == KB_CHUNK_EMBED_FAILED}
    assert success_ids
    assert failed_ids

    capture_before = (
        await db_session.execute(
            select(func.count())
            .select_from(WalletTransaction)
            .where(WalletTransaction.type == TX_CAPTURE)
        )
    ).scalar_one()

    # 成功向量快照
    success_vecs = {
        c.id: list(c.embedding)
        for c in chunks
        if c.embedding_status == KB_CHUNK_EMBED_SUCCESS
    }

    call_count["n"] = 0

    def handler_ok(payload):
        texts = payload["input"]
        if isinstance(texts, str):
            texts = [texts]
        return _make_upstream_response(texts, dim=dim)

    retried = await user_client.post(f"/kb/documents/{doc_id}/retry", headers=headers)
    assert retried.status_code == 200

    with _patch_embedding_httpx(handler_ok):
        job2 = await kb_ingest_service.process_one(db_session, max_concurrent=10)
        assert job2 is not None and job2.job_type == KB_JOB_TYPE_EMBED

    final = await user_client.get(f"/kb/documents/{doc_id}", headers=headers)
    assert final.json()["status"] == KB_DOC_STATUS_READY

    chunks2 = list(
        (
            await db_session.execute(
                select(KbChunk).where(KbChunk.document_id == doc_id)
            )
        )
        .scalars()
        .all()
    )
    assert all(c.embedding_status == KB_CHUNK_EMBED_SUCCESS for c in chunks2)
    # 原成功分块向量不变
    for c in chunks2:
        if c.id in success_vecs:
            assert list(c.embedding) == success_vecs[c.id]

    capture_after = (
        await db_session.execute(
            select(func.count())
            .select_from(WalletTransaction)
            .where(WalletTransaction.type == TX_CAPTURE)
        )
    ).scalar_one()
    # 重试失败分块会产生新的文档级 capture
    assert capture_after > capture_before


@pytest.mark.asyncio
async def test_repeat_embed_idempotent_no_double_charge(
    user_client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = await _seed_embedding_stack(db_session)
    monkeypatch.setattr(settings, "kb_default_embedding_model", model.alias)
    token, email = await _register_and_login(user_client)
    user = await _user_by_email(db_session, email)
    wallet = await _fund_wallet(db_session, user.id)
    headers = _auth_header(token)

    kb_id = (
        await user_client.post("/kb", headers=headers, json={"name": "幂等向量库"})
    ).json()["id"]
    uploaded = await user_client.post(
        f"/kb/{kb_id}/documents",
        headers=headers,
        files=[("files", ("sample.txt", _load("sample.txt"), "text/plain"))],
    )
    doc_id = uploaded.json()[0]["id"]
    dim = settings.kb_embedding_dim

    def handler(payload):
        texts = payload["input"]
        if isinstance(texts, str):
            texts = [texts]
        return _make_upstream_response(texts, dim=dim)

    with _patch_embedding_httpx(handler):
        await _run_parse_then_embed(db_session)

    await db_session.refresh(wallet)
    balance_after_first = wallet.balance
    capture_count_1 = (
        await db_session.execute(
            select(func.count())
            .select_from(WalletTransaction)
            .where(
                WalletTransaction.wallet_id == wallet.id,
                WalletTransaction.type == TX_CAPTURE,
            )
        )
    ).scalar_one()

    # 再创建一个 embed job（全部分块已 success）→ 短路无扣费
    doc = (
        await db_session.execute(select(KbDocument).where(KbDocument.id == doc_id))
    ).scalar_one()
    assert doc.status == KB_DOC_STATUS_READY
    # 人为回到 pending_embedding 以允许再跑（模拟重复触发）
    doc.status = KB_DOC_STATUS_PENDING_EMBEDDING
    await db_session.flush()
    job = await kb_ingest_service.create_embed_job(db_session, doc=doc)
    job.status = "running"
    await db_session.flush()

    with _patch_embedding_httpx(handler):
        await kb_ingest_service.run_embed_job(db_session, job=job)

    assert job.status == KB_JOB_STATUS_SUCCESS
    assert doc.status == KB_DOC_STATUS_READY
    await db_session.refresh(wallet)
    assert wallet.balance == balance_after_first
    capture_count_2 = (
        await db_session.execute(
            select(func.count())
            .select_from(WalletTransaction)
            .where(
                WalletTransaction.wallet_id == wallet.id,
                WalletTransaction.type == TX_CAPTURE,
            )
        )
    ).scalar_one()
    assert capture_count_2 == capture_count_1


@pytest.mark.asyncio
async def test_re_embed_chunk_incremental_billing(
    user_client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = await _seed_embedding_stack(db_session)
    monkeypatch.setattr(settings, "kb_default_embedding_model", model.alias)
    token, email = await _register_and_login(user_client)
    user = await _user_by_email(db_session, email)
    wallet = await _fund_wallet(db_session, user.id)
    headers = _auth_header(token)

    kb_id = (
        await user_client.post("/kb", headers=headers, json={"name": "重嵌入库"})
    ).json()["id"]
    uploaded = await user_client.post(
        f"/kb/{kb_id}/documents",
        headers=headers,
        files=[("files", ("sample.txt", _load("sample.txt"), "text/plain"))],
    )
    doc_id = uploaded.json()[0]["id"]
    dim = settings.kb_embedding_dim

    def handler(payload):
        texts = payload["input"]
        if isinstance(texts, str):
            texts = [texts]
        return _make_upstream_response(texts, dim=dim)

    with _patch_embedding_httpx(handler):
        await _run_parse_then_embed(db_session)

    chunk_row = (
        await db_session.execute(
            select(KbChunk)
            .where(KbChunk.document_id == doc_id)
            .order_by(KbChunk.seq)
            .limit(1)
        )
    ).scalar_one()
    old_vec = list(chunk_row.embedding)
    chunk_row.content = "人工编辑后的新内容，用于增量重嵌入测试。"
    await db_session.flush()

    await db_session.refresh(wallet)
    bal_before = wallet.balance

    with _patch_embedding_httpx(handler):
        updated = await kb_ingest_service.re_embed_chunk(
            db_session, chunk_id=chunk_row.id
        )

    assert updated.embedding_status == KB_CHUNK_EMBED_SUCCESS
    assert list(updated.embedding) != old_vec
    await db_session.refresh(wallet)
    assert wallet.balance < bal_before

    # re_embed 的 usage 也是 kb_ingest，且 request 无 api_key
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
    assert reembed_reqs[0].api_key_id is None


@pytest.mark.asyncio
async def test_all_embed_fail_releases_hold(
    user_client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = await _seed_embedding_stack(db_session)
    monkeypatch.setattr(settings, "kb_default_embedding_model", model.alias)
    token, email = await _register_and_login(user_client)
    user = await _user_by_email(db_session, email)
    wallet = await _fund_wallet(db_session, user.id, amount=100_000)
    headers = _auth_header(token)

    kb_id = (
        await user_client.post("/kb", headers=headers, json={"name": "全失败库"})
    ).json()["id"]
    uploaded = await user_client.post(
        f"/kb/{kb_id}/documents",
        headers=headers,
        files=[("files", ("sample.txt", _load("sample.txt"), "text/plain"))],
    )
    doc_id = uploaded.json()[0]["id"]

    def handler(_payload):
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.json = MagicMock(return_value={"error": {"message": "down"}})
        return mock_resp

    await db_session.refresh(wallet)
    bal0 = wallet.balance

    with _patch_embedding_httpx(handler):
        await _run_parse_then_embed(db_session)

    detail = await user_client.get(f"/kb/documents/{doc_id}", headers=headers)
    assert detail.json()["status"] == "failed"

    await db_session.refresh(wallet)
    assert wallet.balance == bal0
    assert wallet.held == 0

    releases = list(
        (
            await db_session.execute(
                select(WalletTransaction).where(
                    WalletTransaction.wallet_id == wallet.id,
                    WalletTransaction.type == TX_RELEASE,
                )
            )
        )
        .scalars()
        .all()
    )
    assert releases
