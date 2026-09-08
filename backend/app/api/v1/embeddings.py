"""POST /v1/embeddings - OpenAI 兼容向量化接口。

完整调用链路与 chat 一致：
  鉴权 → 模型权限 → model_type 校验 → 计费规则 → 计费主体决策 → 预扣 →
  路由选择 → 上游 HTTP 调用 → 实扣结算 → 写 request_log/usage_log → 返回

计费：按输入 token（output 按 0）；组织身份同样受额度约束。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Annotated, Any
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.base import RouteCandidate
from app.adapters.registry import AdapterNotConfiguredError, get_adapter
from app.api.deps import get_auth_context
from app.core.constants import MODEL_TYPE_EMBEDDING
from app.core.errors import AppError, InvalidRequestError
from app.core.logging import get_logger
from app.core.metrics import EMBEDDING_LATENCY, EMBEDDING_REQUESTS
from app.db.models.billing import BillingRule
from app.db.session import get_db
from app.schemas.auth import AuthContext
from app.schemas.openai import EmbeddingRequest, EmbeddingResponse
from app.services.billing_service import billing_service
from app.services.billing_subject_service import BillingSubject, billing_subject_service
from app.services.permission_service import permission_service
from app.services.quota_service import quota_service
from app.services.rate_limit_service import rate_limit_service
from app.services.routing_service import NoAvailableChannelError, routing_service
from app.services.usage_service import write_request_log, write_usage_log
from app.services.wallet_service import wallet_service

logger = get_logger(__name__)

router = APIRouter(tags=["v1"])

_DEFAULT_MAX_UPSTREAM_ATTEMPTS = 3
_RETRYABLE_STATUS_CODES = {401, 403, 429, 500, 502, 503, 504}


def _record_embedding_metrics(*, model: str, status: str, latency_ms: int) -> None:
    EMBEDDING_REQUESTS.labels(model=model, status=status).inc()
    EMBEDDING_LATENCY.labels(model=model).observe(latency_ms / 1000.0)


def _estimate_input_tokens(body: EmbeddingRequest) -> int:
    """粗略估算 embeddings 输入 token（4 字符 ≈ 1 token）。"""
    inp = body.input
    if isinstance(inp, str):
        return max(1, len(inp) // 4)
    if isinstance(inp, list):
        if not inp:
            return 1
        if isinstance(inp[0], str):
            chars = sum(len(s) for s in inp)  # type: ignore[arg-type]
            return max(1, chars // 4)
        if isinstance(inp[0], int):
            return max(1, len(inp))  # type: ignore[arg-type]
        if isinstance(inp[0], list):
            return max(1, sum(len(x) for x in inp))  # type: ignore[arg-type]
    return 1


async def _advance_retry_exclusion(
    db: AsyncSession,
    model_alias: str,
    exclude_key_ids: set[str],
    route: RouteCandidate,
    *,
    estimated_tokens: int = 1,
) -> None:
    """将失败 Key 加入排除集；若无备用 Key 则清空排除集以允许同 Key 重试。"""
    exclude_key_ids.add(route.channel_key_id)
    try:
        await routing_service.pick_route(
            db,
            model_alias,
            exclude_key_ids=exclude_key_ids,
            estimated_tokens=estimated_tokens,
        )
    except NoAvailableChannelError:
        exclude_key_ids.clear()


def _should_retry_upstream(status_code: int | None, *, is_timeout: bool) -> bool:
    if is_timeout:
        return True
    return status_code in _RETRYABLE_STATUS_CODES


@dataclass
class _PreparedEmbeddingContext:
    """与 chat 的 _PreparedChatContext 同构：预扣快照，结算不得重新决策主体/规则。"""
    request_id: str
    start_ts: float
    model_id: Any
    model_alias: str
    billing_rule: BillingRule
    wallet_id: UUID
    hold_amount: int
    input_tokens_est: int
    tokens_est: int
    billing_subject_ref: str
    org_id: UUID | None
    org_member_id: UUID | None
    actor_user_id: UUID | None
    operator_user_id: UUID


async def _prepare_embedding_context(
    db: AsyncSession,
    body: EmbeddingRequest,
    auth: AuthContext,
    *,
    request_id: str,
    header_subject: str | None,
) -> _PreparedEmbeddingContext:
    """鉴权后共用：权限、model_type、限流、计费规则、主体决策、预扣。

    比 chat 多一步：必须 model_type=embedding，防止拿 chat 模型打 /v1/embeddings。
    计费只按输入 token（output=0）。限流在预扣之前，超限无资金变动。
    """
    start_ts = time.monotonic()

    if body.input is None or body.input == "" or body.input == []:
        raise InvalidRequestError("'input' is required and must not be empty")

    model_access = await permission_service.check_model_access(
        db,
        user_id=auth.user_id,
        model_alias=body.model,
        api_key=auth.api_key,
    )
    model = model_access.model
    if model.model_type != MODEL_TYPE_EMBEDDING:
        raise InvalidRequestError(
            f"Model '{body.model}' is not an embedding model "
            f"(model_type={model.model_type})"
        )

    billing_rule = await billing_service.require_active_rule(db, model_id=model.id)

    input_tokens_est = _estimate_input_tokens(body)
    tokens_est = input_tokens_est

    await rate_limit_service.enforce_api_key_limits(
        api_key_id=str(auth.api_key_id),
        rpm_limit=auth.api_key.rpm_limit,
        tpm_limit=auth.api_key.tpm_limit,
        estimated_tokens=tokens_est,
    )

    # embedding 无输出 token：预扣仅按输入（price_output 通常为 0）
    hold_amount = billing_service.estimate_hold_amount(
        billing_rule, input_tokens_est, max_output_tokens=0
    )

    subject: BillingSubject = await billing_subject_service.resolve(
        db,
        user=auth.user,
        api_key=auth.api_key,
        header_subject=header_subject,
        estimated_amount=hold_amount if hold_amount > 0 else None,
    )

    operator_user_id = subject.actor_user_id or auth.user_id
    if hold_amount > 0:
        await wallet_service.hold(
            db,
            wallet_id=subject.wallet.id,
            amount=hold_amount,
            request_id=request_id,
            operator_user_id=operator_user_id,
        )
    await db.commit()

    org_member_id = subject.org_member.id if subject.org_member is not None else None
    return _PreparedEmbeddingContext(
        request_id=request_id,
        start_ts=start_ts,
        model_id=model.id,
        model_alias=body.model,
        billing_rule=billing_rule,
        wallet_id=subject.wallet.id,
        hold_amount=hold_amount,
        input_tokens_est=input_tokens_est,
        tokens_est=tokens_est,
        billing_subject_ref=subject.subject_ref,
        org_id=subject.org_id,
        org_member_id=org_member_id,
        actor_user_id=subject.actor_user_id,
        operator_user_id=operator_user_id,
    )


async def _settle_charge(
    session: AsyncSession,
    *,
    ctx: _PreparedEmbeddingContext,
    charge_amount: int,
) -> None:
    if ctx.hold_amount > 0:
        await wallet_service.capture(
            session,
            wallet_id=ctx.wallet_id,
            hold_amount=ctx.hold_amount,
            actual_amount=charge_amount,
            request_id=ctx.request_id,
            operator_user_id=ctx.operator_user_id,
        )
    if ctx.org_member_id is not None and charge_amount > 0:
        await quota_service.quota_consume(
            session,
            org_member_id=ctx.org_member_id,
            amount=charge_amount,
        )


async def _on_failure(
    db: AsyncSession,
    *,
    ctx: _PreparedEmbeddingContext,
    auth: AuthContext,
    channel_id: Any,
    latency_ms: int,
    error_exc: Exception,
    http_status: int,
) -> None:
    """失败：释放冻结 + 写 request_log（不写 usage_log，不累计额度）。"""
    if ctx.hold_amount > 0:
        await wallet_service.release(
            db,
            wallet_id=ctx.wallet_id,
            hold_amount=ctx.hold_amount,
            request_id=ctx.request_id,
            operator_user_id=ctx.operator_user_id,
        )

    error_code = getattr(error_exc, "code", "internal_error")
    await write_request_log(
        db,
        request_id=ctx.request_id,
        api_key_id=auth.api_key_id,
        user_id=auth.user_id,
        billing_subject=ctx.billing_subject_ref,
        model_id=ctx.model_id,
        channel_id=channel_id,
        status="failed",
        http_status=http_status,
        latency_ms=latency_ms,
        is_stream=False,
        error_code=error_code,
        org_id=ctx.org_id,
        actor_user_id=ctx.actor_user_id,
    )
    await db.commit()
    _record_embedding_metrics(
        model=ctx.model_alias, status="failed", latency_ms=latency_ms or 0
    )
    logger.info(
        "embedding_failed",
        request_id=ctx.request_id,
        model=ctx.model_alias,
        status="failed",
        error_code=error_code,
        latency_ms=latency_ms,
    )


@router.post("/embeddings", response_model=EmbeddingResponse)
async def create_embeddings(
    body: EmbeddingRequest,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
    x_billing_subject: Annotated[str | None, Header(alias="X-Billing-Subject")] = None,
) -> EmbeddingResponse:
    """OpenAI 兼容 embeddings；按输入 token 计费。"""
    request_id = getattr(request.state, "request_id", None) or str(uuid.uuid4())
    ctx = await _prepare_embedding_context(
        db,
        body,
        auth,
        request_id=request_id,
        header_subject=x_billing_subject,
    )

    exclude_key_ids: set[str] = set()
    last_error: Exception | None = None
    raw_response: dict[str, Any] | None = None
    route: RouteCandidate | None = None
    adapter = None
    channel_id = None

    for _attempt in range(_DEFAULT_MAX_UPSTREAM_ATTEMPTS):
        try:
            route = await routing_service.pick_route(
                db,
                body.model,
                exclude_key_ids=exclude_key_ids,
                estimated_tokens=ctx.tokens_est,
            )
        except NoAvailableChannelError as exc:
            if last_error is not None:
                break
            last_error = exc
            break

        channel_id = uuid.UUID(route.channel_id)
        try:
            adapter = get_adapter(route.adapter_type)
        except AdapterNotConfiguredError as exc:
            last_error = exc
            break

        if not adapter.supports_embeddings():
            last_error = InvalidRequestError(
                f"Adapter '{route.adapter_type}' does not support embeddings"
            )
            break

        upstream_payload = adapter.build_embeddings_request(
            body, route.upstream_model_name
        )
        timeout_s = (route.timeout_ms or 120_000) / 1000.0

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    adapter.build_embeddings_url(route.base_url),
                    json=upstream_payload,
                    headers=adapter.build_headers(route.upstream_key, ctx.request_id),
                    timeout=timeout_s,
                )

            if resp.status_code == 200:
                raw_response = resp.json()
                break

            try:
                err_body = resp.json()
            except Exception:
                err_body = {}
            last_error = adapter.normalize_error(resp.status_code, err_body)
            if _should_retry_upstream(resp.status_code, is_timeout=False):
                await _advance_retry_exclusion(
                    db,
                    body.model,
                    exclude_key_ids,
                    route,
                    estimated_tokens=ctx.tokens_est,
                )
                continue
            break

        except httpx.TimeoutException:
            last_error = AppError(
                "Upstream request timed out",
                code="upstream_error",
                error_type="upstream_error",
                status_code=502,
            )
            await _advance_retry_exclusion(
                db,
                body.model,
                exclude_key_ids,
                route,
                estimated_tokens=ctx.tokens_est,
            )
            continue
        except Exception as exc:
            last_error = AppError(
                f"Upstream request failed: {exc}",
                code="upstream_error",
                error_type="upstream_error",
                status_code=502,
            )
            await _advance_retry_exclusion(
                db,
                body.model,
                exclude_key_ids,
                route,
                estimated_tokens=ctx.tokens_est,
            )
            continue

    latency_ms = int((time.monotonic() - ctx.start_ts) * 1000)

    if raw_response is None:
        if last_error is None:
            last_error = NoAvailableChannelError()
        await _on_failure(
            db,
            ctx=ctx,
            auth=auth,
            channel_id=channel_id,
            latency_ms=latency_ms,
            error_exc=last_error,
            http_status=getattr(last_error, "status_code", 502),
        )
        raise last_error

    assert adapter is not None
    std_response = adapter.parse_embeddings_response(raw_response)
    usage = std_response.usage
    is_estimated = usage is None

    input_tokens = usage.prompt_tokens if usage else ctx.input_tokens_est
    output_tokens = 0
    cached_tokens = 0

    charge_amount = billing_service.calculate_charge_amount(
        ctx.billing_rule, input_tokens, output_tokens, cached_tokens
    )
    cost_amount = billing_service.calculate_cost_amount(
        ctx.billing_rule, input_tokens, output_tokens, cached_tokens
    )

    await _settle_charge(db, ctx=ctx, charge_amount=charge_amount)

    await write_request_log(
        db,
        request_id=ctx.request_id,
        api_key_id=auth.api_key_id,
        user_id=auth.user_id,
        billing_subject=ctx.billing_subject_ref,
        model_id=ctx.model_id,
        channel_id=channel_id,
        status="success",
        http_status=200,
        latency_ms=latency_ms,
        is_stream=False,
        error_code=None,
        org_id=ctx.org_id,
        actor_user_id=ctx.actor_user_id,
    )
    await write_usage_log(
        db,
        request_id=ctx.request_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_tokens=cached_tokens,
        billing_rule_version=ctx.billing_rule.version,
        cost_amount=cost_amount,
        charge_amount=charge_amount,
        is_estimated=is_estimated,
        org_id=ctx.org_id,
        actor_user_id=ctx.actor_user_id,
    )
    await db.commit()
    _record_embedding_metrics(
        model=ctx.model_alias, status="success", latency_ms=latency_ms
    )
    logger.info(
        "embedding_completed",
        request_id=ctx.request_id,
        model=ctx.model_alias,
        status="success",
        latency_ms=latency_ms,
        charge_amount=charge_amount,
        input_tokens=input_tokens,
    )

    return std_response.model_copy(update={"model": body.model})
