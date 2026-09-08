"""上游厂商、渠道实例与加密存储的上游 API Key。"""

from uuid import UUID

from sqlalchemy import ForeignKey, Index, Integer, SmallInteger, String, Text
from sqlalchemy.dialects.postgresql import BYTEA, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Provider(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """LLM 厂商类型（如 OpenAI 兼容），一个 Provider 可对应多个 Channel。"""

    __tablename__ = "providers"

    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    adapter_type: Mapped[str] = mapped_column(String(64), nullable=False)
    default_base_url: Mapped[str | None] = mapped_column(String(512), nullable=True)

    channels: Mapped[list["Channel"]] = relationship(back_populates="provider")


class Channel(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """具体接入线路；创建时可继承 Provider 的 adapter_type / default_base_url。
    路由时按 priority 分组、组内按 weight 加权选择。
    """

    __tablename__ = "channels"
    __table_args__ = (
        Index("ix_channels_provider_id", "provider_id"),
        Index("ix_channels_status_health_priority", "status", "health_status", "priority"),
    )

    provider_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("providers.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    base_url: Mapped[str] = mapped_column(String(512), nullable=False)
    adapter_type: Mapped[str] = mapped_column(String(64), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # 越大越优先
    weight: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)  # 0 启用，1 禁用
    health_status: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)  # 健康/熔断/半开
    fail_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # 连续失败次数，用于熔断判断
    timeout_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=120_000)
    max_retries: Mapped[int] = mapped_column(Integer, nullable=False, default=2)

    provider: Mapped["Provider"] = relationship(back_populates="channels")
    channel_keys: Mapped[list["ChannelKey"]] = relationship(back_populates="channel")
    model_mappings: Mapped[list["ModelChannelMap"]] = relationship(back_populates="channel")
    request_logs: Mapped[list["RequestLog"]] = relationship(back_populates="channel")
    costs: Mapped[list["ChannelCost"]] = relationship(back_populates="channel")


class ChannelKey(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """渠道下的上游密钥池；同一渠道可多 Key 轮换与限流。"""

    __tablename__ = "channel_keys"

    channel_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("channels.id"), nullable=False
    )
    api_key_encrypted: Mapped[bytes] = mapped_column(BYTEA, nullable=False)  # AES-GCM 密文
    key_masked: Mapped[str] = mapped_column(String(64), nullable=False)  # 脱敏展示，通常保留后 4 位
    weight: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    rpm_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 每分钟请求上限
    tpm_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 每分钟 token 上限
    status: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)  # 0 启用，1 禁用
    health_status: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)

    channel: Mapped["Channel"] = relationship(back_populates="channel_keys")
