"""后台写操作审计日志（只追加）。"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.log import AuditLog


async def record_audit(
    session: AsyncSession,
    *,
    action: str,
    target: str,
    actor_user_id: UUID | None = None,
    detail: dict | None = None,
) -> AuditLog:
    log = AuditLog(
        actor_user_id=actor_user_id,
        action=action,
        target=target,
        detail=detail or {},
    )
    session.add(log)
    await session.flush()
    return log
