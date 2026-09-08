"""知识库专项任务 1：数据模型与 pgvector 迁移的基础模型测试。

验收：
  - kb_chunks 可写入向量并按 cosine 相似度排序查询（HNSW 索引，cosine 距离）
  - 文档状态机 / 任务类型的 check 约束生效；kb_ingest_jobs 幂等键唯一
  - usage_logs / request_logs 未显式写 source 时默认 'api'（存量兼容口径）
  - roles 知识库配额字段组可空（NULL = 系统默认值）
  - 中文内容 UTF-8 round-trip 不损坏
"""

from __future__ import annotations

import math
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.constants import (
    MODEL_TYPE_EMBEDDING,
    STATUS_ENABLED,
    VISIBILITY_PUBLIC,
)
from app.db.models import (
    ApiKey,
    KbChunk,
    KbDocument,
    KbIngestJob,
    KnowledgeBase,
    LLMModel,
    RequestLog,
    Role,
    UsageLog,
    User,
)

DIM = settings.kb_embedding_dim


def _vec(*head: float) -> list[float]:
    """构造维度为 DIM 的向量：前几维取给定值，其余补 0。"""
    values = list(head) + [0.0] * (DIM - len(head))
    return values


def _cosine_distance(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return 1.0 - dot / norm


async def _seed_user(session: AsyncSession) -> User:
    user = User(email=f"kb-{uuid4().hex[:8]}@example.com", status=STATUS_ENABLED)
    session.add(user)
    await session.flush()
    return user


async def _seed_embedding_model(session: AsyncSession) -> LLMModel:
    model = LLMModel(
        alias=f"embed-{uuid4().hex[:6]}",
        display_name="测试嵌入模型",
        model_type=MODEL_TYPE_EMBEDDING,
        capabilities={},
        visibility=VISIBILITY_PUBLIC,
        status=STATUS_ENABLED,
    )
    session.add(model)
    await session.flush()
    return model


async def _seed_kb(session: AsyncSession, user: User, model: LLMModel) -> KnowledgeBase:
    kb = KnowledgeBase(
        owner_user_id=user.id,
        name="测试知识库",
        description="产品手册与内部制度",
        embedding_model_id=model.id,
        embedding_dim=DIM,
    )
    session.add(kb)
    await session.flush()
    return kb


async def _seed_document(session: AsyncSession, kb: KnowledgeBase) -> KbDocument:
    doc = KbDocument(
        kb_id=kb.id,
        file_name="产品手册.pdf",
        file_type="pdf",
        file_size_bytes=1024,
        storage_path=f"kb/{uuid4().hex}",
        chunk_size=kb.chunk_size,
        chunk_overlap=kb.chunk_overlap,
        separators=kb.separators,
    )
    session.add(doc)
    await session.flush()
    return doc


@pytest.mark.asyncio
async def test_kb_defaults_and_chinese_roundtrip(db_session: AsyncSession) -> None:
    """库默认参数落地；中文名称/描述 UTF-8 round-trip 完整。"""
    user = await _seed_user(db_session)
    model = await _seed_embedding_model(db_session)
    kb = await _seed_kb(db_session, user, model)
    await db_session.refresh(kb)

    assert kb.chunk_size == 500
    assert kb.chunk_overlap == 50
    assert kb.top_k == 5
    assert kb.score_threshold == pytest.approx(0.35)
    assert kb.doc_count == 0 and kb.chunk_count == 0 and kb.storage_bytes == 0
    assert kb.status == "active"
    assert kb.name == "测试知识库"
    assert kb.description == "产品手册与内部制度"


@pytest.mark.asyncio
async def test_kb_chunk_cosine_similarity_query(db_session: AsyncSession) -> None:
    """向量写入后可按 cosine 距离排序检索，顺序与 Python 计算一致。"""
    user = await _seed_user(db_session)
    model = await _seed_embedding_model(db_session)
    kb = await _seed_kb(db_session, user, model)
    doc = await _seed_document(db_session, kb)

    probe = _vec(1.0)
    vectors = {
        "分块甲：与查询向量完全一致": _vec(1.0),
        "分块乙：与查询向量部分相似": _vec(0.5, 0.5),
        "分块丙：与查询向量正交": _vec(0.0, 1.0),
    }
    for seq, (content, vec) in enumerate(vectors.items()):
        db_session.add(
            KbChunk(
                document_id=doc.id,
                kb_id=kb.id,
                seq=seq,
                content=content,
                token_count=10,
                source_location={"page": seq + 1, "heading_path": ["第一章", "概述"]},
                embedding=vec,
                embedding_status="success",
            )
        )
    await db_session.flush()

    distance = KbChunk.embedding.cosine_distance(probe).label("distance")
    rows = (
        await db_session.execute(
            select(KbChunk.content, distance)
            .where(KbChunk.document_id == doc.id)
            .order_by(distance)
        )
    ).all()

    contents = [row.content for row in rows]
    assert contents == [
        "分块甲：与查询向量完全一致",
        "分块乙：与查询向量部分相似",
        "分块丙：与查询向量正交",
    ]
    for row in rows:
        expected = _cosine_distance(probe, vectors[row.content])
        assert row.distance == pytest.approx(expected, abs=1e-6)


@pytest.mark.asyncio
async def test_kb_chunk_embedding_nullable_and_roundtrip(db_session: AsyncSession) -> None:
    """未向量化分块 embedding 为 NULL；写入的向量可完整读回。"""
    user = await _seed_user(db_session)
    model = await _seed_embedding_model(db_session)
    kb = await _seed_kb(db_session, user, model)
    doc = await _seed_document(db_session, kb)

    pending = KbChunk(
        document_id=doc.id, kb_id=kb.id, seq=0, content="待向量化的中文分块", token_count=8
    )
    embedded = KbChunk(
        document_id=doc.id,
        kb_id=kb.id,
        seq=1,
        content="已向量化分块",
        token_count=6,
        embedding=_vec(0.25, 0.75),
        embedding_status="success",
    )
    db_session.add_all([pending, embedded])
    await db_session.flush()
    await db_session.refresh(pending)
    await db_session.refresh(embedded)

    assert pending.embedding is None
    assert pending.embedding_status == "pending"
    assert pending.is_enabled is True
    assert pending.is_manually_edited is False
    got = list(embedded.embedding)
    assert len(got) == DIM
    assert got[0] == pytest.approx(0.25)
    assert got[1] == pytest.approx(0.75)


@pytest.mark.asyncio
async def test_kb_document_status_check_constraint(db_session: AsyncSession) -> None:
    """状态机取值受 check 约束保护，非法状态写入被拒绝。"""
    user = await _seed_user(db_session)
    model = await _seed_embedding_model(db_session)
    kb = await _seed_kb(db_session, user, model)
    doc = await _seed_document(db_session, kb)
    assert doc.status == "uploaded"

    async with db_session.begin_nested():
        db_session.add(
            KbDocument(
                kb_id=kb.id,
                file_name="bad.txt",
                file_type="txt",
                file_size_bytes=1,
                storage_path=f"kb/{uuid4().hex}",
                status="not_a_status",
            )
        )
        with pytest.raises(IntegrityError):
            await db_session.flush()


@pytest.mark.asyncio
async def test_kb_ingest_job_idempotency_key_unique(db_session: AsyncSession) -> None:
    """同幂等键的任务只能存在一条（重复执行不产生重复任务）。"""
    user = await _seed_user(db_session)
    model = await _seed_embedding_model(db_session)
    kb = await _seed_kb(db_session, user, model)
    doc = await _seed_document(db_session, kb)

    idem = f"{doc.id}:parse:v1"
    db_session.add(
        KbIngestJob(job_type="parse", document_id=doc.id, kb_id=kb.id, idempotency_key=idem)
    )
    await db_session.flush()

    async with db_session.begin_nested():
        db_session.add(
            KbIngestJob(job_type="parse", document_id=doc.id, kb_id=kb.id, idempotency_key=idem)
        )
        with pytest.raises(IntegrityError):
            await db_session.flush()


@pytest.mark.asyncio
async def test_kb_cascade_delete_document_chunks_jobs(db_session: AsyncSession) -> None:
    """删除知识库后文档 / 分块 / 任务级联清理（ON DELETE CASCADE）。"""
    user = await _seed_user(db_session)
    model = await _seed_embedding_model(db_session)
    kb = await _seed_kb(db_session, user, model)
    doc = await _seed_document(db_session, kb)
    db_session.add(
        KbChunk(document_id=doc.id, kb_id=kb.id, seq=0, content="级联删除分块", token_count=5)
    )
    db_session.add(
        KbIngestJob(
            job_type="parse",
            document_id=doc.id,
            kb_id=kb.id,
            idempotency_key=f"{doc.id}:parse:v1",
        )
    )
    await db_session.flush()

    await db_session.delete(kb)
    await db_session.flush()

    remaining_docs = (
        await db_session.execute(select(KbDocument).where(KbDocument.kb_id == kb.id))
    ).scalars().all()
    remaining_chunks = (
        await db_session.execute(select(KbChunk).where(KbChunk.kb_id == kb.id))
    ).scalars().all()
    remaining_jobs = (
        await db_session.execute(select(KbIngestJob).where(KbIngestJob.kb_id == kb.id))
    ).scalars().all()
    assert remaining_docs == [] and remaining_chunks == [] and remaining_jobs == []


@pytest.mark.asyncio
async def test_usage_and_request_log_source_defaults_api(db_session: AsyncSession) -> None:
    """不显式写 source 时（既有一、二期代码路径），落库默认 'api'。"""
    user = await _seed_user(db_session)
    model = await _seed_embedding_model(db_session)
    api_key = ApiKey(
        user_id=user.id,
        name="kb-test-key",
        key_prefix="sk-test",
        key_hash=f"hash-{uuid4().hex}",
        status=STATUS_ENABLED,
    )
    db_session.add(api_key)
    await db_session.flush()

    request_id = f"req-kb-{uuid4().hex}"
    req_log = RequestLog(
        request_id=request_id,
        api_key_id=api_key.id,
        user_id=user.id,
        billing_subject="personal",
        model_id=model.id,
        status="success",
        http_status=200,
        is_stream=False,
    )
    db_session.add(req_log)
    await db_session.flush()
    usage_log = UsageLog(
        request_id=request_id,
        input_tokens=10,
        output_tokens=0,
        cached_tokens=0,
        billing_rule_version=1,
        cost_amount=0,
        charge_amount=0,
        is_estimated=False,
    )
    db_session.add(usage_log)
    await db_session.flush()
    await db_session.refresh(req_log)
    await db_session.refresh(usage_log)

    assert req_log.source == "api"
    assert usage_log.source == "api"

    # 显式写入 kb_ingest 来源也可用（后续任务 5 使用）
    kb_request_id = f"req-kb-{uuid4().hex}"
    db_session.add(
        RequestLog(
            request_id=kb_request_id,
            api_key_id=api_key.id,
            user_id=user.id,
            billing_subject="personal",
            model_id=model.id,
            status="success",
            http_status=200,
            is_stream=False,
            source="kb_ingest",
        )
    )
    await db_session.flush()
    fetched = (
        await db_session.execute(
            select(RequestLog).where(RequestLog.request_id == kb_request_id)
        )
    ).scalar_one()
    assert fetched.source == "kb_ingest"


@pytest.mark.asyncio
async def test_role_kb_quota_fields_nullable(db_session: AsyncSession) -> None:
    """角色配额字段组：未配置为 NULL（系统默认），可整数配置。"""
    role_default = Role(name=f"kb-role-{uuid4().hex[:8]}")
    role_custom = Role(
        name=f"kb-role-{uuid4().hex[:8]}",
        kb_max_kb_count=10,
        kb_max_docs_per_kb=200,
        kb_max_storage_bytes=2 * 1024**3,
        kb_max_file_size_bytes=50 * 1024**2,
    )
    db_session.add_all([role_default, role_custom])
    await db_session.flush()
    await db_session.refresh(role_default)
    await db_session.refresh(role_custom)

    assert role_default.kb_max_kb_count is None
    assert role_default.kb_max_storage_bytes is None
    assert role_custom.kb_max_kb_count == 10
    assert role_custom.kb_max_docs_per_kb == 200
    assert role_custom.kb_max_storage_bytes == 2 * 1024**3
    assert role_custom.kb_max_file_size_bytes == 50 * 1024**2
    assert isinstance(role_custom.kb_max_storage_bytes, int)
