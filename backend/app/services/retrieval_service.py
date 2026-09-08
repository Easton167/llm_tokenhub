"""知识库向量检索服务：单库 / 多库合并、阈值过滤、归属隔离。

query 经内部 embedding 链路向量化（source=kb_query，计费主体为查询用户）；
分块侧只返回启用且已向量化、所属文档 ready/partial、所属库 active 的结果。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.constants import (
    KB_CHUNK_EMBED_SUCCESS,
    KB_DOC_STATUS_PARTIAL,
    KB_DOC_STATUS_READY,
    KB_STATUS_ACTIVE,
    USAGE_SOURCE_KB_QUERY,
)
from app.core.errors import ForbiddenError, InvalidRequestError, ResourceNotFoundError
from app.core.metrics import KB_RETRIEVAL_LATENCY
from app.db.models.kb import KbChunk, KbDocument, KnowledgeBase
from app.db.models.user import User
from app.services.embedding_gateway import InternalCaller, internal_embedding_service


@dataclass(frozen=True, slots=True)
class RetrievedChunk:
    chunk_id: UUID
    kb_id: UUID
    document_id: UUID
    document_name: str
    content: str
    score: float
    seq: int
    source_location: dict | None
    token_count: int


@dataclass(slots=True)
class RetrievalSearchResult:
    """检索结果 + query 向量化计费摘要（命中测试 / Chat 编排复用）。"""

    chunks: list[RetrievedChunk]
    charge_amount: int = 0
    input_tokens: int = 0
    request_ids: list[str] = field(default_factory=list)


class RetrievalService:
    async def search(
        self,
        db: AsyncSession,
        *,
        user: User,
        kb_ids: list[UUID],
        query: str,
        top_k: int,
        score_threshold: float,
    ) -> RetrievalSearchResult:
        """跨知识库向量检索；先做归属校验，再向量化与检索。

        kb_ids 含他人库直接 403，且不产生任何向量化调用。
        """
        q = (query or "").strip()
        if not q:
            raise InvalidRequestError("query must not be empty")
        if top_k < 1:
            raise InvalidRequestError("top_k must be >= 1")
        if score_threshold < 0.0 or score_threshold > 1.0:
            raise InvalidRequestError("score_threshold must be between 0 and 1")

        started = time.perf_counter()
        try:
            return await self._search_impl(
                db,
                user=user,
                kb_ids=kb_ids,
                query=q,
                top_k=top_k,
                score_threshold=score_threshold,
            )
        finally:
            KB_RETRIEVAL_LATENCY.observe(time.perf_counter() - started)

    async def _search_impl(
        self,
        db: AsyncSession,
        *,
        user: User,
        kb_ids: list[UUID],
        query: str,
        top_k: int,
        score_threshold: float,
    ) -> RetrievalSearchResult:
        kbs = await self._require_owned_kbs(db, user_id=user.id, kb_ids=kb_ids)
        # 仅检索 active 库；disabled 库静默跳过（不报错、不参与）
        active_kbs = [kb for kb in kbs if kb.status == KB_STATUS_ACTIVE]
        if not active_kbs:
            return RetrievalSearchResult(chunks=[])

        # 按 embedding 模型分组，同模型只向量化一次
        by_model: dict[UUID, list[KnowledgeBase]] = {}
        for kb in active_kbs:
            by_model.setdefault(kb.embedding_model_id, []).append(kb)

        caller = InternalCaller(user=user, source=USAGE_SOURCE_KB_QUERY)
        merged: list[RetrievedChunk] = []
        total_charge = 0
        total_tokens = 0
        request_ids: list[str] = []

        for model_kbs in by_model.values():
            model = model_kbs[0].embedding_model
            if model is None:
                raise InvalidRequestError("Knowledge base embedding model is missing")
            expected_dim = model_kbs[0].embedding_dim
            embed = await internal_embedding_service.embed_texts(
                db,
                caller=caller,
                model_alias=model.alias,
                texts=[query],
                settle=True,
                expected_dim=expected_dim,
            )
            total_charge += embed.charge_amount
            total_tokens += embed.input_tokens
            request_ids.append(embed.request_id)
            query_vec = embed.embeddings[0]

            for kb in model_kbs:
                hits = await self._search_one_kb(
                    db,
                    kb_id=kb.id,
                    query_embedding=query_vec,
                    top_k=top_k,
                    score_threshold=score_threshold,
                )
                merged.extend(hits)

        merged.sort(key=lambda c: (-c.score, str(c.chunk_id)))
        return RetrievalSearchResult(
            chunks=merged[:top_k],
            charge_amount=total_charge,
            input_tokens=total_tokens,
            request_ids=request_ids,
        )

    async def _require_owned_kbs(
        self,
        db: AsyncSession,
        *,
        user_id: UUID,
        kb_ids: list[UUID],
    ) -> list[KnowledgeBase]:
        if not kb_ids:
            raise InvalidRequestError("kb_ids must not be empty")

        unique_ids = list(dict.fromkeys(kb_ids))
        stmt = (
            select(KnowledgeBase)
            .where(KnowledgeBase.id.in_(unique_ids))
            .options(selectinload(KnowledgeBase.embedding_model))
        )
        rows = list((await db.execute(stmt)).scalars().all())
        found = {kb.id: kb for kb in rows}

        ordered: list[KnowledgeBase] = []
        for kid in unique_ids:
            kb = found.get(kid)
            if kb is None:
                raise ResourceNotFoundError("Knowledge base not found")
            if kb.owner_user_id != user_id:
                raise ForbiddenError("You do not have access to this knowledge base")
            ordered.append(kb)
        return ordered

    async def _search_one_kb(
        self,
        db: AsyncSession,
        *,
        kb_id: UUID,
        query_embedding: list[float],
        top_k: int,
        score_threshold: float,
    ) -> list[RetrievedChunk]:
        """单库 cosine 相似度检索；score = 1 - cosine_distance。"""
        distance_expr = KbChunk.embedding.cosine_distance(query_embedding)
        # 相似度阈值：score >= threshold ⇔ distance <= 1 - threshold
        max_distance = 1.0 - score_threshold

        stmt = (
            select(
                KbChunk,
                KbDocument.file_name,
                distance_expr.label("distance"),
            )
            .join(KbDocument, KbDocument.id == KbChunk.document_id)
            .where(
                KbChunk.kb_id == kb_id,
                KbChunk.is_enabled.is_(True),
                KbChunk.embedding_status == KB_CHUNK_EMBED_SUCCESS,
                KbChunk.embedding.is_not(None),
                KbDocument.status.in_((KB_DOC_STATUS_READY, KB_DOC_STATUS_PARTIAL)),
                distance_expr <= max_distance,
            )
            .order_by(distance_expr.asc())
            .limit(top_k)
        )
        rows = (await db.execute(stmt)).all()

        results: list[RetrievedChunk] = []
        for chunk, file_name, distance in rows:
            score = 1.0 - float(distance)
            results.append(
                RetrievedChunk(
                    chunk_id=chunk.id,
                    kb_id=chunk.kb_id,
                    document_id=chunk.document_id,
                    document_name=file_name,
                    content=chunk.content,
                    score=score,
                    seq=chunk.seq,
                    source_location=chunk.source_location,
                    token_count=chunk.token_count,
                )
            )
        return results


retrieval_service = RetrievalService()
