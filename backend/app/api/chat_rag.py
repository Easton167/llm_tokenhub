"""前台 Chat RAG 编排 API（JWT + 平台 API Key）。

``POST /chat/kb-completions``：检索 → 预算截断 → 提示词拼装 → 走现有 chat 流式链路；
流式结束后追加 citations 事件。不改动对外 ``/v1/chat/completions`` 协议。
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_user_context
from app.api.v1 import chat as v1_chat
from app.core.constants import (
    CHAT_MISS_STRATEGY_FALLBACK,
    RAG_MISS_REPLY,
)
from app.core.errors import ForbiddenError, InvalidRequestError, UnauthorizedError
from app.db.session import get_db
from app.schemas.auth import UserContext
from app.schemas.chat import KbCompletionsRequest, parse_kb_ids
from app.schemas.openai import ChatCompletionRequest, ChatMessage
from app.services.auth_service import auth_service
from app.services.chat_session_service import chat_session_service
from app.services.rag_chat_service import RagPlan, rag_chat_service

router = APIRouter(prefix="/chat", tags=["chat-rag"])


def _citations_payload(plan: RagPlan) -> dict[str, Any]:
    return {
        "object": "chat.completion.citations",
        "citations": [c.to_dict() for c in plan.citations],
        "rag": {
            "hit": plan.hit,
            "miss_strategy": plan.miss_strategy,
            "used_knowledge_base": plan.used_knowledge_base,
            "call_model": plan.call_model,
            "context_tokens": plan.context_tokens,
            "context_budget": plan.context_budget,
            "truncated_count": plan.truncated_count,
            "query_charge_amount": plan.query_charge_amount,
            "query_input_tokens": plan.query_input_tokens,
        },
    }


def _sse_data(payload: dict[str, Any] | str) -> str:
    if isinstance(payload, str):
        return f"data: {payload}\n\n"
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _openai_content_chunk(*, model: str, content: str, finish: bool = False) -> dict[str, Any]:
    chunk: dict[str, Any] = {
        "id": f"chatcmpl-rag-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": {"content": content} if content else {},
                "finish_reason": "stop" if finish else None,
            }
        ],
    }
    return chunk


async def _stream_strict_miss(*, model: str, plan: RagPlan) -> AsyncIterator[str]:
    text = plan.miss_reply or RAG_MISS_REPLY
    yield _sse_data(_openai_content_chunk(model=model, content=text))
    yield _sse_data(_openai_content_chunk(model=model, content="", finish=True))
    yield _sse_data(_citations_payload(plan))
    yield _sse_data("[DONE]")


async def _wrap_chat_stream_with_citations(
    upstream: AsyncIterator[str],
    plan: RagPlan,
) -> AsyncIterator[str]:
    citations_sent = False
    async for item in upstream:
        if "data: [DONE]" in item and not citations_sent:
            yield _sse_data(_citations_payload(plan))
            citations_sent = True
        yield item
    if not citations_sent:
        yield _sse_data(_citations_payload(plan))
        yield _sse_data("[DONE]")


@router.post("/kb-completions", response_model=None)
async def kb_completions(
    body: KbCompletionsRequest,
    request: Request,
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
    x_api_key: Annotated[str | None, Header(alias="X-Api-Key")] = None,
    x_billing_subject: Annotated[str | None, Header(alias="X-Billing-Subject")] = None,
) -> StreamingResponse:
    """RAG 编排 + 流式回答；需 JWT，调用模型时另需 ``X-Api-Key``（平台 sk-）。"""
    if not body.stream:
        raise InvalidRequestError("Only stream=true is supported for kb-completions")

    miss_strategy = body.miss_strategy or CHAT_MISS_STRATEGY_FALLBACK
    top_k = body.top_k
    score_threshold = body.score_threshold
    kb_ids: list[UUID] = list(body.kb_ids) if body.kb_ids is not None else []

    if body.session_id is not None:
        session = await chat_session_service.get_session(
            db, user_id=ctx.user_id, session_id=body.session_id
        )
        if body.kb_ids is None:
            kb_ids = parse_kb_ids(session.kb_ids)
        if body.miss_strategy is None:
            miss_strategy = session.miss_strategy or CHAT_MISS_STRATEGY_FALLBACK
        if body.top_k is None:
            top_k = session.retrieval_top_k
        if body.score_threshold is None:
            score_threshold = session.retrieval_score_threshold

    chat_messages = [
        ChatMessage(role=m.role, content=m.content) for m in body.messages
    ]
    model_ctx = await rag_chat_service.resolve_model_context_tokens(
        db, model_alias=body.model
    )
    plan = await rag_chat_service.prepare(
        db,
        user=ctx.user,
        messages=chat_messages,
        kb_ids=kb_ids,
        miss_strategy=miss_strategy,
        top_k=top_k,
        score_threshold=score_threshold,
        model_context_tokens=model_ctx,
    )
    await db.commit()

    headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }

    if not plan.call_model:
        return StreamingResponse(
            _stream_strict_miss(model=body.model, plan=plan),
            media_type="text/event-stream",
            headers=headers,
        )

    if not x_api_key:
        raise UnauthorizedError("X-Api-Key is required when calling the chat model")

    auth = await auth_service.verify_api_key(db, x_api_key)
    if auth.user_id != ctx.user_id:
        raise ForbiddenError("API key does not belong to the current user")

    chat_body = ChatCompletionRequest(
        model=body.model,
        messages=plan.messages,
        temperature=body.temperature,
        max_tokens=body.max_tokens,
        stream=True,
    )
    request_id = getattr(request.state, "request_id", None) or str(uuid.uuid4())
    prepared = await v1_chat._prepare_chat_context(
        db,
        chat_body,
        auth,
        request_id=request_id,
        header_subject=x_billing_subject,
    )
    upstream_resp = await v1_chat._chat_completions_stream(
        request, chat_body, auth, prepared, db
    )
    return StreamingResponse(
        _wrap_chat_stream_with_citations(upstream_resp.body_iterator, plan),
        media_type="text/event-stream",
        headers=headers,
    )
