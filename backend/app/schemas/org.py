"""组织与成员 API 的请求/响应模型。"""

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.datetime_utils import UtcDateTime
from app.db.models.org import Org, OrgMember
from app.db.models.user import User

OrgRole = Literal["owner", "admin", "member"]
BillingFallback = Literal["personal", "deny"]
QuotaPeriod = Literal["none", "daily", "monthly"]


@dataclass(frozen=True, slots=True)
class OrgAccessContext:
    """组织内鉴权上下文：当前用户在指定组织的有效成员身份。"""

    user: User
    org: Org
    membership: OrgMember

    @property
    def user_id(self) -> UUID:
        return self.user.id

    @property
    def org_id(self) -> UUID:
        return self.org.id

    @property
    def org_role(self) -> str:
        return self.membership.org_role


class OrgCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    billing_fallback: BillingFallback = "deny"


class OrgUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    billing_fallback: BillingFallback | None = None


class OrgOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    owner_user_id: UUID
    status: int
    billing_fallback: str
    created_at: UtcDateTime
    updated_at: UtcDateTime


class OrgWithRoleOut(OrgOut):
    """当前用户视角下的组织，附带自身组织角色。"""

    my_org_role: OrgRole


class OrgMemberInvite(BaseModel):
    user_id: UUID
    org_role: OrgRole = "member"


class OrgMemberRoleUpdate(BaseModel):
    """兼容旧调用：仅改角色。"""

    org_role: OrgRole


class OrgMemberUpdate(BaseModel):
    """更新成员角色、消费开关与额度（至少提供一项）。"""

    org_role: OrgRole | None = None
    can_consume_org_wallet: bool | None = None
    quota_period: QuotaPeriod | None = None
    quota_limit: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _require_at_least_one_field(self) -> "OrgMemberUpdate":
        if (
            self.org_role is None
            and self.can_consume_org_wallet is None
            and self.quota_period is None
            and self.quota_limit is None
        ):
            raise ValueError("At least one field must be provided")
        return self


class MemberQuotaOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    quota_period: QuotaPeriod
    quota_limit: int
    quota_used: int
    period_reset_at: UtcDateTime | None = None


class OrgMemberOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    org_id: UUID
    user_id: UUID
    org_role: OrgRole
    can_consume_org_wallet: bool
    status: int
    created_at: UtcDateTime
    updated_at: UtcDateTime
    quota: MemberQuotaOut | None = None


class OrgDetailOut(OrgOut):
    """组织详情（含活跃成员列表）。"""

    members: list[OrgMemberOut] = Field(default_factory=list)


class AdminOrgDeactivate(BaseModel):
    """管理端停用组织（可选备注，写入审计 detail）。"""

    reason: str | None = Field(default=None, max_length=512)


_UNSET = object()


def org_member_to_out(
    member: OrgMember,
    *,
    quota: object = _UNSET,
) -> OrgMemberOut:
    """将成员 ORM 转为响应。

    quota 显式传入时不再访问关系（避免异步 MissingGreenlet）。
    未传 quota 时仅在关系已加载的情况下读取，否则为 None。
    """
    from app.db.models.org import MemberQuota as MemberQuotaModel

    quota_out: MemberQuotaOut | None = None
    if quota is not _UNSET:
        if isinstance(quota, MemberQuotaModel):
            quota_out = MemberQuotaOut.model_validate(quota)
    else:
        from sqlalchemy import inspect as sa_inspect

        state = sa_inspect(member)
        if "quota" not in state.unloaded:
            q = member.__dict__.get("quota")
            if isinstance(q, MemberQuotaModel):
                quota_out = MemberQuotaOut.model_validate(q)

    return OrgMemberOut(
        id=member.id,
        org_id=member.org_id,
        user_id=member.user_id,
        org_role=member.org_role,  # type: ignore[arg-type]
        can_consume_org_wallet=member.can_consume_org_wallet,
        status=member.status,
        created_at=member.created_at,
        updated_at=member.updated_at,
        quota=quota_out,
    )
