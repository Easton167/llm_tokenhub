"""Channel 与 Channel Key 管理 API。"""

from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.admin.common import audit_write, get_or_404, list_all
from app.api.admin.deps import get_admin_actor_id
from app.core.channel_key_crypto import encrypt_channel_key, mask_channel_key
from app.core.constants import STATUS_ENABLED
from app.core.errors import InvalidRequestError
from app.db.models.channel import Channel, ChannelKey, Provider
from app.db.session import get_db
from app.schemas.admin import (
    ChannelCreate,
    ChannelKeyCreate,
    ChannelKeyOut,
    ChannelKeyUpdate,
    ChannelOut,
    ChannelTestOut,
    ChannelUpdate,
)

router = APIRouter(prefix="/channels", tags=["admin-channels"])


@router.post("", response_model=ChannelOut, status_code=201)
async def create_channel(
    body: ChannelCreate,
    db: AsyncSession = Depends(get_db),
    actor_id: UUID | None = Depends(get_admin_actor_id),
) -> Channel:
    provider = await get_or_404(db, Provider, body.provider_id)
    payload = body.model_dump(exclude_none=True)
    payload["adapter_type"] = payload.get("adapter_type") or provider.adapter_type
    payload["base_url"] = payload.get("base_url") or provider.default_base_url
    if not payload.get("base_url"):
        raise InvalidRequestError(
            "base_url is required when the provider has no default_base_url"
        )
    channel = Channel(**payload)
    db.add(channel)
    await db.flush()
    await audit_write(
        db,
        action="channel.create",
        target=f"channel:{channel.id}",
        actor_user_id=actor_id,
        detail={
            **body.model_dump(mode="json"),
            "adapter_type": channel.adapter_type,
            "base_url": channel.base_url,
        },
    )
    return channel


@router.get("", response_model=list[ChannelOut])
async def list_channels(db: AsyncSession = Depends(get_db)) -> list[Channel]:
    return await list_all(db, select(Channel).order_by(Channel.priority.desc(), Channel.name))


@router.get("/{channel_id}", response_model=ChannelOut)
async def get_channel(channel_id: UUID, db: AsyncSession = Depends(get_db)) -> Channel:
    return await get_or_404(db, Channel, channel_id)


@router.patch("/{channel_id}", response_model=ChannelOut)
async def update_channel(
    channel_id: UUID,
    body: ChannelUpdate,
    db: AsyncSession = Depends(get_db),
    actor_id: UUID | None = Depends(get_admin_actor_id),
) -> ChannelOut:
    channel = await get_or_404(db, Channel, channel_id)
    for key, value in body.model_dump(exclude_unset=True).items():
        setattr(channel, key, value)
    await db.flush()
    await audit_write(
        db,
        action="channel.update",
        target=f"channel:{channel.id}",
        actor_user_id=actor_id,
        detail=body.model_dump(exclude_unset=True),
    )
    # audit flush 后 updated_at 仍可能因 onupdate 过期；在异步上下文中 refresh 并转为 schema
    await db.refresh(channel)
    return ChannelOut.model_validate(channel)


@router.delete("/{channel_id}", status_code=204)
async def delete_channel(
    channel_id: UUID,
    db: AsyncSession = Depends(get_db),
    actor_id: UUID | None = Depends(get_admin_actor_id),
) -> None:
    channel = await get_or_404(db, Channel, channel_id)
    keys = await list_all(db, select(ChannelKey).where(ChannelKey.channel_id == channel_id))
    for key in keys:
        await db.delete(key)
    await db.delete(channel)
    await audit_write(
        db,
        action="channel.delete",
        target=f"channel:{channel_id}",
        actor_user_id=actor_id,
    )


@router.post("/{channel_id}/test", response_model=ChannelTestOut)
async def test_channel(channel_id: UUID, db: AsyncSession = Depends(get_db)) -> ChannelTestOut:
    await get_or_404(db, Channel, channel_id)
    return ChannelTestOut(
        status="pending",
        message="Upstream connectivity test will be implemented in the routing task",
    )


@router.post("/{channel_id}/keys", response_model=ChannelKeyOut, status_code=201)
async def create_channel_key(
    channel_id: UUID,
    body: ChannelKeyCreate,
    db: AsyncSession = Depends(get_db),
    actor_id: UUID | None = Depends(get_admin_actor_id),
) -> ChannelKey:
    await get_or_404(db, Channel, channel_id)
    channel_key = ChannelKey(
        channel_id=channel_id,
        api_key_encrypted=encrypt_channel_key(body.api_key),
        key_masked=mask_channel_key(body.api_key),
        weight=body.weight,
        rpm_limit=body.rpm_limit,
        tpm_limit=body.tpm_limit,
        status=STATUS_ENABLED,
    )
    db.add(channel_key)
    await db.flush()
    await audit_write(
        db,
        action="channel_key.create",
        target=f"channel_key:{channel_key.id}",
        actor_user_id=actor_id,
        detail={"channel_id": str(channel_id), "key_masked": channel_key.key_masked},
    )
    return channel_key


@router.get("/{channel_id}/keys", response_model=list[ChannelKeyOut])
async def list_channel_keys(
    channel_id: UUID, db: AsyncSession = Depends(get_db)
) -> list[ChannelKey]:
    await get_or_404(db, Channel, channel_id)
    return await list_all(
        db,
        select(ChannelKey)
        .where(ChannelKey.channel_id == channel_id)
        .order_by(ChannelKey.created_at),
    )


@router.patch("/{channel_id}/keys/{key_id}", response_model=ChannelKeyOut)
async def update_channel_key(
    channel_id: UUID,
    key_id: UUID,
    body: ChannelKeyUpdate,
    db: AsyncSession = Depends(get_db),
    actor_id: UUID | None = Depends(get_admin_actor_id),
) -> ChannelKey:
    channel_key = await get_or_404(db, ChannelKey, key_id)
    if channel_key.channel_id != channel_id:
        from app.core.errors import ResourceNotFoundError

        raise ResourceNotFoundError("channel_key not found")
    for key, value in body.model_dump(exclude_unset=True).items():
        setattr(channel_key, key, value)
    await db.flush()
    await db.refresh(channel_key)
    await audit_write(
        db,
        action="channel_key.update",
        target=f"channel_key:{channel_key.id}",
        actor_user_id=actor_id,
        detail=body.model_dump(exclude_unset=True),
    )
    return channel_key


@router.delete("/{channel_id}/keys/{key_id}", status_code=204)
async def delete_channel_key(
    channel_id: UUID,
    key_id: UUID,
    db: AsyncSession = Depends(get_db),
    actor_id: UUID | None = Depends(get_admin_actor_id),
) -> None:
    channel_key = await get_or_404(db, ChannelKey, key_id)
    if channel_key.channel_id != channel_id:
        from app.core.errors import ResourceNotFoundError

        raise ResourceNotFoundError("channel_key not found")
    await db.delete(channel_key)
    await audit_write(
        db,
        action="channel_key.delete",
        target=f"channel_key:{key_id}",
        actor_user_id=actor_id,
    )
