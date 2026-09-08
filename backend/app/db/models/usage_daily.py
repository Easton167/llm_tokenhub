"""日粒度用量预聚合表，供报表只读查询。"""

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UUIDPrimaryKeyMixin

# 可空维度在唯一索引中的占位（COALESCE），避免 PostgreSQL UNIQUE 对 NULL 不互斥
_NULL_DIM_UUID = "00000000-0000-0000-0000-000000000000"


class UsageDailySummary(Base, UUIDPrimaryKeyMixin):
    """按 天 × 模型 × 用户 × 组织 × 渠道 预聚合。

    收入 = 当日 capture 对应的计费金额；成本按 channel_costs 折算。
    未配置成本价时 cost_amount=0 且 cost_price_missing=True。
    重跑同一天：先删后插，保证幂等。
    """

    __tablename__ = "usage_daily_summary"
    __table_args__ = (
        Index(
            "uq_usage_daily_summary_dims",
            "summary_date",
            "model_id",
            "user_id",
            text(f"COALESCE(org_id, '{_NULL_DIM_UUID}'::uuid)"),
            text(f"COALESCE(channel_id, '{_NULL_DIM_UUID}'::uuid)"),
            unique=True,
        ),
        Index("ix_usage_daily_summary_date", "summary_date"),
        Index("ix_usage_daily_summary_org_date", "org_id", "summary_date"),
        Index("ix_usage_daily_summary_user_date", "user_id", "summary_date"),
        Index("ix_usage_daily_summary_model_date", "model_id", "summary_date"),
        Index("ix_usage_daily_summary_channel_date", "channel_id", "summary_date"),
    )

    summary_date: Mapped[date] = mapped_column(Date, nullable=False)
    model_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("models.id"), nullable=False
    )
    user_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    org_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("orgs.id"), nullable=True
    )
    channel_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("channels.id"), nullable=True
    )
    request_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cached_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    revenue_amount: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )  # 计费总额（含赠金）；平台收入看 cash_revenue_amount
    gift_revenue_amount: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )  # 赠金消耗（不计收入）
    cash_revenue_amount: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )  # 现金收入口径
    cost_amount: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    cost_price_missing: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
