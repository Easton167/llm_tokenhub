"""用户端 Chat 会话 API（JWT 鉴权，正文落库）。"""

from uuid import UUID

from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_user_context
from app.db.session import get_db
from app.schemas.auth import UserContext
from app.schemas.chat import (
    ChatMessagesReplace,
    ChatSessionCreate,
    ChatSessionDetailOut,
    ChatSessionSummaryOut,
    ChatSessionUpdate,
    parse_kb_ids,
)
from app.services.chat_session_service import chat_session_service

router = APIRouter(prefix="/chat/sessions", tags=["chat-sessions"])


def _session_to_summary(session, message_count: int) -> ChatSessionSummaryOut:
    return ChatSessionSummaryOut(
        id=session.id,
        title=session.title,
        model=session.model,
        temperature=session.temperature,
        max_tokens=session.max_tokens,
        kb_ids=parse_kb_ids(session.kb_ids),
        miss_strategy=session.miss_strategy,
        retrieval_top_k=session.retrieval_top_k,
        retrieval_score_threshold=session.retrieval_score_threshold,
        message_count=message_count,
        created_at=session.created_at,
        updated_at=session.updated_at,
    )


def _session_to_detail(session) -> ChatSessionDetailOut:
    # model_validate 对 kb_ids JSON 字符串列表需规范化
    data = ChatSessionDetailOut.model_validate(session)
    return data.model_copy(update={"kb_ids": parse_kb_ids(session.kb_ids)})


@router.get("", response_model=list[ChatSessionSummaryOut])
async def list_sessions(
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> list[ChatSessionSummaryOut]:
    rows = await chat_session_service.list_sessions(db, user_id=ctx.user_id)
    return [_session_to_summary(session, count) for session, count in rows]


@router.post("", response_model=ChatSessionDetailOut, status_code=201)
async def create_session(
    body: ChatSessionCreate,
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> ChatSessionDetailOut:
    session = await chat_session_service.create_session(
        db, user_id=ctx.user_id, body=body
    )
    return ChatSessionDetailOut(
        id=session.id,
        title=session.title,
        model=session.model,
        temperature=session.temperature,
        max_tokens=session.max_tokens,
        kb_ids=parse_kb_ids(session.kb_ids),
        miss_strategy=session.miss_strategy,
        retrieval_top_k=session.retrieval_top_k,
        retrieval_score_threshold=session.retrieval_score_threshold,
        messages=[],
        created_at=session.created_at,
        updated_at=session.updated_at,
    )


@router.get("/{session_id}", response_model=ChatSessionDetailOut)
async def get_session(
    session_id: UUID,
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> ChatSessionDetailOut:
    session = await chat_session_service.get_session(
        db, user_id=ctx.user_id, session_id=session_id
    )
    return _session_to_detail(session)


@router.patch("/{session_id}", response_model=ChatSessionDetailOut)
async def update_session(
    session_id: UUID,
    body: ChatSessionUpdate,
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> ChatSessionDetailOut:
    session = await chat_session_service.update_session(
        db, user_id=ctx.user_id, session_id=session_id, body=body
    )
    # update 后 messages 可能未加载，再取一次带消息的详情
    session = await chat_session_service.get_session(
        db, user_id=ctx.user_id, session_id=session.id
    )
    return _session_to_detail(session)


@router.delete("/{session_id}", status_code=204)
async def delete_session(
    session_id: UUID,
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> Response:
    await chat_session_service.delete_session(
        db, user_id=ctx.user_id, session_id=session_id
    )
    return Response(status_code=204)


@router.put("/{session_id}/messages", response_model=ChatSessionDetailOut)
async def replace_messages(
    session_id: UUID,
    body: ChatMessagesReplace,
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> ChatSessionDetailOut:
    session = await chat_session_service.replace_messages(
        db,
        user_id=ctx.user_id,
        session_id=session_id,
        messages=body.messages,
        title=body.title,
        model=body.model,
        temperature=body.temperature,
        max_tokens=body.max_tokens,
    )
    return _session_to_detail(session)
