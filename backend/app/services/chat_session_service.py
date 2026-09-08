"""Chat 会话持久化：创建、列表、读写消息；知识库挂载校验。"""

from uuid import UUID

from app.core.datetime_utils import utc_now

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.core.constants import CHAT_MISS_STRATEGY_FALLBACK
from app.core.errors import ResourceNotFoundError
from app.db.models.chat import ChatSession, ChatSessionMessage
from app.schemas.chat import (
    ChatMessageIn,
    ChatSessionCreate,
    ChatSessionUpdate,
    kb_ids_to_storage,
    parse_kb_ids,
)
from app.services.rag_chat_service import rag_chat_service


class ChatSessionService:
    async def list_sessions(
        self, db: AsyncSession, *, user_id: UUID
    ) -> list[tuple[ChatSession, int]]:
        msg_count = (
            select(func.count(ChatSessionMessage.id))
            .where(ChatSessionMessage.session_id == ChatSession.id)
            .correlate(ChatSession)
            .scalar_subquery()
        )
        stmt = (
            select(ChatSession, msg_count.label("message_count"))
            .where(ChatSession.user_id == user_id)
            .order_by(ChatSession.updated_at.desc())
        )
        result = await db.execute(stmt)
        return [(row[0], int(row[1] or 0)) for row in result.all()]

    async def create_session(
        self, db: AsyncSession, *, user_id: UUID, body: ChatSessionCreate
    ) -> ChatSession:
        if body.kb_ids:
            await rag_chat_service.resolve_owned_kb_ids(
                db, user_id=user_id, kb_ids=body.kb_ids, require_active=True
            )
        session = ChatSession(
            user_id=user_id,
            title=body.title,
            model=body.model,
            temperature=body.temperature,
            max_tokens=body.max_tokens,
            kb_ids=kb_ids_to_storage(body.kb_ids),
            miss_strategy=body.miss_strategy or CHAT_MISS_STRATEGY_FALLBACK,
            retrieval_top_k=body.retrieval_top_k,
            retrieval_score_threshold=body.retrieval_score_threshold,
        )
        db.add(session)
        await db.flush()
        await db.refresh(session)
        return session

    async def get_session(
        self, db: AsyncSession, *, user_id: UUID, session_id: UUID
    ) -> ChatSession:
        stmt = (
            select(ChatSession)
            .where(ChatSession.id == session_id, ChatSession.user_id == user_id)
            .options(selectinload(ChatSession.messages))
        )
        result = await db.execute(stmt)
        session = result.scalar_one_or_none()
        if session is None:
            raise ResourceNotFoundError("Chat session not found")
        return session

    async def update_session(
        self,
        db: AsyncSession,
        *,
        user_id: UUID,
        session_id: UUID,
        body: ChatSessionUpdate,
    ) -> ChatSession:
        session = await self.get_session(db, user_id=user_id, session_id=session_id)
        data = body.model_dump(exclude_unset=True)
        if "kb_ids" in data:
            kb_ids: list[UUID] = data.pop("kb_ids") or []
            if kb_ids:
                await rag_chat_service.resolve_owned_kb_ids(
                    db, user_id=user_id, kb_ids=kb_ids, require_active=True
                )
            session.kb_ids = kb_ids_to_storage(kb_ids)
        for key, value in data.items():
            setattr(session, key, value)
        await db.flush()
        await db.refresh(session)
        return session

    async def delete_session(
        self, db: AsyncSession, *, user_id: UUID, session_id: UUID
    ) -> None:
        session = await self.get_session(db, user_id=user_id, session_id=session_id)
        await db.delete(session)
        await db.flush()

    async def replace_messages(
        self,
        db: AsyncSession,
        *,
        user_id: UUID,
        session_id: UUID,
        messages: list[ChatMessageIn],
        title: str | None = None,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> ChatSession:
        session = await self.get_session(db, user_id=user_id, session_id=session_id)

        # 清空旧消息后按序写入，保证多轮上下文与前端一致
        session.messages.clear()
        await db.flush()

        for idx, msg in enumerate(messages):
            citations = None
            if msg.citations is not None:
                citations = [c.model_dump(mode="json") for c in msg.citations]
            session.messages.append(
                ChatSessionMessage(
                    role=msg.role,
                    content=msg.content,
                    seq=idx,
                    citations=citations,
                )
            )

        if title is not None:
            session.title = title
        if model is not None:
            session.model = model
        if temperature is not None:
            session.temperature = temperature
        if max_tokens is not None:
            session.max_tokens = max_tokens
        session.updated_at = utc_now()

        await db.flush()
        await db.refresh(session)
        return session

    @staticmethod
    def session_kb_ids(session: ChatSession) -> list[UUID]:
        return parse_kb_ids(session.kb_ids)


chat_session_service = ChatSessionService()
