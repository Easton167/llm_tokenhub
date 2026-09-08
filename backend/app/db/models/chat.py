"""用户 Chat 会话与消息正文持久化。"""

from uuid import UUID

from sqlalchemy import CheckConstraint, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ChatSession(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """用户对话会话；消息按 seq 有序存储。"""

    __tablename__ = "chat_sessions"
    __table_args__ = (
        Index("ix_chat_sessions_user_updated", "user_id", "updated_at"),
        CheckConstraint(
            "miss_strategy IN ('strict', 'fallback')",
            name="ck_chat_sessions_miss_strategy",
        ),
    )

    user_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False, default="新对话")
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    temperature: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 会话挂载的知识库 ID 列表（UUID 字符串）；空数组 = 普通对话
    kb_ids: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    # strict=仅知识库；fallback=未命中允许模型自答
    miss_strategy: Mapped[str] = mapped_column(
        String(16), nullable=False, default="fallback"
    )
    # 检索参数覆盖；空则取挂载库默认（多库时取 max top_k / min threshold）
    retrieval_top_k: Mapped[int | None] = mapped_column(Integer, nullable=True)
    retrieval_score_threshold: Mapped[float | None] = mapped_column(Float, nullable=True)

    messages: Mapped[list["ChatSessionMessage"]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="ChatSessionMessage.seq",
    )


class ChatSessionMessage(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """会话内单条消息（user / assistant / system）。"""

    __tablename__ = "chat_session_messages"
    __table_args__ = (
        Index("ix_chat_session_messages_session_seq", "session_id", "seq"),
    )

    session_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("chat_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # RAG 引用来源快照（通常挂在 assistant 消息上）
    citations: Mapped[list | None] = mapped_column(JSONB, nullable=True)

    session: Mapped["ChatSession"] = relationship(back_populates="messages")
