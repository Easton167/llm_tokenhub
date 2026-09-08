"""计费主体决策：请求头 > Key 绑定 > 账户默认；含组织消费开关与额度耗尽回退。

本服务不扣费，只选出最终 Wallet。网关随后对这只钱包 hold。

优先级（select_candidate）：
  1. 请求头 X-Billing-Subject（显式）
  2. ApiKey.billing_subject（Key 绑定）
  3. User.default_billing_subject（账户默认，缺省 personal）

组织路径：
  - 显式指定但非成员 / 开关关闭 / 组织禁用 → 403，不回退（用户明确要组织钱包却没权）
  - 可消费但额度或余额不够 → 按 org.billing_fallback：personal 回退个人，deny 则 402
  - Key/账户默认指向组织但不可消费 → 同样走 fallback，不是 403
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    BILLING_FALLBACK_PERSONAL,
    BILLING_SUBJECT_PERSONAL,
    BILLING_SUBJECT_TYPE_ORG,
    BILLING_SUBJECT_TYPE_PERSONAL,
    STATUS_ENABLED,
)
from app.core.errors import (
    ForbiddenError,
    InsufficientBalanceError,
    InvalidRequestError,
)
from app.db.models.api_key import ApiKey
from app.db.models.org import Org, OrgMember
from app.db.models.user import User
from app.db.models.wallet import Wallet
from app.services.org_service import org_service
from app.services.quota_service import quota_service
from app.services.wallet_service import wallet_service


class SubjectSource(str, Enum):
    """候选主体来源，便于测试与日志。"""

    HEADER = "header"
    API_KEY = "api_key"
    ACCOUNT_DEFAULT = "account_default"


@dataclass(frozen=True, slots=True)
class BillingSubject:
    """供网关预扣使用的决策结果。

    subject_ref 写入 request_log.billing_subject，格式 personal:{user_id} 或 org:{org_id}。
    fell_back_to_personal 表示本意是组织但走了个人钱包，便于排查「为什么扣了个人」。
    """

    subject_type: str  # personal | org
    subject_ref: str  # 落库用：personal / personal:{user_id} / org:{org_id}
    wallet: Wallet
    org_id: UUID | None
    org_member: OrgMember | None  # 组织消费时的发起成员
    source: SubjectSource
    fell_back_to_personal: bool = False

    @property
    def actor_user_id(self) -> UUID | None:
        """组织扣费流水应记录的发起成员用户 ID。"""
        if self.org_member is not None:
            return self.org_member.user_id
        return None


def parse_billing_subject_ref(raw: str) -> tuple[str, UUID | None]:
    """解析计费主体字符串。

    支持：
    - personal
    - user:{uuid} / personal:{uuid}
    - org:{uuid}

    返回 (subject_type, id)；personal 无 id 时第二项为 None。
    """
    value = (raw or "").strip()
    if not value:
        raise InvalidRequestError("Billing subject must not be empty")

    lower = value.lower()
    if lower == BILLING_SUBJECT_PERSONAL:
        return BILLING_SUBJECT_TYPE_PERSONAL, None

    if ":" not in value:
        raise InvalidRequestError(
            "Invalid billing subject; expected personal, user:{id}, or org:{id}"
        )

    prefix, _, rest = value.partition(":")
    prefix = prefix.lower().strip()
    rest = rest.strip()
    if not rest:
        raise InvalidRequestError("Invalid billing subject: missing id")

    try:
        subject_id = UUID(rest)
    except ValueError as exc:
        raise InvalidRequestError("Invalid billing subject: id must be a UUID") from exc

    if prefix in ("user", "personal"):
        return BILLING_SUBJECT_TYPE_PERSONAL, subject_id
    if prefix == "org":
        return BILLING_SUBJECT_TYPE_ORG, subject_id

    raise InvalidRequestError(
        "Invalid billing subject; expected personal, user:{id}, or org:{id}"
    )


def format_personal_subject_ref(user_id: UUID) -> str:
    return f"personal:{user_id}"


def format_org_subject_ref(org_id: UUID) -> str:
    return f"org:{org_id}"


class BillingSubjectService:
    """扣费主体决策（不扣费；额度耗尽按组织回退策略处理；金额级校验见 quota_check）。"""

    def select_candidate(
        self,
        *,
        user: User,
        api_key: ApiKey,
        header_subject: str | None,
    ) -> tuple[str, SubjectSource]:
        """按优先级选出原始主体字符串与来源（尚未做成员/开关校验）。"""
        if header_subject is not None and header_subject.strip() != "":
            return header_subject.strip(), SubjectSource.HEADER

        key_bound = api_key.billing_subject
        if key_bound is not None and str(key_bound).strip() != "":
            return str(key_bound).strip(), SubjectSource.API_KEY

        account = (user.default_billing_subject or BILLING_SUBJECT_PERSONAL).strip()
        return account or BILLING_SUBJECT_PERSONAL, SubjectSource.ACCOUNT_DEFAULT

    async def resolve(
        self,
        session: AsyncSession,
        *,
        user: User,
        api_key: ApiKey,
        header_subject: str | None = None,
        estimated_amount: int | None = None,
    ) -> BillingSubject:
        """决策最终扣费钱包。

        规则摘要：
        - 显式指定（请求头）他人个人主体 → 403
        - 显式指定组织但非成员 / 消费开关关闭 → 403（不走回退）
        - 可消费但额度不足（含 estimated_amount）→ 按 org.billing_fallback 回退个人或 402
        - Key/账户默认指向组织但不可消费 → 按 org.billing_fallback 回退个人或 402

        estimated_amount：可选预估金额；未传时仅判断「额度是否已耗尽」（remaining>=1）。
        与组织余额取更严的金额校验由 quota_check 提供，供网关 hold 前调用。
        """
        raw, source = self.select_candidate(
            user=user, api_key=api_key, header_subject=header_subject
        )
        explicit = source == SubjectSource.HEADER
        subject_type, subject_id = parse_billing_subject_ref(raw)

        if subject_type == BILLING_SUBJECT_TYPE_PERSONAL:
            return await self._resolve_personal(
                session,
                user=user,
                subject_user_id=subject_id,
                source=source,
            )

        assert subject_id is not None
        return await self._resolve_org(
            session,
            user=user,
            org_id=subject_id,
            source=source,
            explicit=explicit,
            estimated_amount=estimated_amount,
        )

    async def _resolve_personal(
        self,
        session: AsyncSession,
        *,
        user: User,
        subject_user_id: UUID | None,
        source: SubjectSource,
    ) -> BillingSubject:
        if subject_user_id is not None and subject_user_id != user.id:
            raise ForbiddenError("Cannot bill another user's personal wallet")

        wallet = await wallet_service.get_or_create_personal_wallet(session, user.id)
        return BillingSubject(
            subject_type=BILLING_SUBJECT_TYPE_PERSONAL,
            subject_ref=format_personal_subject_ref(user.id),
            wallet=wallet,
            org_id=None,
            org_member=None,
            source=source,
            fell_back_to_personal=False,
        )

    async def _resolve_org(
        self,
        session: AsyncSession,
        *,
        user: User,
        org_id: UUID,
        source: SubjectSource,
        explicit: bool,
        estimated_amount: int | None = None,
    ) -> BillingSubject:
        org = await org_service.get_org(session, org_id)
        membership = None
        if org is not None:
            membership = await org_service.get_active_membership(
                session, org_id=org_id, user_id=user.id
            )

        can_consume = (
            org is not None
            and org.status == STATUS_ENABLED
            and membership is not None
            and membership.can_consume_org_wallet
        )

        if can_consume:
            assert org is not None and membership is not None
            # 未传 estimated_amount：仅判断额度是否已耗尽；
            # 传入时：与组织余额取更严（quota_check）
            if estimated_amount is None:
                quota_ok = await quota_service.is_quota_available(
                    session, org_member_id=membership.id, amount=1
                )
            else:
                check = await quota_service.quota_check(
                    session,
                    org_id=org.id,
                    org_member_id=membership.id,
                    amount=estimated_amount,
                )
                quota_ok = check.ok
            if quota_ok:
                wallet = await wallet_service.require_org_wallet(session, org.id)
                return BillingSubject(
                    subject_type=BILLING_SUBJECT_TYPE_ORG,
                    subject_ref=format_org_subject_ref(org.id),
                    wallet=wallet,
                    org_id=org.id,
                    org_member=membership,
                    source=source,
                    fell_back_to_personal=False,
                )
            # 开关已开但额度/余额不足：按组织回退策略（显式指定同样回退，非 403）
            return await self._fallback_or_deny(
                session,
                user=user,
                org=org,
                source=source,
                reason="member_quota_exhausted",
            )

        # 显式指定：无权一律 403，不回退
        if explicit:
            if org is None or membership is None:
                raise ForbiddenError("Not a member of this organization")
            if org.status != STATUS_ENABLED:
                raise ForbiddenError("Organization is disabled")
            raise ForbiddenError("Not allowed to consume organization wallet")

        # Key / 账户默认：按组织回退策略
        return await self._fallback_or_deny(
            session,
            user=user,
            org=org,
            source=source,
        )

    async def _fallback_or_deny(
        self,
        session: AsyncSession,
        *,
        user: User,
        org: Org | None,
        source: SubjectSource,
        reason: str | None = None,
    ) -> BillingSubject:
        fallback = (
            org.billing_fallback if org is not None else BILLING_FALLBACK_PERSONAL
        )
        # 组织不存在时无法读取策略：按个人回退，避免误拒已有个人路径
        if org is None:
            fallback = BILLING_FALLBACK_PERSONAL

        if fallback == BILLING_FALLBACK_PERSONAL:
            wallet = await wallet_service.get_or_create_personal_wallet(session, user.id)
            return BillingSubject(
                subject_type=BILLING_SUBJECT_TYPE_PERSONAL,
                subject_ref=format_personal_subject_ref(user.id),
                wallet=wallet,
                org_id=None,
                org_member=None,
                source=source,
                fell_back_to_personal=True,
            )

        message = "Organization billing unavailable and fallback is denied"
        if reason == "member_quota_exhausted":
            message = "Member quota exhausted and fallback is denied"
        raise InsufficientBalanceError(message)

    async def validate_key_billing_subject(
        self,
        session: AsyncSession,
        *,
        user_id: UUID,
        billing_subject: str | None,
    ) -> str | None:
        """校验并规范化 Key 绑定主体；None 表示未绑定（走账户默认）。"""
        if billing_subject is None:
            return None
        raw = billing_subject.strip()
        if raw == "":
            return None

        subject_type, subject_id = parse_billing_subject_ref(raw)
        if subject_type == BILLING_SUBJECT_TYPE_PERSONAL:
            if subject_id is not None and subject_id != user_id:
                raise ForbiddenError("Cannot bind another user's personal wallet")
            return BILLING_SUBJECT_PERSONAL

        assert subject_id is not None
        org, membership = await org_service.require_active_membership(
            session, org_id=subject_id, user_id=user_id
        )
        if org.status != STATUS_ENABLED:
            raise ForbiddenError("Organization is disabled")
        _ = membership
        return format_org_subject_ref(subject_id)


billing_subject_service = BillingSubjectService()
