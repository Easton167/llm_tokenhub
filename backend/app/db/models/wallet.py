"""钱包余额、预扣款、赠送批次与只追加的资金流水。"""

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
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.db.models.user import User


class Wallet(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """个人或组织钱包；金额字段均为最小货币单位的整数（见 settings.money_scale）。

    两资金池必须在同一把 FOR UPDATE 行锁内变更（见 WalletService）：
    - 现金池 balance / held：可退可提，充值与调账进入这里
    - 赠送池 gift_balance / gift_held：不可退不可提，消费 FIFO 核销 gift_grants

    可用额 = gift_balance + balance + credit_limit（预扣判定）。
    owner_id 按 owner_type 多态指向 users.id 或 orgs.id，不在库层建 FK。
    """

    __tablename__ = "wallets"
    __table_args__ = (
        UniqueConstraint("owner_type", "owner_id", name="uq_wallets_owner"),
        CheckConstraint(
            "owner_type IN ('personal', 'org')",
            name="ck_wallets_owner_type",
        ),
        CheckConstraint("balance >= 0", name="ck_wallets_balance_non_negative"),
        CheckConstraint("held >= 0", name="ck_wallets_held_non_negative"),
        CheckConstraint("gift_balance >= 0", name="ck_wallets_gift_balance_non_negative"),
        CheckConstraint("gift_held >= 0", name="ck_wallets_gift_held_non_negative"),
    )

    owner_type: Mapped[str] = mapped_column(String(32), nullable=False, default="personal")
    owner_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    # 现金池：充值与调账所得，可退可提。语义已收窄，不再表示「全部余额」。
    balance: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)  # 现金池可用余额
    held: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)  # 现金池预扣中金额
    gift_balance: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )  # 赠送池可用余额，不可退不可提
    gift_held: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )  # 赠送池预扣中金额
    currency: Mapped[str] = mapped_column(String(16), nullable=False, default="CNY")
    credit_limit: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)  # 允许透支额度

    user: Mapped["User | None"] = relationship(
        back_populates="wallet",
        primaryjoin="and_(foreign(Wallet.owner_id)==User.id, Wallet.owner_type=='personal')",
        uselist=False,
        viewonly=True,
    )
    transactions: Mapped[list["WalletTransaction"]] = relationship(back_populates="wallet")
    gift_grants: Mapped[list["GiftGrant"]] = relationship(back_populates="wallet")


class WalletTransaction(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """资金流水，只追加不修改；同一请求的扣款/退款通过部分唯一索引保证幂等。"""

    __tablename__ = "wallet_transactions"
    __table_args__ = (
        Index("ix_wallet_transactions_wallet_created", "wallet_id", "created_at"),
        Index("ix_wallet_transactions_ref_request_id", "ref_request_id"),
        Index("ix_wallet_transactions_org_created", "org_id", "created_at"),
        # ref_request_id 非空时，(wallet, request, type) 组合唯一，防止重复扣款
        Index(
            "uq_wallet_transactions_request_type",
            "wallet_id",
            "ref_request_id",
            "type",
            unique=True,
            postgresql_where=text("ref_request_id IS NOT NULL"),
        ),
    )

    wallet_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("wallets.id"), nullable=False
    )
    # hold / capture / release / recharge / adjust / gift_grant / gift_revoke / gift_expire
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)  # 正为入账，负为出账
    gift_amount: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )  # 其中赠送池部分，符号与 amount 一致；纯现金为 0
    balance_after: Mapped[int] = mapped_column(BigInteger, nullable=False)  # 变动后现金池余额
    held_after: Mapped[int] = mapped_column(BigInteger, nullable=False)  # 变动后现金池冻结
    gift_balance_after: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )  # 变动后赠送池余额
    gift_held_after: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )  # 变动后赠送池冻结
    gift_grant_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("gift_grants.id"), nullable=True
    )
    ref_request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)  # 关联 request_logs.request_id
    org_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("orgs.id"), nullable=True
    )
    operator_user_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    remark: Mapped[str | None] = mapped_column(Text, nullable=True)

    wallet: Mapped["Wallet"] = relationship(back_populates="transactions")


class GiftGrant(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """赠送批次账本：一次发放一条记录，按 expires_at FIFO 消耗。

    expires_at 为 NULL 表示永久有效。
    不变式（对账任务校验）：Σ(status=active 的 remaining) == wallet.gift_balance + wallet.gift_held。
    """

    __tablename__ = "gift_grants"
    __table_args__ = (
        CheckConstraint(
            "remaining >= 0 AND remaining <= amount",
            name="ck_gift_grants_remaining_range",
        ),
        CheckConstraint(
            "reason IN ('promotion', 'compensation', 'internal', 'partnership', 'other')",
            name="ck_gift_grants_reason",
        ),
        CheckConstraint(
            "status IN ('active', 'used_up', 'expired', 'revoked')",
            name="ck_gift_grants_status",
        ),
        UniqueConstraint(
            "wallet_id", "idempotency_key", name="uq_gift_grants_wallet_idempotency"
        ),
        Index("ix_gift_grants_wallet_status_expires", "wallet_id", "status", "expires_at"),
    )

    wallet_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("wallets.id"), nullable=False
    )
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    remaining: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reason: Mapped[str] = mapped_column(String(32), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )  # NULL = 永久有效
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    granted_by: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    revoked_by: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    remark: Mapped[str | None] = mapped_column(Text, nullable=True)

    wallet: Mapped["Wallet"] = relationship(back_populates="gift_grants")
