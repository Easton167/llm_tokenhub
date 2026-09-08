"""管理 API 公共依赖与工具。"""

from typing import TypeVar
from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, InvalidRequestError, ResourceNotFoundError
from app.services.audit_service import record_audit

T = TypeVar("T")


async def get_or_404(session: AsyncSession, model: type[T], resource_id: UUID) -> T:
    obj = await session.get(model, resource_id)
    if obj is None:
        raise ResourceNotFoundError(f"{model.__tablename__} not found")
    return obj


async def audit_write(
    session: AsyncSession,
    *,
    action: str,
    target: str,
    actor_user_id: UUID | None,
    detail: dict | None = None,
) -> None:
    await record_audit(
        session,
        action=action,
        target=target,
        actor_user_id=actor_user_id,
        detail=detail,
    )


def handle_integrity_error(exc: Exception, *, field: str) -> None:
    message = str(exc.orig) if hasattr(exc, "orig") else str(exc)
    if "unique" in message.lower() or "duplicate" in message.lower():
        raise ConflictError(f"{field} already exists") from exc
    raise InvalidRequestError("Database constraint violation") from exc


async def list_all(session: AsyncSession, stmt: Select[tuple[T]]) -> list[T]:
    result = await session.execute(stmt)
    return list(result.scalars().all())
