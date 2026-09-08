"""Model 与 Model Mapping 管理 API。"""

from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.admin.common import audit_write, get_or_404, handle_integrity_error, list_all
from app.api.admin.deps import get_admin_actor_id
from app.db.models.channel import Channel
from app.db.models.llm_model import LLMModel, ModelChannelMap
from app.db.session import get_db
from app.schemas.admin import (
    ModelCreate,
    ModelMappingCreate,
    ModelMappingOut,
    ModelMappingUpdate,
    ModelOut,
    ModelUpdate,
)

router = APIRouter(prefix="/models", tags=["admin-models"])


@router.post("", response_model=ModelOut, status_code=201)
async def create_model(
    body: ModelCreate,
    db: AsyncSession = Depends(get_db),
    actor_id: UUID | None = Depends(get_admin_actor_id),
) -> LLMModel:
    model = LLMModel(**body.model_dump())
    db.add(model)
    try:
        await db.flush()
    except IntegrityError as exc:
        handle_integrity_error(exc, field="alias")
    await audit_write(
        db,
        action="model.create",
        target=f"model:{model.id}",
        actor_user_id=actor_id,
        detail=body.model_dump(),
    )
    return model


@router.get("", response_model=list[ModelOut])
async def list_models(db: AsyncSession = Depends(get_db)) -> list[LLMModel]:
    return await list_all(db, select(LLMModel).order_by(LLMModel.alias))


@router.get("/{model_id}", response_model=ModelOut)
async def get_model(model_id: UUID, db: AsyncSession = Depends(get_db)) -> LLMModel:
    return await get_or_404(db, LLMModel, model_id)


@router.patch("/{model_id}", response_model=ModelOut)
async def update_model(
    model_id: UUID,
    body: ModelUpdate,
    db: AsyncSession = Depends(get_db),
    actor_id: UUID | None = Depends(get_admin_actor_id),
) -> LLMModel:
    model = await get_or_404(db, LLMModel, model_id)
    for key, value in body.model_dump(exclude_unset=True).items():
        setattr(model, key, value)
    try:
        await db.flush()
    except IntegrityError as exc:
        handle_integrity_error(exc, field="alias")
    await db.refresh(model)
    await audit_write(
        db,
        action="model.update",
        target=f"model:{model.id}",
        actor_user_id=actor_id,
        detail=body.model_dump(exclude_unset=True),
    )
    return model


@router.delete("/{model_id}", status_code=204)
async def delete_model(
    model_id: UUID,
    db: AsyncSession = Depends(get_db),
    actor_id: UUID | None = Depends(get_admin_actor_id),
) -> None:
    from app.core.constants import STATUS_DISABLED

    model = await get_or_404(db, LLMModel, model_id)
    model.status = STATUS_DISABLED
    await db.flush()
    await audit_write(
        db,
        action="model.disable",
        target=f"model:{model_id}",
        actor_user_id=actor_id,
    )


@router.post("/{model_id}/mappings", response_model=ModelMappingOut, status_code=201)
async def create_model_mapping(
    model_id: UUID,
    body: ModelMappingCreate,
    db: AsyncSession = Depends(get_db),
    actor_id: UUID | None = Depends(get_admin_actor_id),
) -> ModelChannelMap:
    await get_or_404(db, LLMModel, model_id)
    await get_or_404(db, Channel, body.channel_id)
    mapping = ModelChannelMap(model_id=model_id, **body.model_dump())
    db.add(mapping)
    try:
        await db.flush()
    except IntegrityError as exc:
        handle_integrity_error(exc, field="mapping")
    await audit_write(
        db,
        action="model_mapping.create",
        target=f"model_mapping:{mapping.id}",
        actor_user_id=actor_id,
        detail=body.model_dump(mode="json"),
    )
    return mapping


@router.get("/{model_id}/mappings", response_model=list[ModelMappingOut])
async def list_model_mappings(
    model_id: UUID, db: AsyncSession = Depends(get_db)
) -> list[ModelChannelMap]:
    await get_or_404(db, LLMModel, model_id)
    return await list_all(
        db,
        select(ModelChannelMap)
        .where(ModelChannelMap.model_id == model_id)
        .order_by(ModelChannelMap.created_at),
    )


@router.patch("/{model_id}/mappings/{mapping_id}", response_model=ModelMappingOut)
async def update_model_mapping(
    model_id: UUID,
    mapping_id: UUID,
    body: ModelMappingUpdate,
    db: AsyncSession = Depends(get_db),
    actor_id: UUID | None = Depends(get_admin_actor_id),
) -> ModelChannelMap:
    mapping = await get_or_404(db, ModelChannelMap, mapping_id)
    if mapping.model_id != model_id:
        from app.core.errors import ResourceNotFoundError

        raise ResourceNotFoundError("model_mapping not found")
    for key, value in body.model_dump(exclude_unset=True).items():
        setattr(mapping, key, value)
    await db.flush()
    await audit_write(
        db,
        action="model_mapping.update",
        target=f"model_mapping:{mapping.id}",
        actor_user_id=actor_id,
        detail=body.model_dump(exclude_unset=True),
    )
    return mapping


@router.delete("/{model_id}/mappings/{mapping_id}", status_code=204)
async def delete_model_mapping(
    model_id: UUID,
    mapping_id: UUID,
    db: AsyncSession = Depends(get_db),
    actor_id: UUID | None = Depends(get_admin_actor_id),
) -> None:
    mapping = await get_or_404(db, ModelChannelMap, mapping_id)
    if mapping.model_id != model_id:
        from app.core.errors import ResourceNotFoundError

        raise ResourceNotFoundError("model_mapping not found")
    await db.delete(mapping)
    await audit_write(
        db,
        action="model_mapping.delete",
        target=f"model_mapping:{mapping_id}",
        actor_user_id=actor_id,
    )
