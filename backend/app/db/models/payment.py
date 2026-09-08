"""支付单：在线充值订单与渠道回调原文。"""

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.db.models.user import User
    from app.db.models.wallet import Wallet


class PaymentOrder(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """支付单；入账幂等键为 order_no（写入 wallet_transactions.ref_request_id）。"""

    __tablename__ = "payment_orders"
    __table_args__ = (
        CheckConstraint(
            "status IN ('created', 'paid', 'expired', 'failed')",
            name="ck_payment_orders_status",
        ),
        CheckConstraint(
            "target_type IN ('personal', 'org')",
            name="ck_payment_orders_target_type",
        ),
        CheckConstraint("amount > 0", name="ck_payment_orders_amount_positive"),
        Index("ix_payment_orders_user_created", "user_id", "created_at"),
        Index("ix_payment_orders_status_created", "status", "created_at"),
        Index("ix_payment_orders_wallet_created", "target_wallet_id", "created_at"),
        Index("ix_payment_orders_org_created", "org_id", "created_at"),
        Index("ix_payment_orders_external_order_no", "external_order_no"),
    )

    order_no: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    user_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    target_wallet_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("wallets.id"), nullable=False
    )
    # personal=个人钱包；org=组织钱包（此时 org_id 必填，应用层校验）
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    org_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("orgs.id"), nullable=True
    )
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(16), nullable=False, default="CNY")
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="created")
    external_order_no: Mapped[str | None] = mapped_column(String(128), nullable=True)
    callback_payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fail_reason: Mapped[str | None] = mapped_column(String(512), nullable=True)

    user: Mapped["User"] = relationship(foreign_keys=[user_id])
    target_wallet: Mapped["Wallet"] = relationship(foreign_keys=[target_wallet_id])
