"""管理端知识库：列表筛选、禁用/启用、用户汇总、全局配置。"""

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.admin.common import audit_write, get_or_404
from app.api.admin.deps import get_admin_actor_id
from app.core.constants import (
    KB_STATUS_ACTIVE,
    KB_STATUS_DISABLED,
    USAGE_SOURCE_KB_INGEST,
)
from app.core.errors import InvalidRequestError
from app.db.models.kb import KnowledgeBase
from app.db.models.log import RequestLog, UsageLog
from app.db.models.user import User
from app.db.session import get_db
from app.schemas.admin import (
    AdminKbUserStatsOut,
    AdminKnowledgeBaseOut,
    KbGlobalSettingsOut,
    KbGlobalSettingsUpdate,
)
from app.services.kb_settings_service import kb_settings_service

router = APIRouter(prefix="/kb", tags=["admin-kb"])


def _kb_out(kb: KnowledgeBase) -> AdminKnowledgeBaseOut:
    owner_email = kb.owner.email if kb.owner is not None else None
    model_alias = (
        kb.embedding_model.alias if kb.embedding_model is not None else None
    )
    return AdminKnowledgeBaseOut(
        id=kb.id,
        owner_user_id=kb.owner_user_id,
        owner_email=owner_email,
        name=kb.name,
        description=kb.description,
        embedding_model_id=kb.embedding_model_id,
        embedding_model_alias=model_alias,
        embedding_dim=kb.embedding_dim,
        doc_count=kb.doc_count,
        chunk_count=kb.chunk_count,
        storage_bytes=kb.storage_bytes,
        status=kb.status,
        created_at=kb.created_at,
        updated_at=kb.updated_at,
    )


@router.get("/settings", response_model=KbGlobalSettingsOut)
async def get_kb_settings(db: AsyncSession = Depends(get_db)) -> KbGlobalSettingsOut:
    row = await kb_settings_service.get_row(db)
    eff = await kb_settings_service.get_effective(db)
    return KbGlobalSettingsOut(
        default_embedding_model=row.default_embedding_model,
        max_file_size_bytes=row.max_file_size_bytes,
        upload_batch_limit=row.upload_batch_limit,
        embedding_batch_size=row.embedding_batch_size,
        embedding_max_concurrent=row.embedding_max_concurrent,
        effective_default_embedding_model=eff.default_embedding_model,
        effective_max_file_size_bytes=eff.max_file_size_bytes,
        effective_upload_batch_limit=eff.upload_batch_limit,
        effective_embedding_batch_size=eff.embedding_batch_size,
        effective_embedding_max_concurrent=eff.embedding_max_concurrent,
    )


@router.patch("/settings", response_model=KbGlobalSettingsOut)
async def update_kb_settings(
    body: KbGlobalSettingsUpdate,
    db: AsyncSession = Depends(get_db),
    actor_id: UUID | None = Depends(get_admin_actor_id),
) -> KbGlobalSettingsOut:
    patch = body.model_dump(exclude_unset=True)
    if not patch:
        raise InvalidRequestError("No settings to update")
    _row, eff, changed = await kb_settings_service.update(db, patch=patch)
    if changed:
        await audit_write(
            db,
            action="kb_settings.update",
            target="kb_global_settings:1",
            actor_user_id=actor_id,
            detail=changed,
        )
    return await get_kb_settings(db)


