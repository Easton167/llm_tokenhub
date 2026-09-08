"""渠道上游成本价（版本化），供日聚合毛利核算。"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ChannelCost(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """渠道 × 上游模型维度的成本单价；按 version / effective_at 版本化生效。

    cost_input / cost_output 为「每 1M token」的最小货币单位整数。
    """

    __tablename__ = "channel_costs"
    __table_args__ = (
        UniqueConstraint(
            "channel_id",
            "upstream_model_name",
            "version",
            name="uq_channel_costs_channel_upstream_version",
        ),
        Index(
            "ix_channel_costs_lookup",
            "channel_id",
            "upstream_model_name",
            "effective_at",
            "status",
        ),
    )

    channel_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("channels.id"), nullable=False
    )
    upstream_model_name: Mapped[str] = mapped_column(String(255), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    cost_input: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    cost_output: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    currency: Mapped[str] = mapped_column(String(16), nullable=False, default="CNY")
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)  # 0 启用，1 作废

    channel: Mapped["Channel"] = relationship(back_populates="costs")
