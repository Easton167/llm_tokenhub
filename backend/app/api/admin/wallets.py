"""后台钱包管理 API：充值、调账、查询个人/组织钱包与流水。"""

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.admin.common import audit_write, get_or_404
from app.api.admin.deps import get_admin_actor_id
from app.core.config import settings
from app.core.constants import STATUS_ENABLED
from app.core.datetime_utils import (
    business_day_bounds_utc,
    business_day_end_utc,
    to_utc_aware,
    to_utc_iso,
)
from app.core.errors import GiftAmountExceedsLimitError
from app.core.logging import get_logger
from app.db.models.org import Org
from app.db.models.user import User
from app.db.models.wallet import GiftGrant, Wallet, WalletTransaction
from app.db.session import get_db
from app.schemas.admin import (
    GiftGrantCreate,
    GiftGrantOut,
    GiftRevoke,
    WalletAdjust,
    WalletOut,
    WalletRecharge,
    WalletTransactionOut,
)
from app.services.gift_service import gift_service
from app.services.user_service import user_service
from app.services.wallet_service import wallet_service

logger = get_logger(__name__)

# admin_users 不能写入 granted_by（FK → users）；env 兜底超管和库内管理员都落到系统操作人
SYSTEM_OPERATOR_EMAIL = "system-gift-operator@internal.local"

router = APIRouter(prefix="/wallets", tags=["admin-wallets"])
gift_ops_router = APIRouter(prefix="/wallets", tags=["admin-wallets"])
gifts_router = APIRouter(prefix="/gifts", tags=["admin-wallets"])


async def _get_user_wallet(db: AsyncSession, user_id: UUID) -> Wallet:
    """查找用户的个人钱包，不存在时自动创建。"""
    await get_or_404(db, User, user_id)
    return await wallet_service.get_or_create_personal_wallet(db, user_id)


async def _get_org_wallet(db: AsyncSession, org_id: UUID) -> Wallet:
    """查找组织钱包，不存在时自动创建。"""
    await get_or_404(db, Org, org_id)
    return await wallet_service.get_or_create_org_wallet(db, org_id)