@router.get("/user-stats", response_model=list[AdminKbUserStatsOut])
async def list_kb_user_stats(
    user_id: UUID | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> list[AdminKbUserStatsOut]:
    """用户维度汇总：库数 / 文档数 / 存储量 / 向量化累计用量。"""
    kb_agg = (
        select(
            KnowledgeBase.owner_user_id.label("user_id"),
            func.count().label("kb_count"),
            func.coalesce(func.sum(KnowledgeBase.doc_count), 0).label("doc_count"),
            func.coalesce(func.sum(KnowledgeBase.storage_bytes), 0).label(
                "storage_bytes"
            ),
        )
        .group_by(KnowledgeBase.owner_user_id)
    )
    if user_id is not None:
        kb_agg = kb_agg.where(KnowledgeBase.owner_user_id == user_id)

    embed_agg = (
        select(
            RequestLog.user_id.label("user_id"),
            func.coalesce(func.sum(UsageLog.input_tokens), 0).label(
                "embed_input_tokens"
            ),
        )
        .join(UsageLog, UsageLog.request_id == RequestLog.request_id)
        .where(UsageLog.source == USAGE_SOURCE_KB_INGEST)
        .group_by(RequestLog.user_id)
    )
    if user_id is not None:
        embed_agg = embed_agg.where(RequestLog.user_id == user_id)

    kb_rows = {
        r.user_id: r for r in (await db.execute(kb_agg)).all()
    }
    embed_rows = {
        r.user_id: int(r.embed_input_tokens)
        for r in (await db.execute(embed_agg)).all()
    }
    user_ids = set(kb_rows) | set(embed_rows)
    if user_id is not None and user_id not in user_ids:
        user = await db.get(User, user_id)
        if user is None:
            return []
        return [
            AdminKbUserStatsOut(
                user_id=user_id,
                email=user.email,
                kb_count=0,
                doc_count=0,
                storage_bytes=0,
                embed_input_tokens=0,
            )
        ]

    users = {}
    if user_ids:
        for u in (
            await db.execute(select(User).where(User.id.in_(list(user_ids))))
        ).scalars().all():
            users[u.id] = u

    out: list[AdminKbUserStatsOut] = []
    for uid in sorted(user_ids, key=lambda x: str(x)):
        agg = kb_rows.get(uid)
        out.append(
            AdminKbUserStatsOut(
                user_id=uid,
                email=users[uid].email if uid in users else None,
                kb_count=int(agg.kb_count) if agg else 0,
                doc_count=int(agg.doc_count) if agg else 0,
                storage_bytes=int(agg.storage_bytes) if agg else 0,
                embed_input_tokens=embed_rows.get(uid, 0),
            )
        )
    return out


@router.get("", response_model=list[AdminKnowledgeBaseOut])
async def list_knowledge_bases(
    owner_user_id: UUID | None = Query(default=None),
    status: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> list[AdminKnowledgeBaseOut]:
    if status is not None and status not in (KB_STATUS_ACTIVE, KB_STATUS_DISABLED):
        raise InvalidRequestError("status must be 'active' or 'disabled'")
    stmt = (
        select(KnowledgeBase)
        .options(
            selectinload(KnowledgeBase.owner),
            selectinload(KnowledgeBase.embedding_model),
        )
        .order_by(KnowledgeBase.created_at.desc())
    )
    if owner_user_id is not None:
        stmt = stmt.where(KnowledgeBase.owner_user_id == owner_user_id)
    if status is not None:
        stmt = stmt.where(KnowledgeBase.status == status)
    rows = list((await db.execute(stmt)).scalars().all())
    return [_kb_out(kb) for kb in rows]


@router.post("/{kb_id}/disable", response_model=AdminKnowledgeBaseOut)
async def disable_knowledge_base(
    kb_id: UUID,
    db: AsyncSession = Depends(get_db),
    actor_id: UUID | None = Depends(get_admin_actor_id),
) -> AdminKnowledgeBaseOut:
    kb = await get_or_404(db, KnowledgeBase, kb_id)
    if kb.status != KB_STATUS_DISABLED:
        kb.status = KB_STATUS_DISABLED
        await db.flush()
        await audit_write(
            db,
            action="kb.disable",
            target=f"kb:{kb_id}",
            actor_user_id=actor_id,
            detail={"owner_user_id": str(kb.owner_user_id), "name": kb.name},
        )
    await db.refresh(kb, attribute_names=["owner", "embedding_model"])
    # refresh 可能不带 relationship；重新加载
    stmt = (
        select(KnowledgeBase)
        .where(KnowledgeBase.id == kb_id)
        .options(
            selectinload(KnowledgeBase.owner),
            selectinload(KnowledgeBase.embedding_model),
        )
    )
    kb = (await db.execute(stmt)).scalar_one()
    return _kb_out(kb)


@router.post("/{kb_id}/enable", response_model=AdminKnowledgeBaseOut)
async def enable_knowledge_base(
    kb_id: UUID,
    db: AsyncSession = Depends(get_db),
    actor_id: UUID | None = Depends(get_admin_actor_id),
) -> AdminKnowledgeBaseOut:
    kb = await get_or_404(db, KnowledgeBase, kb_id)
    if kb.status != KB_STATUS_ACTIVE:
        kb.status = KB_STATUS_ACTIVE
        await db.flush()
        await audit_write(
            db,
            action="kb.enable",
            target=f"kb:{kb_id}",
            actor_user_id=actor_id,
            detail={"owner_user_id": str(kb.owner_user_id), "name": kb.name},
        )
    stmt = (
        select(KnowledgeBase)
        .where(KnowledgeBase.id == kb_id)
        .options(
            selectinload(KnowledgeBase.owner),
            selectinload(KnowledgeBase.embedding_model),
        )
    )
    kb = (await db.execute(stmt)).scalar_one()
    return _kb_out(kb)
