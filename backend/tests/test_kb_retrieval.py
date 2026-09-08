"""知识库专项任务 6：检索服务与命中测试。

验收：
  - 构造向量夹具下，返回顺序与 cosine 分数严格一致，低于阈值的不返回
  - 禁用的分块、failed 文档的分块不出现在结果中
  - 命中测试产生 source=kb_query 的 usage log 并从查询用户钱包扣费
  - kb_ids 含他人知识库时 403 且不产生任何向量化调用
"""

from __future__ import annotations

import math
from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.constants import (
    KB_CHUNK_EMBED_SUCCESS,
    KB_DOC_STATUS_FAILED,
    KB_DOC_STATUS_READY,
    KB_STATUS_DISABLED,
    MODEL_TYPE_EMBEDDING,
    STATUS_ENABLED,
    USAGE_SOURCE_KB_QUERY,
    VISIBILITY_PUBLIC,
)
from app.core.channel_key_crypto import encrypt_channel_key
from app.core.errors import ForbiddenError
from app.db.models.billing import BillingRule
from app.db.models.channel import Channel, ChannelKey, Provider
from app.db.models.kb import KbChunk, KbDocument, KnowledgeBase
from app.db.models.llm_model import LLMModel, ModelChannelMap
from app.db.models.log import UsageLog
from app.db.models.user import User
from app.db.models.wallet import Wallet, WalletTransaction
from app.db.session import get_db
from app.main import app
from app.services.embedding_gateway import (
    InternalEmbedResult,
    reset_embed_semaphore_for_tests,
)
from app.services.kb_service import kb_service
from app.services.retrieval_service import retrieval_service
from app.services.wallet_service import TX_CAPTURE, TX_HOLD, wallet_service
from app.storage import get_file_storage
from app.storage.local import LocalFileStorage

DIM = settings.kb_embedding_dim


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
    reset_embed_semaphore_for_tests()
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
    email = f"kb6-{uuid4().hex[:8]}@example.com"
    password = "password123"
    await client.post(
        "/auth/register",
        json={"email": email, "password": password, "nickname": "KbRetrieval"},
    )
    login = await client.post("/auth/login", json={"email": email, "password": password})
    assert login.status_code == 200
    return login.json()["access_token"], email


async def _user_by_email(session: AsyncSession, email: str) -> User:
    return (await session.execute(select(User).where(User.email == email))).scalar_one()


def _vec(*head: float) -> list[float]:
    return list(head) + [0.0] * (DIM - len(head))


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm


async def _seed_embedding_stack(session: AsyncSession) -> LLMModel:
    model = LLMModel(
        alias=f"emb-kb6-{uuid4().hex[:6]}",
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
            price_input=1_000_000,  # 1 token = 1 最小单位
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
        api_key_encrypted=encrypt_channel_key("sk-upstream-kb6"),
        key_masked="****kb6",
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


async def _seed_kb(
    session: AsyncSession,
    user: User,
    model: LLMModel,
    *,
    top_k: int = 5,
    score_threshold: float = 0.35,
    status: str = "active",
) -> KnowledgeBase:
    kb = KnowledgeBase(
        owner_user_id=user.id,
        name="检索测试库",
        description="任务6",
        embedding_model_id=model.id,
        embedding_dim=DIM,
        top_k=top_k,
        score_threshold=score_threshold,
        status=status,
    )
    session.add(kb)
    await session.flush()
    return kb


async def _seed_doc(
    session: AsyncSession,
    kb: KnowledgeBase,
    *,
    status: str = KB_DOC_STATUS_READY,
    file_name: str = "手册.pdf",
) -> KbDocument:
    doc = KbDocument(
        kb_id=kb.id,
        file_name=file_name,
        file_type="pdf",
        file_size_bytes=1024,
        storage_path=f"kb/{uuid4().hex}",
        chunk_size=kb.chunk_size,
        chunk_overlap=kb.chunk_overlap,
        separators=kb.separators or [],
        status=status,
    )
    session.add(doc)
    await session.flush()
    return doc


async def _seed_chunk(
    session: AsyncSession,
    doc: KbDocument,
    *,
    seq: int,
    content: str,
    embedding: list[float] | None,
    is_enabled: bool = True,
    embedding_status: str = KB_CHUNK_EMBED_SUCCESS,
    source_location: dict | None = None,
) -> KbChunk:
    chunk = KbChunk(
        document_id=doc.id,
        kb_id=doc.kb_id,
        seq=seq,
        content=content,
        token_count=max(1, len(content) // 4),
        source_location=source_location or {"page": seq + 1},
        is_enabled=is_enabled,
        embedding=embedding,
        embedding_status=embedding_status,
    )
    session.add(chunk)
    await session.flush()
    return chunk


def _mock_embed_result(
    query_vec: list[float], *, charge_amount: int = 3, input_tokens: int = 3
) -> InternalEmbedResult:
    return InternalEmbedResult(
        embeddings=[query_vec],
        input_tokens=input_tokens,
        is_estimated=False,
        request_id=str(uuid4()),
        model_id=uuid4(),
        model_alias="mock-emb",
        billing_rule=MagicMock(),
        channel_id=None,
        charge_amount=charge_amount,
        cost_amount=0,
        hold_amount=charge_amount,
        latency_ms=1,
    )


def _patch_embedding_httpx(query_vec: list[float], *, prompt_tokens: int = 4):
    """Mock 上游 embeddings，固定返回 query_vec。"""

    async def _post(url, *, json=None, headers=None, timeout=None):
        texts = json["input"]
        if isinstance(texts, str):
            texts = [texts]
        body = {
            "object": "list",
            "data": [
                {"object": "embedding", "index": i, "embedding": query_vec}
                for i in range(len(texts))
            ],
            "model": "text-embedding-3-small",
            "usage": {"prompt_tokens": prompt_tokens, "total_tokens": prompt_tokens},
        }
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json = MagicMock(return_value=body)
        return mock_resp

    mock_client = MagicMock()
    mock_client.post = AsyncMock(side_effect=_post)
    mock_client.aclose = AsyncMock()
    mock_client_cls = MagicMock()
    mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
    return patch(
        "app.services.embedding_gateway.httpx.AsyncClient", mock_client_cls
    )


# ---------------------------------------------------------------------------
# 服务层：排序 / 阈值 / 过滤
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_order_and_threshold_with_vector_fixture(
    db_session: AsyncSession,
) -> None:
    """构造向量夹具：返回顺序与 cosine 分数一致，低于阈值不返回。"""
    user = User(email=f"kb6-s-{uuid4().hex[:8]}@example.com", status=STATUS_ENABLED)
    db_session.add(user)
    await db_session.flush()
    model = await _seed_embedding_stack(db_session)
    kb = await _seed_kb(db_session, user, model, top_k=10, score_threshold=0.0)
    doc = await _seed_doc(db_session, kb)

    probe = _vec(1.0)
    fixtures = [
        ("完全一致", _vec(1.0)),
        ("部分相似", _vec(0.5, 0.5)),
        ("正交", _vec(0.0, 1.0)),
    ]
    for seq, (content, vec) in enumerate(fixtures):
        await _seed_chunk(db_session, doc, seq=seq, content=content, embedding=vec)

    # 正交向量 cosine=0；阈值 0.1 应过滤掉它
    threshold = 0.1
    expected_scores = {
        content: _cosine_similarity(probe, vec) for content, vec in fixtures
    }

    with patch(
        "app.services.retrieval_service.internal_embedding_service.embed_texts",
        new_callable=AsyncMock,
        return_value=_mock_embed_result(probe),
    ) as embed_mock:
        result = await retrieval_service.search(
            db_session,
            user=user,
            kb_ids=[kb.id],
            query="测试查询",
            top_k=10,
            score_threshold=threshold,
        )

    embed_mock.assert_awaited_once()
    contents = [c.content for c in result.chunks]
    assert contents == ["完全一致", "部分相似"]
    assert "正交" not in contents
    for hit in result.chunks:
        assert hit.score == pytest.approx(expected_scores[hit.content], abs=1e-6)
        assert hit.score >= threshold
    # 分数严格降序
    scores = [c.score for c in result.chunks]
    assert scores == sorted(scores, reverse=True)


@pytest.mark.asyncio
async def test_search_excludes_disabled_chunk_and_failed_document(
    db_session: AsyncSession,
) -> None:
    """禁用分块、failed 文档分块不出现在结果中。"""
    user = User(email=f"kb6-f-{uuid4().hex[:8]}@example.com", status=STATUS_ENABLED)
    db_session.add(user)
    await db_session.flush()
    model = await _seed_embedding_stack(db_session)
    kb = await _seed_kb(db_session, user, model, score_threshold=0.0)
    ready_doc = await _seed_doc(db_session, kb, file_name="就绪.pdf")
    failed_doc = await _seed_doc(
        db_session, kb, status=KB_DOC_STATUS_FAILED, file_name="失败.pdf"
    )

    probe = _vec(1.0)
    keep = await _seed_chunk(
        db_session,
        ready_doc,
        seq=0,
        content="应命中",
        embedding=_vec(1.0),
    )
    await _seed_chunk(
        db_session,
        ready_doc,
        seq=1,
        content="已禁用",
        embedding=_vec(1.0),
        is_enabled=False,
    )
    await _seed_chunk(
        db_session,
        failed_doc,
        seq=0,
        content="失败文档分块",
        embedding=_vec(1.0),
    )

    with patch(
        "app.services.retrieval_service.internal_embedding_service.embed_texts",
        new_callable=AsyncMock,
        return_value=_mock_embed_result(probe),
    ):
        result = await retrieval_service.search(
            db_session,
            user=user,
            kb_ids=[kb.id],
            query="任意",
            top_k=10,
            score_threshold=0.0,
        )

    assert [c.chunk_id for c in result.chunks] == [keep.id]
    assert [c.content for c in result.chunks] == ["应命中"]


@pytest.mark.asyncio
async def test_search_skips_disabled_kb(db_session: AsyncSession) -> None:
    """disabled 知识库不参与检索（结果为空，但仍完成归属校验）。"""
    user = User(email=f"kb6-d-{uuid4().hex[:8]}@example.com", status=STATUS_ENABLED)
    db_session.add(user)
    await db_session.flush()
    model = await _seed_embedding_stack(db_session)
    kb = await _seed_kb(
        db_session, user, model, status=KB_STATUS_DISABLED, score_threshold=0.0
    )
    doc = await _seed_doc(db_session, kb)
    await _seed_chunk(
        db_session, doc, seq=0, content="不可见", embedding=_vec(1.0)
    )

    with patch(
        "app.services.retrieval_service.internal_embedding_service.embed_texts",
        new_callable=AsyncMock,
    ) as embed_mock:
        result = await retrieval_service.search(
            db_session,
            user=user,
            kb_ids=[kb.id],
            query="任意",
            top_k=5,
            score_threshold=0.0,
        )

    embed_mock.assert_not_awaited()
    assert result.chunks == []


@pytest.mark.asyncio
async def test_search_multi_kb_merge_global_top_k(db_session: AsyncSession) -> None:
    """多库各库检索后按分数合并取全局 top_k。"""
    user = User(email=f"kb6-m-{uuid4().hex[:8]}@example.com", status=STATUS_ENABLED)
    db_session.add(user)
    await db_session.flush()
    model = await _seed_embedding_stack(db_session)
    kb_a = await _seed_kb(db_session, user, model, score_threshold=0.0)
    kb_b = await _seed_kb(db_session, user, model, score_threshold=0.0)
    doc_a = await _seed_doc(db_session, kb_a, file_name="A.pdf")
    doc_b = await _seed_doc(db_session, kb_b, file_name="B.pdf")

    probe = _vec(1.0)
    await _seed_chunk(db_session, doc_a, seq=0, content="A高", embedding=_vec(1.0))
    await _seed_chunk(
        db_session, doc_a, seq=1, content="A中", embedding=_vec(0.5, 0.5)
    )
    await _seed_chunk(
        db_session, doc_b, seq=0, content="B次高", embedding=_vec(0.8, 0.2)
    )

    with patch(
        "app.services.retrieval_service.internal_embedding_service.embed_texts",
        new_callable=AsyncMock,
        return_value=_mock_embed_result(probe),
    ) as embed_mock:
        result = await retrieval_service.search(
            db_session,
            user=user,
            kb_ids=[kb_a.id, kb_b.id],
            query="合并",
            top_k=2,
            score_threshold=0.0,
        )

    # 同模型只向量化一次
    embed_mock.assert_awaited_once()
    assert [c.content for c in result.chunks] == ["A高", "B次高"]


@pytest.mark.asyncio
async def test_search_foreign_kb_403_without_embedding(
    db_session: AsyncSession,
) -> None:
    """含他人知识库时 403，且不调用向量化。"""
    owner = User(email=f"kb6-o-{uuid4().hex[:8]}@example.com", status=STATUS_ENABLED)
    other = User(email=f"kb6-x-{uuid4().hex[:8]}@example.com", status=STATUS_ENABLED)
    db_session.add_all([owner, other])
    await db_session.flush()
    model = await _seed_embedding_stack(db_session)
    own_kb = await _seed_kb(db_session, owner, model)
    foreign_kb = await _seed_kb(db_session, other, model)

    with patch(
        "app.services.retrieval_service.internal_embedding_service.embed_texts",
        new_callable=AsyncMock,
    ) as embed_mock:
        with pytest.raises(ForbiddenError):
            await retrieval_service.search(
                db_session,
                user=owner,
                kb_ids=[own_kb.id, foreign_kb.id],
                query="越权",
                top_k=5,
                score_threshold=0.0,
            )

    embed_mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# API：命中测试计费与隔离
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hit_test_billing_kb_query_and_wallet_debit(
    user_client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """命中测试产生 source=kb_query 用量并从查询用户钱包扣费。"""
    token, email = await _register_and_login(user_client)
    user = await _user_by_email(db_session, email)
    model = await _seed_embedding_stack(db_session)
    monkeypatch.setattr(settings, "kb_default_embedding_model", model.alias)

    wallet = await wallet_service.get_or_create_personal_wallet(db_session, user.id)
    await wallet_service.recharge(
        db_session, wallet_id=wallet.id, amount=10_000, remark="test fund"
    )
    await db_session.flush()
    balance_before = (
        await db_session.execute(select(Wallet).where(Wallet.id == wallet.id))
    ).scalar_one().balance

    create = await user_client.post(
        "/kb",
        headers=_auth_header(token),
        json={
            "name": "命中测试库",
            "embedding_model": model.alias,
            "score_threshold": 0.0,
        },
    )
    assert create.status_code == 201, create.text
    kb_id = create.json()["id"]
    kb = await db_session.get(KnowledgeBase, kb_id)
    assert kb is not None
    doc = await _seed_doc(db_session, kb)
    probe = _vec(1.0)
    await _seed_chunk(
        db_session,
        doc,
        seq=0,
        content="产品退货政策说明",
        embedding=probe,
        source_location={"page": 3, "heading_path": ["售后"]},
    )

    with _patch_embedding_httpx(probe, prompt_tokens=4):
        resp = await user_client.post(
            f"/kb/{kb_id}/hit-test",
            headers=_auth_header(token),
            json={"query": "退货怎么办理", "top_k": 3},
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["top_k"] == 3
    assert data["score_threshold"] == pytest.approx(0.0)
    assert len(data["hits"]) == 1
    assert data["hits"][0]["content"] == "产品退货政策说明"
    assert data["hits"][0]["document_name"] == "手册.pdf"
    assert data["hits"][0]["source_location"]["page"] == 3
    assert data["estimated_charge_amount"] > 0
    assert data["request_id"]

    usage = (
        await db_session.execute(
            select(UsageLog).where(UsageLog.request_id == data["request_id"])
        )
    ).scalar_one()
    assert usage.source == USAGE_SOURCE_KB_QUERY
    assert usage.charge_amount == data["estimated_charge_amount"]

    wallet_after = (
        await db_session.execute(select(Wallet).where(Wallet.id == wallet.id))
    ).scalar_one()
    assert wallet_after.balance == balance_before - usage.charge_amount

    txs = list(
        (
            await db_session.execute(
                select(WalletTransaction).where(
                    WalletTransaction.ref_request_id == data["request_id"]
                )
            )
        )
        .scalars()
        .all()
    )
    kinds = {t.type for t in txs}
    assert TX_HOLD in kinds and TX_CAPTURE in kinds


@pytest.mark.asyncio
async def test_hit_test_foreign_kb_403(
    user_client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """访问他人知识库命中测试返回 403，不产生向量化。"""
    token_a, _email_a = await _register_and_login(user_client)
    _token_b, email_b = await _register_and_login(user_client)
    user_b = await _user_by_email(db_session, email_b)
    model = await _seed_embedding_stack(db_session)
    monkeypatch.setattr(settings, "kb_default_embedding_model", model.alias)

    kb = await _seed_kb(db_session, user_b, model)

    with patch(
        "app.services.retrieval_service.internal_embedding_service.embed_texts",
        new_callable=AsyncMock,
    ) as embed_mock:
        resp = await user_client.post(
            f"/kb/{kb.id}/hit-test",
            headers=_auth_header(token_a),
            json={"query": "越权测试"},
        )

    assert resp.status_code == 403
    embed_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_hit_test_uses_kb_default_params(
    user_client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """未传 top_k / score_threshold 时使用库默认检索参数。"""
    token, email = await _register_and_login(user_client)
    user = await _user_by_email(db_session, email)
    model = await _seed_embedding_stack(db_session)
    monkeypatch.setattr(settings, "kb_default_embedding_model", model.alias)
    kb = await _seed_kb(
        db_session, user, model, top_k=2, score_threshold=0.5
    )
    doc = await _seed_doc(db_session, kb)
    probe = _vec(1.0)
    await _seed_chunk(
        db_session, doc, seq=0, content="高相似", embedding=_vec(1.0)
    )
    await _seed_chunk(
        db_session, doc, seq=1, content="低相似", embedding=_vec(0.1, 0.9)
    )

    with patch(
        "app.services.retrieval_service.internal_embedding_service.embed_texts",
        new_callable=AsyncMock,
        return_value=_mock_embed_result(probe, charge_amount=1, input_tokens=1),
    ):
        resp = await user_client.post(
            f"/kb/{kb.id}/hit-test",
            headers=_auth_header(token),
            json={"query": "默认参数"},
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["top_k"] == 2
    assert data["score_threshold"] == pytest.approx(0.5)
    # 低相似 cosine ≈ 0.1 < 0.5，应被过滤
    assert [h["content"] for h in data["hits"]] == ["高相似"]
