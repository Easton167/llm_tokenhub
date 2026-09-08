"""知识库专项任务 8：Chat RAG 问答编排。

验收：
  - 挂载知识库后提问，回答基于命中分块且引用列表可定位分块
  - strict 未命中：固定文案、无模型调用、无 chat 扣费（可有 query 向量化）
  - fallback 未命中：走普通对话并标注未引用
  - 上下文预算截断按分数舍弃
  - 引用随消息持久化可回看
  - 挂载他人知识库 403
  - 卸载后回退普通对话；未挂载会话行为不变
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.channel_key_crypto import encrypt_channel_key
from app.core.config import settings
from app.core.constants import (
    CHAT_MISS_STRATEGY_FALLBACK,
    KB_CHUNK_EMBED_SUCCESS,
    KB_DOC_STATUS_READY,
    MODEL_TYPE_EMBEDDING,
    RAG_MISS_REPLY,
    STATUS_ENABLED,
    USAGE_SOURCE_CHAT,
    VISIBILITY_PUBLIC,
)
from app.db.models.billing import BillingRule
from app.db.models.channel import Channel, ChannelKey, Provider
from app.db.models.kb import KbChunk, KbDocument, KnowledgeBase
from app.db.models.llm_model import LLMModel, ModelChannelMap
from app.db.models.log import UsageLog
from app.db.models.user import User
from app.db.session import get_db
from app.main import app
from app.schemas.openai import ChatMessage
from app.services.auth_service import auth_service
from app.services.rag_chat_service import (
    apply_context_budget,
    compute_context_budget,
    rag_chat_service,
)
from app.services.retrieval_service import RetrievedChunk
from app.services.wallet_service import wallet_service
from tests.test_stream_api import (
    _MockStreamResponse,
    _make_success_stream_chunks,
    _patch_httpx_stream,
)

DIM = settings.kb_embedding_dim


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


async def _register_and_login(client: AsyncClient) -> tuple[str, str]:
    email = f"kb8-{uuid4().hex[:8]}@example.com"
    password = "password123"
    await client.post(
        "/auth/register",
        json={"email": email, "password": password, "nickname": "KbRag"},
    )
    login = await client.post("/auth/login", json={"email": email, "password": password})
    assert login.status_code == 200
    return login.json()["access_token"], email


async def _user_by_email(session: AsyncSession, email: str) -> User:
    return (await session.execute(select(User).where(User.email == email))).scalar_one()


def _vec(*head: float) -> list[float]:
    return list(head) + [0.0] * (DIM - len(head))


async def _seed_embedding_stack(session: AsyncSession) -> LLMModel:
    model = LLMModel(
        alias=f"emb-kb8-{uuid4().hex[:6]}",
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
        api_key_encrypted=encrypt_channel_key("sk-upstream-kb8"),
        key_masked="****kb8",
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


async def _seed_chat_stack(
    session: AsyncSession, user: User, *, max_context_tokens: int = 10_000
) -> tuple[str, LLMModel]:
    _, plaintext = await auth_service.create_api_key(
        session, user_id=user.id, name="rag-chat-key"
    )
    model = LLMModel(
        alias=f"chat-kb8-{uuid4().hex[:6]}",
        display_name="chat",
        model_type="chat",
        capabilities={"max_context_tokens": max_context_tokens, "stream": True},
        visibility=VISIBILITY_PUBLIC,
        status=STATUS_ENABLED,
    )
    session.add(model)
    await session.flush()
    session.add(
        BillingRule(
            model_id=model.id,
            version=1,
            price_input=180_000,
            price_output=720_000,
            price_cached_input=90_000,
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
        name=f"chat-prov-{uuid4().hex[:6]}",
        adapter_type="openai_compatible",
        default_base_url="https://api.openai.com/v1",
    )
    session.add(provider)
    await session.flush()
    channel = Channel(
        provider_id=provider.id,
        name=f"chat-chan-{uuid4().hex[:6]}",
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
    session.add(
        ChannelKey(
            channel_id=channel.id,
            api_key_encrypted=encrypt_channel_key("sk-upstream-chat"),
            key_masked="****chat",
            weight=1,
            status=STATUS_ENABLED,
            health_status=0,
        )
    )
    await session.flush()
    session.add(
        ModelChannelMap(
            model_id=model.id,
            channel_id=channel.id,
            upstream_model_name="gpt-4o-2024-11-20",
            weight=1,
            status=STATUS_ENABLED,
        )
    )
    await session.flush()
    return plaintext, model


async def _seed_kb(
    session: AsyncSession,
    user: User,
    emb_model: LLMModel,
    *,
    top_k: int = 5,
    score_threshold: float = 0.0,
) -> KnowledgeBase:
    kb = KnowledgeBase(
        owner_user_id=user.id,
        name="RAG测试库",
        embedding_model_id=emb_model.id,
        embedding_dim=DIM,
        top_k=top_k,
        score_threshold=score_threshold,
        status="active",
    )
    session.add(kb)
    await session.flush()
    return kb


async def _seed_doc_with_chunk(
    session: AsyncSession,
    kb: KnowledgeBase,
    *,
    content: str,
    embedding: list[float],
    file_name: str = "手册.pdf",
    token_count: int | None = None,
) -> KbChunk:
    doc = KbDocument(
        kb_id=kb.id,
        file_name=file_name,
        file_type="pdf",
        file_size_bytes=1024,
        storage_path=f"kb/{uuid4().hex}",
        chunk_size=kb.chunk_size,
        chunk_overlap=kb.chunk_overlap,
        separators=kb.separators or [],
        status=KB_DOC_STATUS_READY,
    )
    session.add(doc)
    await session.flush()
    tokens = token_count if token_count is not None else max(1, len(content) // 4)
    chunk = KbChunk(
        document_id=doc.id,
        kb_id=kb.id,
        seq=0,
        content=content,
        token_count=tokens,
        source_location={"page": 1},
        is_enabled=True,
        embedding=embedding,
        embedding_status=KB_CHUNK_EMBED_SUCCESS,
    )
    session.add(chunk)
    await session.flush()
    return chunk


def _mock_search_result(
    chunks: list[RetrievedChunk] | None = None,
    *,
    charge_amount: int = 3,
    input_tokens: int = 3,
):
    from app.services.retrieval_service import RetrievalSearchResult

    return RetrievalSearchResult(
        chunks=list(chunks or []),
        charge_amount=charge_amount,
        input_tokens=input_tokens,
        request_ids=[str(uuid4())],
    )


def _patch_retrieval_search(result):
    return patch(
        "app.services.rag_chat_service.retrieval_service.search",
        new=AsyncMock(return_value=result),
    )


def _parse_sse(text: str) -> list[dict[str, Any] | str]:
    events: list[dict[str, Any] | str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            events.append("[DONE]")
            continue
        import json

        events.append(json.loads(payload))
    return events


# ---------------------------------------------------------------------------
# 纯函数：上下文预算
# ---------------------------------------------------------------------------


def test_compute_context_budget_takes_min_of_ratio_and_cap() -> None:
    # 10000 * 0.4 = 4000 → 取 4000
    assert compute_context_budget(10_000) == 4000
    # 5000 * 0.4 = 2000 → 取 2000
    assert compute_context_budget(5_000) == 2000


def test_apply_context_budget_drops_low_score_chunks() -> None:
    chunks = [
        RetrievedChunk(
            chunk_id=uuid4(),
            kb_id=uuid4(),
            document_id=uuid4(),
            document_name="a.pdf",
            content="高分",
            score=0.9,
            seq=0,
            source_location=None,
            token_count=100,
        ),
        RetrievedChunk(
            chunk_id=uuid4(),
            kb_id=uuid4(),
            document_id=uuid4(),
            document_name="b.pdf",
            content="中分",
            score=0.8,
            seq=1,
            source_location=None,
            token_count=100,
        ),
        RetrievedChunk(
            chunk_id=uuid4(),
            kb_id=uuid4(),
            document_id=uuid4(),
            document_name="c.pdf",
            content="低分",
            score=0.7,
            seq=2,
            source_location=None,
            token_count=100,
        ),
    ]
    kept, used, truncated = apply_context_budget(chunks, budget_tokens=250)
    assert len(kept) == 2
    assert kept[0].document_name == "a.pdf"
    assert kept[1].document_name == "b.pdf"
    assert used == 200
    assert truncated == 1


# ---------------------------------------------------------------------------
# 会话挂载 API
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_mount_kb_and_persist_citations(
    user_client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    token, email = await _register_and_login(user_client)
    user = await _user_by_email(db_session, email)
    emb = await _seed_embedding_stack(db_session)
    monkeypatch.setattr(settings, "kb_default_embedding_model", emb.alias)
    kb = await _seed_kb(db_session, user, emb)
    chunk = await _seed_doc_with_chunk(
        db_session, kb, content="年假共计十五天", embedding=_vec(1.0)
    )

    created = await user_client.post(
        "/chat/sessions",
        headers=_auth_header(token),
        json={
            "title": "知识库会话",
            "kb_ids": [str(kb.id)],
            "miss_strategy": "strict",
            "retrieval_top_k": 3,
        },
    )
    assert created.status_code == 201, created.text
    session = created.json()
    assert session["kb_ids"] == [str(kb.id)]
    assert session["miss_strategy"] == "strict"
    assert session["retrieval_top_k"] == 3
    session_id = session["id"]

    # 挂载他人库 → 403
    other = User(email=f"other-{uuid4().hex[:6]}@example.com", status=STATUS_ENABLED)
    db_session.add(other)
    await db_session.flush()
    other_kb = await _seed_kb(db_session, other, emb)
    forbidden = await user_client.patch(
        f"/chat/sessions/{session_id}",
        headers=_auth_header(token),
        json={"kb_ids": [str(other_kb.id)]},
    )
    assert forbidden.status_code == 403

    # 引用持久化
    cite = {
        "chunk_id": str(chunk.id),
        "kb_id": str(kb.id),
        "document_id": str(chunk.document_id),
        "document_name": "手册.pdf",
        "content_summary": "年假共计十五天",
        "score": 0.95,
        "index": 1,
    }
    replaced = await user_client.put(
        f"/chat/sessions/{session_id}/messages",
        headers=_auth_header(token),
        json={
            "messages": [
                {"role": "user", "content": "年假几天？"},
                {
                    "role": "assistant",
                    "content": "根据资料[1]，年假十五天。",
                    "citations": [cite],
                },
            ]
        },
    )
    assert replaced.status_code == 200, replaced.text
    msgs = replaced.json()["messages"]
    assert msgs[1]["citations"][0]["chunk_id"] == str(chunk.id)
    assert msgs[1]["citations"][0]["document_name"] == "手册.pdf"

    detail = await user_client.get(
        f"/chat/sessions/{session_id}", headers=_auth_header(token)
    )
    assert detail.status_code == 200
    assert detail.json()["messages"][1]["citations"][0]["chunk_id"] == str(chunk.id)

    # 卸载
    unmounted = await user_client.patch(
        f"/chat/sessions/{session_id}",
        headers=_auth_header(token),
        json={"kb_ids": []},
    )
    assert unmounted.status_code == 200
    assert unmounted.json()["kb_ids"] == []


# ---------------------------------------------------------------------------
# RAG 编排：命中 / strict / fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kb_completions_hit_with_citations(
    user_client: AsyncClient, db_session: AsyncSession
) -> None:
    token, email = await _register_and_login(user_client)
    user = await _user_by_email(db_session, email)
    wallet = await wallet_service.get_or_create_personal_wallet(db_session, user.id)
    wallet.balance = 100_000_000
    await db_session.flush()

    emb = await _seed_embedding_stack(db_session)
    api_key, chat_model = await _seed_chat_stack(db_session, user)
    kb = await _seed_kb(db_session, user, emb)
    chunk = await _seed_doc_with_chunk(
        db_session,
        kb,
        content="公司年假政策：全职员工每年享有十五天带薪年假。",
        embedding=_vec(1.0),
        file_name="人事手册.pdf",
    )

    sess = await user_client.post(
        "/chat/sessions",
        headers=_auth_header(token),
        json={"title": "RAG", "kb_ids": [str(kb.id)], "miss_strategy": "fallback"},
    )
    assert sess.status_code == 201
    session_id = sess.json()["id"]

    hit = RetrievedChunk(
        chunk_id=chunk.id,
        kb_id=kb.id,
        document_id=chunk.document_id,
        document_name="人事手册.pdf",
        content=chunk.content,
        score=0.92,
        seq=0,
        source_location={"page": 1},
        token_count=chunk.token_count,
    )
    lines = _make_success_stream_chunks(content_parts=["根据", "资料，年假十五天"])
    chat_patch, _ = _patch_httpx_stream([_MockStreamResponse(200, lines)])
    search_patch = _patch_retrieval_search(_mock_search_result([hit]))

    with search_patch, chat_patch:
        resp = await user_client.post(
            "/chat/kb-completions",
            headers={**_auth_header(token), "X-Api-Key": api_key},
            json={
                "session_id": session_id,
                "model": chat_model.alias,
                "messages": [{"role": "user", "content": "年假有几天？"}],
                "stream": True,
            },
        )

    assert resp.status_code == 200, resp.text
    assert "text/event-stream" in resp.headers["content-type"]
    events = _parse_sse(resp.text)
    assert "[DONE]" in events
    citations_evt = next(
        e for e in events if isinstance(e, dict) and e.get("object") == "chat.completion.citations"
    )
    assert citations_evt["rag"]["hit"] is True
    assert citations_evt["rag"]["used_knowledge_base"] is True
    assert citations_evt["rag"]["call_model"] is True
    assert len(citations_evt["citations"]) == 1
    assert citations_evt["citations"][0]["chunk_id"] == str(chunk.id)
    assert citations_evt["citations"][0]["document_name"] == "人事手册.pdf"

    contents = []
    for e in events:
        if isinstance(e, dict) and e.get("object") == "chat.completion.chunk":
            delta = e["choices"][0].get("delta") or {}
            if delta.get("content"):
                contents.append(delta["content"])
    assert "".join(contents) == "根据资料，年假十五天"


@pytest.mark.asyncio
async def test_kb_completions_strict_miss_no_chat_charge(
    user_client: AsyncClient, db_session: AsyncSession
) -> None:
    token, email = await _register_and_login(user_client)
    user = await _user_by_email(db_session, email)
    wallet = await wallet_service.get_or_create_personal_wallet(db_session, user.id)
    wallet.balance = 100_000_000
    await db_session.flush()

    emb = await _seed_embedding_stack(db_session)
    api_key, chat_model = await _seed_chat_stack(db_session, user)
    kb = await _seed_kb(db_session, user, emb)

    chat_called = MagicMock()

    def _fail_chat(*_a, **_k):
        chat_called()
        raise AssertionError("chat upstream must not be called on strict miss")

    search_patch = _patch_retrieval_search(
        _mock_search_result([], charge_amount=5, input_tokens=4)
    )
    with search_patch, patch("app.api.v1.chat.httpx.AsyncClient", side_effect=_fail_chat):
        resp = await user_client.post(
            "/chat/kb-completions",
            headers={**_auth_header(token), "X-Api-Key": api_key},
            json={
                "model": chat_model.alias,
                "kb_ids": [str(kb.id)],
                "miss_strategy": "strict",
                "messages": [{"role": "user", "content": "完全不相关的问题"}],
                "stream": True,
            },
        )

    assert resp.status_code == 200, resp.text
    events = _parse_sse(resp.text)
    contents = []
    for e in events:
        if isinstance(e, dict) and e.get("object") == "chat.completion.chunk":
            delta = e["choices"][0].get("delta") or {}
            if delta.get("content"):
                contents.append(delta["content"])
    assert "".join(contents) == RAG_MISS_REPLY

    citations_evt = next(
        e for e in events if isinstance(e, dict) and e.get("object") == "chat.completion.citations"
    )
    assert citations_evt["rag"]["hit"] is False
    assert citations_evt["rag"]["call_model"] is False
    assert citations_evt["citations"] == []
    assert citations_evt["rag"]["query_charge_amount"] == 5
    chat_called.assert_not_called()

    chat_usage = (
        await db_session.execute(
            select(UsageLog).where(UsageLog.source == USAGE_SOURCE_CHAT)
        )
    ).scalars().all()
    assert chat_usage == []


@pytest.mark.asyncio
async def test_kb_completions_fallback_miss_calls_model(
    user_client: AsyncClient, db_session: AsyncSession
) -> None:
    token, email = await _register_and_login(user_client)
    user = await _user_by_email(db_session, email)
    wallet = await wallet_service.get_or_create_personal_wallet(db_session, user.id)
    wallet.balance = 100_000_000
    await db_session.flush()

    emb = await _seed_embedding_stack(db_session)
    api_key, chat_model = await _seed_chat_stack(db_session, user)
    kb = await _seed_kb(db_session, user, emb)

    lines = _make_success_stream_chunks(content_parts=["通用回答，未引用知识库"])
    chat_patch, call_counter = _patch_httpx_stream([_MockStreamResponse(200, lines)])
    search_patch = _patch_retrieval_search(_mock_search_result([]))

    with search_patch, chat_patch:
        resp = await user_client.post(
            "/chat/kb-completions",
            headers={**_auth_header(token), "X-Api-Key": api_key},
            json={
                "model": chat_model.alias,
                "kb_ids": [str(kb.id)],
                "miss_strategy": "fallback",
                "messages": [{"role": "user", "content": "天气怎么样"}],
                "stream": True,
            },
        )

    assert resp.status_code == 200, resp.text
    assert call_counter[0] >= 1
    events = _parse_sse(resp.text)
    citations_evt = next(
        e for e in events if isinstance(e, dict) and e.get("object") == "chat.completion.citations"
    )
    assert citations_evt["rag"]["hit"] is False
    assert citations_evt["rag"]["call_model"] is True
    assert citations_evt["rag"]["used_knowledge_base"] is False
    assert citations_evt["rag"]["miss_strategy"] == CHAT_MISS_STRATEGY_FALLBACK


@pytest.mark.asyncio
async def test_prepare_context_budget_truncation(db_session: AsyncSession) -> None:
    user = User(email=f"budget-{uuid4().hex[:6]}@example.com", status=STATUS_ENABLED)
    db_session.add(user)
    await db_session.flush()
    emb = await _seed_embedding_stack(db_session)
    kb = await _seed_kb(db_session, user, emb, score_threshold=0.0)

    # 三个高分块，各自 1500 token；预算 = min(10000*0.4, 4000)=4000 → 只能留 2 个
    chunks = [
        RetrievedChunk(
            chunk_id=uuid4(),
            kb_id=kb.id,
            document_id=uuid4(),
            document_name=name,
            content=f"内容{i}",
            score=0.9 - i * 0.05,
            seq=i,
            source_location=None,
            token_count=1500,
        )
        for i, name in enumerate(["a.pdf", "b.pdf", "c.pdf"])
    ]
    search_patch = _patch_retrieval_search(_mock_search_result(chunks))

    with search_patch:
        plan = await rag_chat_service.prepare(
            db_session,
            user=user,
            messages=[ChatMessage(role="user", content="问")],
            kb_ids=[kb.id],
            miss_strategy=CHAT_MISS_STRATEGY_FALLBACK,
            top_k=10,
            score_threshold=0.0,
            model_context_tokens=10_000,
        )

    assert plan.hit is True
    assert plan.context_budget == 4000
    assert plan.context_tokens == 3000
    assert plan.truncated_count == 1
    assert len(plan.citations) == 2
    assert plan.citations[0].document_name == "a.pdf"
    assert plan.citations[1].document_name == "b.pdf"


@pytest.mark.asyncio
async def test_kb_completions_other_users_kb_403(
    user_client: AsyncClient, db_session: AsyncSession
) -> None:
    token, email = await _register_and_login(user_client)
    user = await _user_by_email(db_session, email)
    emb = await _seed_embedding_stack(db_session)
    api_key, chat_model = await _seed_chat_stack(db_session, user)

    other = User(email=f"o-{uuid4().hex[:6]}@example.com", status=STATUS_ENABLED)
    db_session.add(other)
    await db_session.flush()
    other_kb = await _seed_kb(db_session, other, emb)

    resp = await user_client.post(
        "/chat/kb-completions",
        headers={**_auth_header(token), "X-Api-Key": api_key},
        json={
            "model": chat_model.alias,
            "kb_ids": [str(other_kb.id)],
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_unmounted_session_passthrough_no_retrieval(
    user_client: AsyncClient, db_session: AsyncSession
) -> None:
    """卸载/未挂载：不检索，直接走模型（与普通对话一致）。"""
    token, email = await _register_and_login(user_client)
    user = await _user_by_email(db_session, email)
    wallet = await wallet_service.get_or_create_personal_wallet(db_session, user.id)
    wallet.balance = 100_000_000
    await db_session.flush()

    api_key, chat_model = await _seed_chat_stack(db_session, user)
    lines = _make_success_stream_chunks(content_parts=["普通回答"])
    chat_patch, _ = _patch_httpx_stream([_MockStreamResponse(200, lines)])

    search_mock = AsyncMock()
    with chat_patch, patch(
        "app.services.rag_chat_service.retrieval_service.search", search_mock
    ):
        resp = await user_client.post(
            "/chat/kb-completions",
            headers={**_auth_header(token), "X-Api-Key": api_key},
            json={
                "model": chat_model.alias,
                "kb_ids": [],
                "messages": [{"role": "user", "content": "你好"}],
                "stream": True,
            },
        )

    assert resp.status_code == 200, resp.text
    search_mock.assert_not_awaited()
    events = _parse_sse(resp.text)
    citations_evt = next(
        e for e in events if isinstance(e, dict) and e.get("object") == "chat.completion.citations"
    )
    assert citations_evt["rag"]["used_knowledge_base"] is False
    assert citations_evt["citations"] == []


@pytest.mark.asyncio
async def test_existing_chat_sessions_still_work(user_client: AsyncClient) -> None:
    """未挂载知识库的会话 CRUD 与现状一致。"""
    token, _ = await _register_and_login(user_client)
    headers = _auth_header(token)
    created = await user_client.post(
        "/chat/sessions",
        headers=headers,
        json={"title": "普通会话", "model": "gpt-4o"},
    )
    assert created.status_code == 201
    body = created.json()
    assert body["kb_ids"] == []
    assert body["miss_strategy"] == "fallback"
    assert body["messages"] == []
