"""模型计费规则（带版本）与生效时间。"""

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, Numeric, SmallInteger, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class BillingRule(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """按模型版本化定价；同一 model_id 可有多个 version，按 effective_at 选取生效规则。

    网关在一次请求内锁定本行（_PreparedChatContext.billing_rule），
    结算不得重新查询「当前最新规则」，否则预扣与实扣可能用了两个版本。
    price_* 是对用户售价，cost_* 是上游成本，单位都是「每 1M token 的最小货币单位」。
    """

    __tablename__ = "billing_rules"
    __table_args__ = (
        UniqueConstraint("model_id", "version", name="uq_billing_rules_model_version"),
        Index("ix_billing_rules_model_effective", "model_id", "effective_at", "status"),
    )

    model_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("models.id"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    # 以下 price_* / cost_* 均为「每 1M token」的最小货币单位整数
    price_input: Mapped[int] = mapped_column(BigInteger, nullable=False)
    price_output: Mapped[int] = mapped_column(BigInteger, nullable=False)
    price_cached_input: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    price_per_call: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    cost_input: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)  # 上游成本价
    cost_output: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    multiplier: Mapped[Decimal] = mapped_column(Numeric(8, 3), nullable=False, default=1)  # 对用户售价倍率
    min_charge_unit: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)  # 最小计费粒度
    currency: Mapped[str] = mapped_column(String(16), nullable=False, default="CNY")
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)  # 0 启用，1 禁用

    model: Mapped["LLMModel"] = relationship(back_populates="billing_rules")
