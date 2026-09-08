"""组织 CRUD、成员邀请/移除与组织内角色管理。"""

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.constants import (
    BILLING_FALLBACK_DENY,
    ORG_ROLE_ADMIN,
    ORG_ROLE_MEMBER,
    ORG_ROLE_OWNER,
    STATUS_DISABLED,
    STATUS_ENABLED,
)
from app.core.errors import (
    ConflictError,
    ForbiddenError,
    InvalidRequestError,
    ResourceNotFoundError,
)
from app.db.models.org import Org, OrgMember
from app.db.models.user import User
from app.services.audit_service import record_audit
from app.services.wallet_service import wallet_service

# 组织内角色权重量：用于权限比较与「不可降到低于自身」等规则
_ORG_ROLE_RANK: dict[str, int] = {
    ORG_ROLE_MEMBER: 1,
    ORG_ROLE_ADMIN: 2,
    ORG_ROLE_OWNER: 3,
}


class OrgService:
    def role_rank(self, role: str) -> int:
        return _ORG_ROLE_RANK.get(role, 0)

    def ensure_org_writable(self, org: Org) -> None:
        if org.status != STATUS_ENABLED:
            raise ForbiddenError("Organization is disabled")

    async def get_org(self, session: AsyncSession, org_id: UUID) -> Org | None:
        return await session.get(Org, org_id)

    async def require_org(self, session: AsyncSession, org_id: UUID) -> Org:
        org = await self.get_org(session, org_id)
        if org is None:
            raise ResourceNotFoundError("Organization not found")
        return org

    async def get_active_membership(
        self,
        session: AsyncSession,
        *,
        org_id: UUID,
        user_id: UUID,
    ) -> OrgMember | None:
        stmt = select(OrgMember).where(
            OrgMember.org_id == org_id,
            OrgMember.user_id == user_id,
            OrgMember.status == STATUS_ENABLED,
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def require_active_membership(
        self,
        session: AsyncSession,
        *,
        org_id: UUID,
        user_id: UUID,
    ) -> tuple[Org, OrgMember]:
        org = await self.require_org(session, org_id)
        membership = await self.get_active_membership(
            session, org_id=org_id, user_id=user_id
        )
        if membership is None:
            raise ForbiddenError("Not a member of this organization")
        return org, membership

    async def require_min_role(
        self,
        session: AsyncSession,
        *,
        org_id: UUID,
        user_id: UUID,
        min_role: str,
    ) -> tuple[Org, OrgMember]:
        org, membership = await self.require_active_membership(
            session, org_id=org_id, user_id=user_id
        )
        if self.role_rank(membership.org_role) < self.role_rank(min_role):
            raise ForbiddenError("Insufficient organization role")
        return org, membership

    async def create_org(
        self,
        session: AsyncSession,
        *,
        name: str,
        owner_user_id: UUID,
        billing_fallback: str = BILLING_FALLBACK_DENY,
    ) -> Org:
        """创建组织：创建者成为 Owner，并自动初始化组织钱包。"""
        org = Org(
            name=name.strip(),
            owner_user_id=owner_user_id,
            status=STATUS_ENABLED,
            billing_fallback=billing_fallback,
        )
        session.add(org)
        await session.flush()

        member = OrgMember(
            org_id=org.id,
            user_id=owner_user_id,
            org_role=ORG_ROLE_OWNER,
            can_consume_org_wallet=False,
            status=STATUS_ENABLED,
        )
        session.add(member)
        await wallet_service.get_or_create_org_wallet(session, org.id)
        await session.flush()
        await session.refresh(org)
        return org

    async def update_org(
        self,
        session: AsyncSession,
        org: Org,
        *,
        name: str | None = None,
        billing_fallback: str | None = None,
    ) -> Org:
        self.ensure_org_writable(org)
        if name is not None:
            org.name = name.strip()
        if billing_fallback is not None:
            org.billing_fallback = billing_fallback
        await session.flush()
        await session.refresh(org)
        return org

    async def list_my_orgs(
        self, session: AsyncSession, user_id: UUID
    ) -> list[tuple[Org, OrgMember]]:
        stmt = (
            select(Org, OrgMember)
            .join(OrgMember, OrgMember.org_id == Org.id)
            .where(
                OrgMember.user_id == user_id,
                OrgMember.status == STATUS_ENABLED,
            )
            .order_by(Org.created_at.desc())
        )
        result = await session.execute(stmt)
        return list(result.all())

    async def list_orgs(
        self,
        session: AsyncSession,
        *,
        status: int | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Org]:
        stmt = select(Org).order_by(Org.created_at.desc()).limit(limit).offset(offset)
        if status is not None:
            stmt = (
                select(Org)
                .where(Org.status == status)
                .order_by(Org.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def deactivate_org(self, session: AsyncSession, org: Org) -> Org:
        if org.status == STATUS_DISABLED:
            return org
        org.status = STATUS_DISABLED
        await session.flush()
        await session.refresh(org)
        return org

    async def list_active_members(
        self, session: AsyncSession, org_id: UUID
    ) -> list[OrgMember]:
        stmt = (
            select(OrgMember)
            .where(
                OrgMember.org_id == org_id,
                OrgMember.status == STATUS_ENABLED,
            )
            .options(selectinload(OrgMember.quota))
            .order_by(OrgMember.created_at.asc())
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def _count_active_owners(
        self, session: AsyncSession, org_id: UUID
    ) -> int:
        stmt = select(func.count()).select_from(OrgMember).where(
            OrgMember.org_id == org_id,
            OrgMember.status == STATUS_ENABLED,
            OrgMember.org_role == ORG_ROLE_OWNER,
        )
        result = await session.execute(stmt)
        return int(result.scalar_one())

    async def _get_membership_row(
        self,
        session: AsyncSession,
        *,
        org_id: UUID,
        user_id: UUID,
    ) -> OrgMember | None:
        """含已停用成员行（用于重新邀请时复用唯一约束行）。"""
        stmt = select(OrgMember).where(
            OrgMember.org_id == org_id,
            OrgMember.user_id == user_id,
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def invite_member(
        self,
        session: AsyncSession,
        *,
        org: Org,
        actor_user_id: UUID,
        user_id: UUID,
        org_role: str = ORG_ROLE_MEMBER,
    ) -> OrgMember:
        self.ensure_org_writable(org)
        if org_role == ORG_ROLE_OWNER:
            raise InvalidRequestError("Cannot invite a second owner; use role update")

        user = await session.get(User, user_id)
        if user is None or user.status != STATUS_ENABLED:
            raise ResourceNotFoundError("User not found")

        existing = await self._get_membership_row(
            session, org_id=org.id, user_id=user_id
        )
        if existing is not None and existing.status == STATUS_ENABLED:
            raise ConflictError("User is already a member of this organization")

        if existing is not None:
            existing.status = STATUS_ENABLED
            existing.org_role = org_role
            member = existing
        else:
            member = OrgMember(
                org_id=org.id,
                user_id=user_id,
                org_role=org_role,
                can_consume_org_wallet=False,
                status=STATUS_ENABLED,
            )
            session.add(member)

        await session.flush()
        await session.refresh(member)
        await record_audit(
            session,
            action="org.member.invite",
            target=f"org:{org.id}:member:{member.id}",
            actor_user_id=actor_user_id,
            detail={
                "user_id": str(user_id),
                "org_role": org_role,
            },
        )
        return member

    async def remove_member(
        self,
        session: AsyncSession,
        *,
        org: Org,
        actor_user_id: UUID,
        member_id: UUID,
    ) -> None:
        """软移除成员（status=disabled），保留历史流水关联。"""
        self.ensure_org_writable(org)
        member = await session.get(OrgMember, member_id)
        if member is None or member.org_id != org.id:
            raise ResourceNotFoundError("Organization member not found")
        if member.status == STATUS_DISABLED:
            return

        if member.org_role == ORG_ROLE_OWNER:
            owners = await self._count_active_owners(session, org.id)
            if owners <= 1:
                raise InvalidRequestError("Cannot remove the last owner")

        member.status = STATUS_DISABLED
        await session.flush()
        await record_audit(
            session,
            action="org.member.remove",
            target=f"org:{org.id}:member:{member.id}",
            actor_user_id=actor_user_id,
            detail={"user_id": str(member.user_id), "org_role": member.org_role},
        )

    async def update_member_role(
        self,
        session: AsyncSession,
        *,
        org: Org,
        actor_user_id: UUID,
        member_id: UUID,
        new_role: str,
        actor_role: str,
    ) -> OrgMember:
        self.ensure_org_writable(org)
        member = await session.get(OrgMember, member_id)
        if member is None or member.org_id != org.id or member.status != STATUS_ENABLED:
            raise ResourceNotFoundError("Organization member not found")

        old_role = member.org_role
        if old_role == new_role:
            return member

        # 仅 Owner 可授予/剥夺 Owner
        if ORG_ROLE_OWNER in (old_role, new_role) and actor_role != ORG_ROLE_OWNER:
            raise ForbiddenError("Only owner can change owner role")

        if old_role == ORG_ROLE_OWNER and new_role != ORG_ROLE_OWNER:
            owners = await self._count_active_owners(session, org.id)
            if owners <= 1:
                raise InvalidRequestError("Cannot demote the last owner")

        member.org_role = new_role

        # 角色变为 Owner 时同步 orgs.owner_user_id（可有多个 owner 行，主字段指向其一）
        if new_role == ORG_ROLE_OWNER:
            org.owner_user_id = member.user_id
        elif (
            old_role == ORG_ROLE_OWNER
            and org.owner_user_id == member.user_id
            and new_role != ORG_ROLE_OWNER
        ):
            # 原主 Owner 被降级：指向另一活跃 Owner
            stmt = (
                select(OrgMember)
                .where(
                    OrgMember.org_id == org.id,
                    OrgMember.status == STATUS_ENABLED,
                    OrgMember.org_role == ORG_ROLE_OWNER,
                    OrgMember.id != member.id,
                )
                .limit(1)
            )
            result = await session.execute(stmt)
            other = result.scalar_one_or_none()
            if other is not None:
                org.owner_user_id = other.user_id

        await session.flush()
        await session.refresh(member)
        await record_audit(
            session,
            action="org.member.role_update",
            target=f"org:{org.id}:member:{member.id}",
            actor_user_id=actor_user_id,
            detail={
                "user_id": str(member.user_id),
                "old_role": old_role,
                "new_role": new_role,
            },
        )
        return member


org_service = OrgService()
