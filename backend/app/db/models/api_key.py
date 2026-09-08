"""平台 API Key：仅存哈希与前缀，明文不落库。"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Index, Integer, SmallInteger, String
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ApiKey(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "api_keys"
    __table_args__ = (Index("ix_api_keys_user_id_status", "user_id", "status"),)

    user_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    key_hash: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)  # pepper + 明文 Key 的哈希
    key_prefix: Mapped[str] = mapped_column(String(32), nullable=False)  # 列表展示用，如 sk-abc1...
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # NULL = 未绑定，决策时回退到 users.default_billing_subject
    billing_subject: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 空列表 = 继承角色授权；非空 = 与角色授权取交集（模型 alias）
    allowed_models: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    rpm_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 每分钟请求上限
    tpm_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 每分钟 token 上限
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)  # 0 启用，1 禁用
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    user: Mapped["User"] = relationship(back_populates="api_keys")
    request_logs: Mapped[list["RequestLog"]] = relationship(back_populates="api_key")
