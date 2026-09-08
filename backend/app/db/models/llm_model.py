"""对外模型别名与到上游渠道的映射关系。"""

from uuid import UUID

from sqlalchemy import ForeignKey, Integer, SmallInteger, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class LLMModel(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """对外暴露的模型；alias 即客户端请求的 model 名称。"""

    __tablename__ = "models"

    alias: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    model_type: Mapped[str] = mapped_column(String(64), nullable=False, default="chat")  # chat/embedding/...
    capabilities: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)  # 流式、函数调用、上下文上限等
    visibility: Mapped[str] = mapped_column(String(64), nullable=False, default="public")  # public / role_restricted
    status: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)  # 0 启用，1 禁用

    role_grants: Mapped[list["RoleModelGrant"]] = relationship(back_populates="model")
    channel_mappings: Mapped[list["ModelChannelMap"]] = relationship(back_populates="model")
    billing_rules: Mapped[list["BillingRule"]] = relationship(back_populates="model")
    request_logs: Mapped[list["RequestLog"]] = relationship(back_populates="model")


class ModelChannelMap(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """将对外 alias 路由到具体渠道及上游真实模型名；同映射可多权重负载均衡。"""

    __tablename__ = "model_channel_map"
    __table_args__ = (
        UniqueConstraint(
            "model_id",
            "channel_id",
            "upstream_model_name",
            name="uq_model_channel_map",
        ),
    )

    model_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("models.id"), nullable=False
    )
    channel_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("channels.id"), nullable=False
    )
    upstream_model_name: Mapped[str] = mapped_column(String(255), nullable=False)  # 上游 API 实际 model 参数
    weight: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)  # 0 启用，1 禁用

    model: Mapped["LLMModel"] = relationship(back_populates="channel_mappings")
    channel: Mapped["Channel"] = relationship(back_populates="model_mappings")
