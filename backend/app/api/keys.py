"""用户端 API Key 管理（JWT 鉴权）。"""

from uuid import UUID

from fastapi import APIRouter, Depends, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_user_context
from app.core.constants import STATUS_DISABLED, STATUS_ENABLED
from app.core.errors import InvalidRequestError, ResourceNotFoundError
from app.db.models.api_key import ApiKey
from app.db.session import get_db
from app.schemas.auth import (
    ApiKeyCreate,
    ApiKeyCreatedOut,
    ApiKeyOut,
    ApiKeyUpdate,
    UserContext,
)
from app.services.auth_service import auth_service
from app.services.billing_subject_service import billing_subject_service

router = APIRouter(prefix="/keys", tags=["keys"])


@router.post("", response_model=ApiKeyCreatedOut, status_code=201)
async def create_key(
    body: ApiKeyCreate,
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> ApiKeyCreatedOut:
    subject = await billing_subject_service.validate_key_billing_subject(
        db, user_id=ctx.user_id, billing_subject=body.billing_subject
    )
    api_key, plaintext = await auth_service.create_api_key(
        db,
        user_id=ctx.user_id,
        name=body.name,
        billing_subject=subject,
        allowed_models=body.allowed_models,
        rpm_limit=body.rpm_limit,
        tpm_limit=body.tpm_limit,
        expires_at=body.expires_at,
    )
    base = ApiKeyOut.model_validate(api_key)
    return ApiKeyCreatedOut(**base.model_dump(), key=plaintext)


@router.get("", response_model=list[ApiKeyOut])
async def list_keys(
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> list[ApiKey]:
    stmt = (
        select(ApiKey)
        .where(ApiKey.user_id == ctx.user_id)
        .order_by(ApiKey.created_at.desc())
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


@router.patch("/{key_id}", response_model=ApiKeyOut)
async def update_key(
    key_id: UUID,
    body: ApiKeyUpdate,
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> ApiKey:
    stmt = select(ApiKey).where(ApiKey.id == key_id, ApiKey.user_id == ctx.user_id)
    result = await db.execute(stmt)
    api_key = result.scalar_one_or_none()
    if api_key is None:
        raise ResourceNotFoundError("API key not found")

    fields_set = body.model_fields_set
    if not fields_set:
        raise InvalidRequestError("No fields to update")

    name = body.name if "name" in fields_set else None
    update_subject = "billing_subject" in fields_set
    subject: str | None = None
    if update_subject:
        subject = await billing_subject_service.validate_key_billing_subject(
            db, user_id=ctx.user_id, billing_subject=body.billing_subject
        )
    await auth_service.update_api_key(
        db,
        api_key,
        name=name,
        update_billing_subject=update_subject,
        billing_subject=subject,
        update_allowed_models="allowed_models" in fields_set,
        allowed_models=body.allowed_models,
        update_rpm_limit="rpm_limit" in fields_set,
        rpm_limit=body.rpm_limit,
        update_tpm_limit="tpm_limit" in fields_set,
        tpm_limit=body.tpm_limit,
        update_expires_at="expires_at" in fields_set,
        expires_at=body.expires_at,
    )
    return api_key


@router.delete("/{key_id}", status_code=204)
async def delete_key(
    key_id: UUID,
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> Response:
    stmt = select(ApiKey).where(ApiKey.id == key_id, ApiKey.user_id == ctx.user_id)
    result = await db.execute(stmt)
    api_key = result.scalar_one_or_none()
    if api_key is None:
        raise ResourceNotFoundError("API key not found")
    if api_key.status == STATUS_ENABLED:
        api_key.status = STATUS_DISABLED
        await db.flush()
    return Response(status_code=204)
