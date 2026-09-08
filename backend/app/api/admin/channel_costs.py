"""渠道成本价管理 API（版本化，不覆盖历史版本）。"""

from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.admin.common import audit_write, get_or_404, list_all
from app.api.admin.deps import get_admin_actor_id
from app.core.constants import STATUS_DISABLED, STATUS_ENABLED
from app.core.errors import ResourceNotFoundError
from app.db.models.channel import Channel
from app.db.models.channel_cost import ChannelCost
from app.db.session import get_db
from app.schemas.admin import ChannelCostCreate, ChannelCostOut, ChannelCostUpdate

router = APIRouter(prefix="/channels/{channel_id}/costs", tags=["admin-channel-costs"])


@router.post("", response_model=ChannelCostOut, status_code=201)
async def create_channel_cost(
    channel_id: UUID,
    body: ChannelCostCreate,
    db: AsyncSession = Depends(get_db),
    actor_id: UUID | None = Depends(get_admin_actor_id),
) -> ChannelCost:
    await get_or_404(db, Channel, channel_id)
    max_version = await db.scalar(
        select(func.max(ChannelCost.version)).where(
            ChannelCost.channel_id == channel_id,
            ChannelCost.upstream_model_name == body.upstream_model_name,
        )
    )
    next_version = (max_version or 0) + 1
    cost = ChannelCost(
        channel_id=channel_id,
        version=next_version,
        status=STATUS_ENABLED,
        **body.model_dump(),
    )
    db.add(cost)
    await db.flush()
    await audit_write(
        db,
        action="channel_cost.create",
        target=f"channel_cost:{cost.id}",
        actor_user_id=actor_id,
        detail={
            "channel_id": str(channel_id),
            "upstream_model_name": body.upstream_model_name,
            "version": next_version,
        },
    )
    return cost


@router.get("", response_model=list[ChannelCostOut])
async def list_channel_costs(
    channel_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> list[ChannelCost]:
    await get_or_404(db, Channel, channel_id)
    return await list_all(
        db,
        select(ChannelCost)
        .where(ChannelCost.channel_id == channel_id)
        .order_by(
            ChannelCost.upstream_model_name,
            ChannelCost.version.desc(),
        ),
    )


@router.patch("/{cost_id}", response_model=ChannelCostOut)
async def void_channel_cost(
    channel_id: UUID,
    cost_id: UUID,
    body: ChannelCostUpdate,
    db: AsyncSession = Depends(get_db),
    actor_id: UUID | None = Depends(get_admin_actor_id),
) -> ChannelCost:
    cost = await get_or_404(db, ChannelCost, cost_id)
    if cost.channel_id != channel_id:
        raise ResourceNotFoundError("channel_cost not found")
    cost.status = body.status if body.status is not None else STATUS_DISABLED
    await db.flush()
    await db.refresh(cost)
    await audit_write(
        db,
        action="channel_cost.void",
        target=f"channel_cost:{cost.id}",
        actor_user_id=actor_id,
        detail={"status": cost.status},
    )
    return cost
