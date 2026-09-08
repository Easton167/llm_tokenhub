"""Provider 管理 API。"""

from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.admin.common import audit_write, get_or_404, handle_integrity_error, list_all
from app.api.admin.deps import get_admin_actor_id
from app.core.errors import InvalidRequestError
from app.db.models.channel import Channel, Provider
from app.db.session import get_db
from app.schemas.admin import ProviderCreate, ProviderOut, ProviderUpdate

router = APIRouter(prefix="/providers", tags=["admin-providers"])


@router.post("", response_model=ProviderOut, status_code=201)
async def create_provider(
    body: ProviderCreate,
    db: AsyncSession = Depends(get_db),
    actor_id: UUID | None = Depends(get_admin_actor_id),
) -> Provider:
    provider = Provider(**body.model_dump())
    db.add(provider)
    try:
        await db.flush()
    except IntegrityError as exc:
        handle_integrity_error(exc, field="name")
    await audit_write(
        db,
        action="provider.create",
        target=f"provider:{provider.id}",
        actor_user_id=actor_id,
        detail=body.model_dump(),
    )
    return provider


@router.get("", response_model=list[ProviderOut])
async def list_providers(db: AsyncSession = Depends(get_db)) -> list[Provider]:
    return await list_all(db, select(Provider).order_by(Provider.name))


@router.get("/{provider_id}", response_model=ProviderOut)
async def get_provider(
    provider_id: UUID, db: AsyncSession = Depends(get_db)
) -> Provider:
    return await get_or_404(db, Provider, provider_id)


@router.patch("/{provider_id}", response_model=ProviderOut)
async def update_provider(
    provider_id: UUID,
    body: ProviderUpdate,
    db: AsyncSession = Depends(get_db),
    actor_id: UUID | None = Depends(get_admin_actor_id),
) -> Provider:
    provider = await get_or_404(db, Provider, provider_id)
    for key, value in body.model_dump(exclude_unset=True).items():
        setattr(provider, key, value)
    try:
        await db.flush()
    except IntegrityError as exc:
        handle_integrity_error(exc, field="name")
    await db.refresh(provider)
    await audit_write(
        db,
        action="provider.update",
        target=f"provider:{provider.id}",
        actor_user_id=actor_id,
        detail=body.model_dump(exclude_unset=True),
    )
    return provider


@router.delete("/{provider_id}", status_code=204)
async def delete_provider(
    provider_id: UUID,
    db: AsyncSession = Depends(get_db),
    actor_id: UUID | None = Depends(get_admin_actor_id),
) -> None:
    provider = await get_or_404(db, Provider, provider_id)
    channels = await list_all(
        db, select(Channel).where(Channel.provider_id == provider_id).limit(1)
    )
    if channels:
        raise InvalidRequestError("Cannot delete provider with existing channels")
    await db.delete(provider)
    await audit_write(
        db,
        action="provider.delete",
        target=f"provider:{provider_id}",
        actor_user_id=actor_id,
    )
