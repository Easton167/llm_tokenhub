"""组织、成员与成员额度（二期组织域）。"""

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.db.models.user import User


class Org(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """组织主体；每个组织至多一个钱包（wallets.owner_type='org'）。"""

    __tablename__ = "orgs"
    __table_args__ = (
        CheckConstraint(
            "billing_fallback IN ('personal', 'deny')",
            name="ck_orgs_billing_fallback",
        ),
        Index("ix_orgs_owner_user_id", "owner_user_id"),
        Index("ix_orgs_status", "status"),
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    owner_user_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    status: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)  # 0 正常，1 停用
    # 组织余额/额度不足时：personal=回退个人钱包；deny=402 拒绝
    billing_fallback: Mapped[str] = mapped_column(
        String(32), nullable=False, default="deny"
    )

    owner: Mapped["User"] = relationship(foreign_keys=[owner_user_id])
    members: Mapped[list["OrgMember"]] = relationship(back_populates="org")


class OrgMember(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """组织-用户成员关系；移除成员时建议改 status，保留历史流水关联。"""

    __tablename__ = "org_members"
    __table_args__ = (
        UniqueConstraint("org_id", "user_id", name="uq_org_members_org_user"),
        CheckConstraint(
            "org_role IN ('owner', 'admin', 'member')",
            name="ck_org_members_org_role",
        ),
        Index("ix_org_members_user_status", "user_id", "status"),
        Index("ix_org_members_org_status", "org_id", "status"),
    )

    org_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("orgs.id"), nullable=False
    )
    user_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    org_role: Mapped[str] = mapped_column(String(32), nullable=False, default="member")
    can_consume_org_wallet: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    status: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)  # 0 正常，1 停用

    org: Mapped["Org"] = relationship(back_populates="members")
    user: Mapped["User"] = relationship()
    quota: Mapped["MemberQuota | None"] = relationship(
        back_populates="org_member", uselist=False
    )


class MemberQuota(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """成员消费额度；金额均为最小货币单位整数（决策 D2）。"""

    __tablename__ = "member_quotas"
    __table_args__ = (
        UniqueConstraint("org_member_id", name="uq_member_quotas_org_member"),
        CheckConstraint(
            "quota_period IN ('none', 'daily', 'monthly')",
            name="ck_member_quotas_period",
        ),
        CheckConstraint("quota_limit >= 0", name="ck_member_quotas_limit_non_negative"),
        CheckConstraint("quota_used >= 0", name="ck_member_quotas_used_non_negative"),
    )

    org_member_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("org_members.id"), nullable=False
    )
    # none=总额度（不自动重置）；daily/monthly=周期额度
    quota_period: Mapped[str] = mapped_column(String(32), nullable=False, default="none")
    quota_limit: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    quota_used: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    period_reset_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    org_member: Mapped["OrgMember"] = relationship(back_populates="quota")
