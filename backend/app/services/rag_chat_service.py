"""Chat RAG 编排：检索 → 上下文预算截断 → 提示词拼装。

位于前台 BFF 层（``POST /chat/kb-completions``），不改动对外 ``/v1/chat/completions`` 协议。
实际模型调用仍走现有 chat 链路；本模块只产出「是否调用模型 / 拼装后的 messages / 引用列表」。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    CHAT_MISS_STRATEGY_FALLBACK,
    CHAT_MISS_STRATEGY_STRICT,
    CHAT_MISS_STRATEGIES,
    KB_STATUS_ACTIVE,
    RAG_CITATION_SUMMARY_MAX_CHARS,
    RAG_CONTEXT_BUDGET_MAX_TOKENS,
    RAG_CONTEXT_BUDGET_RATIO,
    RAG_DEFAULT_MODEL_CONTEXT_TOKENS,
    RAG_MISS_REPLY,
)
from app.core.errors import ForbiddenError, InvalidRequestError, ResourceNotFoundError
from app.core.metrics import KB_RAG_HIT
from app.db.models.kb import KnowledgeBase
from app.db.models.llm_model import LLMModel
from app.db.models.user import User
from app.kb.rag_prompts import (
    RAG_FALLBACK_MISS_SYSTEM_PROMPT,
    RAG_HIT_SYSTEM_PROMPT,
    RAG_KNOWLEDGE_ITEM_TEMPLATE,
)
from app.kb.tokens import estimate_tokens
from app.schemas.openai import ChatMessage
from app.services.retrieval_service import RetrievedChunk, retrieval_service


@dataclass(frozen=True, slots=True)
class CitationSnapshot:
    chunk_id: UUID
    kb_id: UUID
    document_id: UUID
    document_name: str
    content_summary: str
    score: float
    index: int  # 1-based 引用编号，与提示词 [n] 对齐

    def to_dict(self) -> dict:
        return {
            "chunk_id": str(self.chunk_id),
            "kb_id": str(self.kb_id),
            "document_id": str(self.document_id),
            "document_name": self.document_name,
            "content_summary": self.content_summary,
            "score": self.score,
            "index": self.index,
        }


@dataclass(slots=True)
class RagPlan:
    """编排结果：供 BFF 决定流式固定文案或转发 chat 链路。"""

    messages: list[ChatMessage]
    citations: list[CitationSnapshot] = field(default_factory=list)
    hit: bool = False
    call_model: bool = True
    miss_strategy: str = CHAT_MISS_STRATEGY_FALLBACK
    miss_reply: str | None = None  # strict 未命中时的固定文案
    used_knowledge_base: bool = False
    context_tokens: int = 0
    context_budget: int = 0
    truncated_count: int = 0  # 因预算被舍弃的分块数
    query_charge_amount: int = 0
    query_input_tokens: int = 0


def compute_context_budget(model_context_tokens: int | None) -> int:
    """注入分块总 token ≤ min(模型上下文 × 40%, 4000)。"""
    ctx = model_context_tokens if model_context_tokens and model_context_tokens > 0 else (
        RAG_DEFAULT_MODEL_CONTEXT_TOKENS
    )
    return min(int(ctx * RAG_CONTEXT_BUDGET_RATIO), RAG_CONTEXT_BUDGET_MAX_TOKENS)


def apply_context_budget(
    chunks: list[RetrievedChunk],
    *,
    budget_tokens: int,
) -> tuple[list[RetrievedChunk], int, int]:
    """按分数优先截断（输入已按分数降序）；返回 (保留, 使用 token, 舍弃数)。"""
    if budget_tokens <= 0 or not chunks:
        return [], 0, len(chunks)

    kept: list[RetrievedChunk] = []
    used = 0
    for chunk in chunks:
        cost = chunk.token_count if chunk.token_count > 0 else estimate_tokens(chunk.content)
        if cost <= 0:
            cost = 1
        if used + cost > budget_tokens:
            break
        kept.append(chunk)
        used += cost
    return kept, used, len(chunks) - len(kept)


def summarize_content(content: str, *, max_chars: int = RAG_CITATION_SUMMARY_MAX_CHARS) -> str:
    text = (content or "").strip().replace("\n", " ")
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1] + "…"


def build_knowledge_block(chunks: list[RetrievedChunk]) -> str:
    parts: list[str] = []
    for i, c in enumerate(chunks, start=1):
        parts.append(
            RAG_KNOWLEDGE_ITEM_TEMPLATE.format(
                index=i,
                document_name=c.document_name,
                content=c.content.strip(),
            )
        )
    return "\n\n".join(parts)


def build_citations(chunks: list[RetrievedChunk]) -> list[CitationSnapshot]:
    return [
        CitationSnapshot(
            chunk_id=c.chunk_id,
            kb_id=c.kb_id,
            document_id=c.document_id,
            document_name=c.document_name,
            content_summary=summarize_content(c.content),
            score=round(float(c.score), 6),
            index=i,
        )
        for i, c in enumerate(chunks, start=1)
    ]


def _extract_latest_user_question(messages: list[ChatMessage]) -> str:
    for msg in reversed(messages):
        if msg.role == "user" and isinstance(msg.content, str) and msg.content.strip():
            return msg.content.strip()
    raise InvalidRequestError("messages must contain a non-empty user question")


def _history_without_trailing_user(messages: list[ChatMessage]) -> list[ChatMessage]:
    """拼装时把「最新用户问题」留给末尾；历史中去掉末尾连续 user。"""
    if not messages:
        return []
    hist = list(messages)
    while hist and hist[-1].role == "user":
        hist.pop()
    return hist


class RagChatService:
    async def resolve_owned_kb_ids(
        self,
        db: AsyncSession,
        *,
        user_id: UUID,
        kb_ids: list[UUID],
        require_active: bool = True,
    ) -> list[KnowledgeBase]:
        """校验归属；挂载时可选要求 active。含他人库 → 403。"""
        if not kb_ids:
            return []

        unique_ids = list(dict.fromkeys(kb_ids))
        stmt = select(KnowledgeBase).where(KnowledgeBase.id.in_(unique_ids))
        rows = list((await db.execute(stmt)).scalars().all())
        found = {kb.id: kb for kb in rows}

        ordered: list[KnowledgeBase] = []
        for kid in unique_ids:
            kb = found.get(kid)
            if kb is None:
                raise ResourceNotFoundError("Knowledge base not found")
            if kb.owner_user_id != user_id:
                raise ForbiddenError("You do not have access to this knowledge base")
            if require_active and kb.status != KB_STATUS_ACTIVE:
                raise InvalidRequestError("Knowledge base is not active")
            ordered.append(kb)
        return ordered

    def resolve_retrieval_params(
        self,
        kbs: list[KnowledgeBase],
        *,
        top_k_override: int | None,
        score_threshold_override: float | None,
    ) -> tuple[int, float]:
        if not kbs:
            return 5, 0.35
        default_top_k = max(kb.top_k for kb in kbs)
        default_threshold = min(kb.score_threshold for kb in kbs)
        top_k = top_k_override if top_k_override is not None else default_top_k
        threshold = (
            score_threshold_override
            if score_threshold_override is not None
            else default_threshold
        )
        if top_k < 1:
            raise InvalidRequestError("top_k must be >= 1")
        if threshold < 0.0 or threshold > 1.0:
            raise InvalidRequestError("score_threshold must be between 0 and 1")
        return top_k, threshold

    async def prepare(
        self,
        db: AsyncSession,
        *,
        user: User,
        messages: list[ChatMessage],
        kb_ids: list[UUID],
        miss_strategy: str = CHAT_MISS_STRATEGY_FALLBACK,
        top_k: int | None = None,
        score_threshold: float | None = None,
        model_context_tokens: int | None = None,
    ) -> RagPlan:
        """检索并拼装；未挂载知识库时原样透传 messages。"""
        if miss_strategy not in CHAT_MISS_STRATEGIES:
            raise InvalidRequestError("miss_strategy must be 'strict' or 'fallback'")

        if not kb_ids:
            return RagPlan(
                messages=list(messages),
                call_model=True,
                miss_strategy=miss_strategy,
                used_knowledge_base=False,
            )

        kbs = await self.resolve_owned_kb_ids(
            db, user_id=user.id, kb_ids=kb_ids, require_active=False
        )
        # 检索侧对 disabled 库静默跳过；若全部不可用则视为未命中
        active_kbs = [kb for kb in kbs if kb.status == KB_STATUS_ACTIVE]
        question = _extract_latest_user_question(messages)
        history = _history_without_trailing_user(messages)
        budget = compute_context_budget(model_context_tokens)

        if not active_kbs:
            plan = self._plan_miss(
                history=history,
                question=question,
                miss_strategy=miss_strategy,
                budget=budget,
            )
            KB_RAG_HIT.labels(result="miss").inc()
            return plan

        top_k_eff, threshold_eff = self.resolve_retrieval_params(
            active_kbs,
            top_k_override=top_k,
            score_threshold_override=score_threshold,
        )
        search = await retrieval_service.search(
            db,
            user=user,
            kb_ids=[kb.id for kb in active_kbs],
            query=question,
            top_k=top_k_eff,
            score_threshold=threshold_eff,
        )

        kept, used_tokens, truncated = apply_context_budget(
            search.chunks, budget_tokens=budget
        )
        if not kept:
            plan = self._plan_miss(
                history=history,
                question=question,
                miss_strategy=miss_strategy,
                budget=budget,
            )
            plan.query_charge_amount = search.charge_amount
            plan.query_input_tokens = search.input_tokens
            plan.truncated_count = truncated
            KB_RAG_HIT.labels(result="miss").inc()
            return plan

        knowledge = build_knowledge_block(kept)
        system_content = RAG_HIT_SYSTEM_PROMPT.format(knowledge_block=knowledge)
        out_messages = [
            ChatMessage(role="system", content=system_content),
            *history,
            ChatMessage(role="user", content=question),
        ]
        KB_RAG_HIT.labels(result="hit").inc()
        return RagPlan(
            messages=out_messages,
            citations=build_citations(kept),
            hit=True,
            call_model=True,
            miss_strategy=miss_strategy,
            used_knowledge_base=True,
            context_tokens=used_tokens,
            context_budget=budget,
            truncated_count=truncated,
            query_charge_amount=search.charge_amount,
            query_input_tokens=search.input_tokens,
        )

    def _plan_miss(
        self,
        *,
        history: list[ChatMessage],
        question: str,
        miss_strategy: str,
        budget: int,
    ) -> RagPlan:
        if miss_strategy == CHAT_MISS_STRATEGY_STRICT:
            return RagPlan(
                messages=[],
                citations=[],
                hit=False,
                call_model=False,
                miss_strategy=miss_strategy,
                miss_reply=RAG_MISS_REPLY,
                used_knowledge_base=False,
                context_budget=budget,
            )

        # fallback：普通对话 + 未引用标注
        out_messages = [
            ChatMessage(role="system", content=RAG_FALLBACK_MISS_SYSTEM_PROMPT),
            *history,
            ChatMessage(role="user", content=question),
        ]
        return RagPlan(
            messages=out_messages,
            citations=[],
            hit=False,
            call_model=True,
            miss_strategy=CHAT_MISS_STRATEGY_FALLBACK,
            used_knowledge_base=False,
            context_budget=budget,
        )

    async def resolve_model_context_tokens(
        self, db: AsyncSession, *, model_alias: str
    ) -> int | None:
        stmt = select(LLMModel).where(LLMModel.alias == model_alias)
        model = (await db.execute(stmt)).scalar_one_or_none()
        if model is None:
            return None
        caps = model.capabilities or {}
        raw = caps.get("max_context_tokens")
        if isinstance(raw, int) and raw > 0:
            return raw
        if isinstance(raw, float) and raw > 0:
            return int(raw)
        return None


rag_chat_service = RagChatService()
