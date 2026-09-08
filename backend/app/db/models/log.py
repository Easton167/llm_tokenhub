"""请求、用量与后台审计日志（只追加，无 updated_at）。"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, UUIDPrimaryKeyMixin


class RequestLog(Base, UUIDPrimaryKeyMixin):
    """每次 API 调用的请求级记录；request_id 为全链路唯一标识。"""

    __tablename__ = "request_logs"
    __table_args__ = (
        Index("ix_request_logs_created_at", "created_at"),
        Index("ix_request_logs_org_created", "org_id", "created_at"),
    )

    request_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    # 内部调用方（kb_ingest / kb_query）可不占用用户 API Key，故可空
    api_key_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("api_keys.id"), nullable=True
    )
    user_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    org_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("orgs.id"), nullable=True
    )
    actor_user_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    billing_subject: Mapped[str] = mapped_column(String(64), nullable=False)
    model_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("models.id"), nullable=False
    )
    channel_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("channels.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)  # success / failed / timeout 等
    http_status: Mapped[int] = mapped_column(Integer, nullable=False)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_stream: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 来源：chat / api / kb_ingest / kb_query；nullable + server default 兼容存量数据
    source: Mapped[str | None] = mapped_column(
        String(16), nullable=True, server_default="api"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    api_key: Mapped["ApiKey"] = relationship(back_populates="request_logs")
    user: Mapped["User"] = relationship(foreign_keys=[user_id])
    actor_user: Mapped["User | None"] = relationship(foreign_keys=[actor_user_id])
    model: Mapped["LLMModel"] = relationship(back_populates="request_logs")
    channel: Mapped["Channel | None"] = relationship(back_populates="request_logs")
    usage_log: Mapped["UsageLog | None"] = relationship(
        back_populates="request_log", uselist=False
    )


class UsageLog(Base, UUIDPrimaryKeyMixin):
    """与 RequestLog 1:1；记录 token 用量、计费规则版本及实际扣费金额。"""

    __tablename__ = "usage_logs"
    __table_args__ = (
        Index("ix_usage_logs_created_at", "created_at"),
        Index("ix_usage_logs_org_created", "org_id", "created_at"),
        Index("ix_usage_logs_source_created", "source", "created_at"),
    )

    request_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("request_logs.request_id"), unique=True, nullable=False
    )
    org_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("orgs.id"), nullable=True
    )
    actor_user_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cached_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    billing_rule_version: Mapped[int] = mapped_column(Integer, nullable=False)  # 计费时采用的规则版本
    cost_amount: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)  # 上游成本
    charge_amount: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)  # 向用户收取（总额）
    gift_charge_amount: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )  # 其中赠送池抵扣，不计入收入
    cash_charge_amount: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )  # 其中现金池支付，真实收入
    is_estimated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)  # 流式未完成时为估算值
    # 来源：chat / api / kb_ingest / kb_query；nullable + server default 兼容存量数据
    source: Mapped[str | None] = mapped_column(
        String(16), nullable=True, server_default="api"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    request_log: Mapped["RequestLog"] = relationship(back_populates="usage_log")


class AuditLog(Base, UUIDPrimaryKeyMixin):
    """后台管理操作的审计轨迹。"""

    __tablename__ = "audit_logs"
    __table_args__ = (Index("ix_audit_logs_created_at", "created_at"),)

    actor_user_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target: Mapped[str] = mapped_column(String(255), nullable=False)
    detail: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
