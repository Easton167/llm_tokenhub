"""Billing Rule 管理 API。"""

from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.admin.common import audit_write, get_or_404, list_all
from app.api.admin.deps import get_admin_actor_id
from app.core.constants import STATUS_DISABLED, STATUS_ENABLED
from app.db.models.billing import BillingRule
from app.db.models.llm_model import LLMModel
from app.db.session import get_db
from app.schemas.admin import BillingRuleCreate, BillingRuleOut, BillingRuleUpdate

router = APIRouter(prefix="/models/{model_id}/billing-rules", tags=["admin-billing"])


@router.post("", response_model=BillingRuleOut, status_code=201)
async def create_billing_rule(
    model_id: UUID,
    body: BillingRuleCreate,
    db: AsyncSession = Depends(get_db),
    actor_id: UUID | None = Depends(get_admin_actor_id),
) -> BillingRule:
    await get_or_404(db, LLMModel, model_id)
    max_version = await db.scalar(
        select(func.max(BillingRule.version)).where(BillingRule.model_id == model_id)
    )
    next_version = (max_version or 0) + 1
    rule = BillingRule(
        model_id=model_id,
        version=next_version,
        status=STATUS_ENABLED,
        **body.model_dump(),
    )
    db.add(rule)
    await db.flush()
    await audit_write(
        db,
        action="billing_rule.create",
        target=f"billing_rule:{rule.id}",
        actor_user_id=actor_id,
        detail={"model_id": str(model_id), "version": next_version},
    )
    return rule


@router.get("", response_model=list[BillingRuleOut])
async def list_billing_rules(
    model_id: UUID, db: AsyncSession = Depends(get_db)
) -> list[BillingRule]:
    await get_or_404(db, LLMModel, model_id)
    return await list_all(
        db,
        select(BillingRule)
        .where(BillingRule.model_id == model_id)
        .order_by(BillingRule.version.desc()),
    )


@router.patch("/{rule_id}", response_model=BillingRuleOut)
async def void_billing_rule(
    model_id: UUID,
    rule_id: UUID,
    body: BillingRuleUpdate,
    db: AsyncSession = Depends(get_db),
    actor_id: UUID | None = Depends(get_admin_actor_id),
) -> BillingRule:
    rule = await _get_rule_for_model(db, model_id, rule_id)
    rule.status = body.status if body.status is not None else STATUS_DISABLED
    await db.flush()
    await db.refresh(rule)
    await audit_write(
        db,
        action="billing_rule.void",
        target=f"billing_rule:{rule.id}",
        actor_user_id=actor_id,
        detail={"status": rule.status},
    )
    return rule


@router.delete("/{rule_id}", status_code=204)
async def delete_billing_rule(
    model_id: UUID,
    rule_id: UUID,
    db: AsyncSession = Depends(get_db),
    actor_id: UUID | None = Depends(get_admin_actor_id),
) -> None:
    """删除（作废）计费规则：软删除，禁止物理删除以保留对账与审计。"""
    rule = await _get_rule_for_model(db, model_id, rule_id)
    rule.status = STATUS_DISABLED
    await db.flush()
    await audit_write(
        db,
        action="billing_rule.void",
        target=f"billing_rule:{rule.id}",
        actor_user_id=actor_id,
        detail={"status": rule.status},
    )


async def _get_rule_for_model(
    db: AsyncSession, model_id: UUID, rule_id: UUID
) -> BillingRule:
    rule = await get_or_404(db, BillingRule, rule_id)
    if rule.model_id != model_id:
        from app.core.errors import ResourceNotFoundError

        raise ResourceNotFoundError("billing_rule not found")
    return rule
