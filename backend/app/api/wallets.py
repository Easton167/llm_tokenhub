"""用户端钱包 API：查看个人钱包与流水（JWT）。"""

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_user_context
from app.db.models.wallet import GiftGrant, Wallet, WalletTransaction
from app.db.session import get_db
from app.schemas.admin import GiftGrantPublicOut, WalletOut, WalletTransactionOut
from app.schemas.auth import UserContext
from app.services.gift_service import GRANT_STATUS_ACTIVE, gift_service
from app.services.wallet_service import wallet_service

router = APIRouter(prefix="/wallets", tags=["wallets"])


@router.get("/me", response_model=WalletOut)
async def get_my_wallet(
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> Wallet:
    """获取当前用户的个人钱包（若不存在则初始化 balance=0 的钱包）。"""
    return await wallet_service.get_or_create_personal_wallet(db, ctx.user_id)


@router.get("/me/gifts", response_model=list[GiftGrantPublicOut])
async def get_my_gifts(
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> list[GiftGrant]:
    """当前用户个人钱包的 active 赠送批次（不暴露操作人）。"""
    wallet = await wallet_service.get_or_create_personal_wallet(db, ctx.user_id)
    return await gift_service.list_grants(
        db, wallet_id=wallet.id, status=GRANT_STATUS_ACTIVE, limit=200, offset=0
    )


@router.get("/me/transactions", response_model=list[WalletTransactionOut])
async def get_my_transactions(
    tx_type: str | None = Query(
        default=None, description="流水类型: hold/capture/release/recharge/adjust"
    ),
    ref_request_id: str | None = Query(default=None, description="关联请求 ID"),
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> list[WalletTransaction]:
    """查询当前用户的钱包流水（按时间倒序）。"""
    wallet = await wallet_service.get_or_create_personal_wallet(db, ctx.user_id)
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