@router.get("/users/{user_id}", response_model=WalletOut)
async def get_user_wallet(
    user_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> Wallet:
    """查看指定用户的个人钱包。"""
    return await _get_user_wallet(db, user_id)


@router.post("/users/{user_id}/recharge", response_model=WalletOut, status_code=200)
async def recharge_wallet(
    user_id: UUID,
    body: WalletRecharge,
    db: AsyncSession = Depends(get_db),
    actor_id: UUID | None = Depends(get_admin_actor_id),
) -> Wallet:
    """后台给用户钱包充值（仅追加正向流水）。"""
    wallet = await _get_user_wallet(db, user_id)
    result = await wallet_service.recharge(
        db,
        wallet_id=wallet.id,
        amount=body.amount,
        operator_user_id=actor_id,
        remark=body.remark,
    )
    await audit_write(
        db,
        action="wallet.recharge",
        target=f"wallet:{wallet.id}",
        actor_user_id=actor_id,
        detail={"user_id": str(user_id), "amount": body.amount, "remark": body.remark},
    )
    return result


@router.post("/users/{user_id}/adjust", response_model=WalletOut, status_code=200)
async def adjust_wallet(
    user_id: UUID,
    body: WalletAdjust,
    db: AsyncSession = Depends(get_db),
    actor_id: UUID | None = Depends(get_admin_actor_id),
) -> Wallet:
    """后台调账：amount 正数为增加，负数为减少（不能使余额变为负数）。"""
    wallet = await _get_user_wallet(db, user_id)
    result = await wallet_service.adjust(
        db,
        wallet_id=wallet.id,
        amount=body.amount,
        operator_user_id=actor_id,
        remark=body.remark,
    )
    await audit_write(
        db,
        action="wallet.adjust",
        target=f"wallet:{wallet.id}",
        actor_user_id=actor_id,
        detail={"user_id": str(user_id), "amount": body.amount, "remark": body.remark},
    )
    return result


@router.get("/users/{user_id}/transactions", response_model=list[WalletTransactionOut])
async def list_user_transactions(
    user_id: UUID,
    tx_type: str | None = Query(default=None, description="流水类型过滤: hold/capture/release/recharge/adjust"),
    ref_request_id: str | None = Query(default=None),
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> list[WalletTransaction]:
    """查询用户钱包流水（管理后台视角）。"""
    wallet = await _get_user_wallet(db, user_id)
    return await wallet_service.get_transactions(
        db,
        wallet_id=wallet.id,
        tx_type=tx_type,
        ref_request_id=ref_request_id,
        from_time=from_time,
        to_time=to_time,
        limit=limit,
        offset=offset,
    )


@router.get("/orgs/{org_id}", response_model=WalletOut)
async def get_org_wallet(
    org_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> Wallet:
    """查看指定组织的钱包。"""
    return await _get_org_wallet(db, org_id)


@router.post("/orgs/{org_id}/recharge", response_model=WalletOut, status_code=200)
async def recharge_org_wallet(
    org_id: UUID,
    body: WalletRecharge,
    db: AsyncSession = Depends(get_db),
    actor_id: UUID | None = Depends(get_admin_actor_id),
) -> Wallet:
    """后台给组织钱包充值。"""
    wallet = await _get_org_wallet(db, org_id)
    result = await wallet_service.recharge(
        db,
        wallet_id=wallet.id,
        amount=body.amount,
        operator_user_id=actor_id,
        remark=body.remark,
    )
    await audit_write(
        db,
        action="wallet.org.recharge",
        target=f"wallet:{wallet.id}",
        actor_user_id=actor_id,
        detail={"org_id": str(org_id), "amount": body.amount, "remark": body.remark},
    )
    return result


@router.post("/orgs/{org_id}/adjust", response_model=WalletOut, status_code=200)
async def adjust_org_wallet(
    org_id: UUID,
    body: WalletAdjust,
    db: AsyncSession = Depends(get_db),
    actor_id: UUID | None = Depends(get_admin_actor_id),
) -> Wallet:
    """后台调账组织钱包：amount 正数为增加，负数为减少。"""
    wallet = await _get_org_wallet(db, org_id)
    result = await wallet_service.adjust(
        db,
        wallet_id=wallet.id,
        amount=body.amount,
        operator_user_id=actor_id,
        remark=body.remark,
    )
    await audit_write(
        db,
        action="wallet.org.adjust",
        target=f"wallet:{wallet.id}",
        actor_user_id=actor_id,
        detail={"org_id": str(org_id), "amount": body.amount, "remark": body.remark},
    )
    return result


@router.get("/orgs/{org_id}/transactions", response_model=list[WalletTransactionOut])
async def list_org_transactions(
    org_id: UUID,
    tx_type: str | None = Query(default=None, description="流水类型过滤: hold/capture/release/recharge/adjust"),
    ref_request_id: str | None = Query(default=None),
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> list[WalletTransaction]:
    """查询组织钱包流水（管理后台视角）。"""
    wallet = await _get_org_wallet(db, org_id)
    return await wallet_service.get_transactions(
        db,
        wallet_id=wallet.id,
        tx_type=tx_type,
        ref_request_id=ref_request_id,
        from_time=from_time,
        to_time=to_time,
        limit=limit,
        offset=offset,
    )


@router.get("/{wallet_id}/transactions", response_model=list[WalletTransactionOut])
async def list_wallet_transactions(
    wallet_id: UUID,
    tx_type: str | None = Query(default=None),
    ref_request_id: str | None = Query(default=None),
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> list[WalletTransaction]:
    """按 wallet_id 直接查询流水（管理后台用）。"""
    await get_or_404(db, Wallet, wallet_id)
    return await wallet_service.get_transactions(
        db,
        wallet_id=wallet_id,
        tx_type=tx_type,
        ref_request_id=ref_request_id,
        from_time=from_time,
        to_time=to_time,
        limit=limit,
        offset=offset,
    )


async def _resolve_operator_id(db: AsyncSession, actor_id: UUID | None) -> UUID:
    """granted_by 必须是 users.id；后台管理员落到系统操作人。"""
    if actor_id is not None:
        return actor_id
    user = await user_service.get_by_email(db, SYSTEM_OPERATOR_EMAIL)
    if user is not None:
        return user.id
    user = User(
        email=SYSTEM_OPERATOR_EMAIL,
        nickname="系统赠送操作人",
        status=STATUS_ENABLED,
    )
    db.add(user)
    await db.flush()
    return user.id


def _resolve_expires_at(body: GiftGrantCreate) -> datetime | None:
    if body.never_expires:
        return None
    if body.expires_at is not None:
        return to_utc_aware(body.expires_at)
    assert body.expires_in_days is not None
    return business_day_end_utc(
        body.expires_in_days, tz_name=settings.business_timezone
    )


def _check_per_grant_limit(amount: int) -> None:
    limit = settings.gift_max_amount_per_grant
    if amount > limit:
        logger.warning(
            "gift_amount_exceeds_limit",
            kind="per_grant",
            amount=amount,
            limit=limit,
        )
        raise GiftAmountExceedsLimitError(
            f"Gift amount {amount} exceeds per-grant limit {limit}"
        )


async def _check_daily_limit(
    db: AsyncSession, operator_id: UUID, amount: int
) -> None:
    start, end = business_day_bounds_utc(tz_name=settings.business_timezone)
    used = await gift_service.sum_granted_by_operator(
        db, granted_by=operator_id, from_time=start, to_time=end
    )
    limit = settings.gift_max_amount_per_operator_daily
    if used + amount > limit:
        logger.warning(
            "gift_amount_exceeds_limit",
            kind="daily",
            amount=amount,
            used=used,
            limit=limit,
            operator_id=str(operator_id),
        )
        raise GiftAmountExceedsLimitError(
            f"Gift amount {amount} exceeds operator daily limit "
            f"(used={used}, limit={limit})"
        )


async def _grant_gift(
    db: AsyncSession,
    *,
    wallet: Wallet,
    body: GiftGrantCreate,
    actor_id: UUID | None,
) -> GiftGrant:
    existing = await gift_service.get_by_idempotency(
        db, wallet_id=wallet.id, idempotency_key=body.idempotency_key
    )
    if existing is not None:
        return existing

    operator_id = await _resolve_operator_id(db, actor_id)
    _check_per_grant_limit(body.amount)
    await _check_daily_limit(db, operator_id, body.amount)

    expires_at = _resolve_expires_at(body)
    grant = await gift_service.grant(
        db,
        wallet_id=wallet.id,
        amount=body.amount,
        reason=body.reason,
        expires_at=expires_at,
        idempotency_key=body.idempotency_key,
        granted_by=operator_id,
        remark=body.remark,
    )
    await audit_write(
        db,
        action="wallet.gift_grant",
        target=f"gift_grant:{grant.id}",
        actor_user_id=actor_id,
        detail={
            "wallet_id": str(wallet.id),
            "amount": body.amount,
            "reason": body.reason,
            "expires_at": to_utc_iso(expires_at) if expires_at else None,
            "grant_id": str(grant.id),
        },
    )
    return grant


@gift_ops_router.post("/users/{user_id}/gifts", response_model=GiftGrantOut)
async def grant_user_gift(
    user_id: UUID,
    body: GiftGrantCreate,
    db: AsyncSession = Depends(get_db),
    actor_id: UUID | None = Depends(get_admin_actor_id),
) -> GiftGrant:
    """向用户个人钱包发放赠送余额。"""
    wallet = await _get_user_wallet(db, user_id)
    return await _grant_gift(db, wallet=wallet, body=body, actor_id=actor_id)


@gift_ops_router.post("/orgs/{org_id}/gifts", response_model=GiftGrantOut)
async def grant_org_gift(
    org_id: UUID,
    body: GiftGrantCreate,
    db: AsyncSession = Depends(get_db),
    actor_id: UUID | None = Depends(get_admin_actor_id),
) -> GiftGrant:
    """向组织钱包发放赠送余额。"""
    wallet = await _get_org_wallet(db, org_id)
    return await _grant_gift(db, wallet=wallet, body=body, actor_id=actor_id)


@gift_ops_router.get("/{wallet_id}/gifts", response_model=list[GiftGrantOut])
async def list_wallet_gifts(
    wallet_id: UUID,
    status: str | None = Query(default=None, description="批次状态: active/used_up/expired/revoked"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> list[GiftGrant]:
    """按钱包查询赠送批次（分页 + 状态过滤）。"""
    await get_or_404(db, Wallet, wallet_id)
    return await gift_service.list_grants(
        db, wallet_id=wallet_id, status=status, limit=limit, offset=offset
    )


@gifts_router.post("/{grant_id}/revoke", response_model=GiftGrantOut)
async def revoke_gift(
    grant_id: UUID,
    body: GiftRevoke = GiftRevoke(),
    db: AsyncSession = Depends(get_db),
    actor_id: UUID | None = Depends(get_admin_actor_id),
) -> GiftGrant:
    """撤回批次未消耗剩余；已耗尽返回 409 gift_already_consumed。"""
    grant = await get_or_404(db, GiftGrant, grant_id)
    remaining = grant.remaining
    operator_id = await _resolve_operator_id(db, actor_id)
    revoked = await gift_service.revoke(
        db, grant_id=grant_id, revoked_by=operator_id, remark=body.remark
    )
    await audit_write(
        db,
        action="wallet.gift_revoke",
        target=f"gift_grant:{revoked.id}",
        actor_user_id=actor_id,
        detail={
            "wallet_id": str(revoked.wallet_id),
            "amount": remaining,
            "reason": revoked.reason,
            "expires_at": to_utc_iso(revoked.expires_at) if revoked.expires_at else None,
            "grant_id": str(revoked.id),
        },
    )
    return revoked

