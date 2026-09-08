"""内部 embedding 调用网关。

供知识库向量化 / 检索 query 等服务内部流量复用：模型解析 → 路由 → 适配器 →
用量记录；不占用用户 API Key，跳过 Key 级 RPM/TPM 限流，计费主体为指定用户
的个人钱包。在线 /v1/embeddings 仍走公开 API，不受本模块影响。

低优先级：平台级并发信号量限制同时在飞的内部上游请求，避免与 chat 争抢渠道预算。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.base import RouteCandidate
from app.adapters.registry import AdapterNotConfiguredError, get_adapter
from app.core.config import settings
from app.core.constants import MODEL_TYPE_EMBEDDING, USAGE_SOURCE_API
from app.core.errors import AppError, InvalidRequestError
from app.core.logging import get_logger
from app.core.metrics import EMBEDDING_LATENCY, EMBEDDING_REQUESTS, KB_EMBED_TOKENS
from app.db.models.billing import BillingRule
from app.db.models.user import User
from app.schemas.openai import EmbeddingRequest, EmbeddingResponse
from app.services.billing_service import billing_service
from app.services.billing_subject_service import format_personal_subject_ref
from app.services.permission_service import permission_service
from app.services.routing_service import NoAvailableChannelError, routing_service
from app.services.usage_service import write_request_log, write_usage_log
from app.services.wallet_service import wallet_service

logger = get_logger(__name__)

_DEFAULT_MAX_UPSTREAM_ATTEMPTS = 3
_RETRYABLE_STATUS_CODES = {401, 403, 429, 500, 502, 503, 504}

# 平台级低优先级并发闸（惰性初始化，便于测试替换）
_embed_semaphore: asyncio.Semaphore | None = None


def _get_embed_semaphore() -> asyncio.Semaphore:
    global _embed_semaphore
    if _embed_semaphore is None:
        _embed_semaphore = asyncio.Semaphore(
            max(1, settings.kb_embedding_max_concurrent)
        )
    return _embed_semaphore


def reset_embed_semaphore_for_tests(*, max_concurrent: int | None = None) -> None:
    """按给定或当前配置重建内部 embedding 并发闸（测试 / 管理端热更新）。"""
    global _embed_semaphore
    limit = (
        max_concurrent
        if max_concurrent is not None
        else settings.kb_embedding_max_concurrent
    )
    _embed_semaphore = asyncio.Semaphore(max(1, limit))


def estimate_embedding_input_tokens(texts: list[str]) -> int:
    """与公开 embeddings API 一致的粗略估算（4 字符 ≈ 1 token）。"""
    if not texts:
        return 1
    chars = sum(len(s) for s in texts)
    return max(1, chars // 4)


@dataclass(frozen=True, slots=True)
class InternalCaller:
    """服务内部调用方身份：无 API Key，计费落到 owner 个人钱包。"""

    user: User
    source: str  # kb_ingest / kb_query / ...

    @property
    def user_id(self) -> UUID:
        return self.user.id


@dataclass
class InternalEmbedResult:
    embeddings: list[list[float]]
    input_tokens: int
    is_estimated: bool
    request_id: str
    model_id: UUID
    model_alias: str
    billing_rule: BillingRule
    channel_id: UUID | None
    charge_amount: int
    cost_amount: int
    hold_amount: int
    latency_ms: int


async def _advance_retry_exclusion(
    db: AsyncSession,
    model_alias: str,
    exclude_key_ids: set[str],
    route: RouteCandidate,
    *,
    estimated_tokens: int = 1,
) -> None:
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


async def _call_upstream_embeddings(
    db: AsyncSession,
    *,
    body: EmbeddingRequest,
    request_id: str,
    tokens_est: int,
) -> tuple[EmbeddingResponse, UUID | None, int]:
    """路由 + 适配器 HTTP 调用；返回 (标准响应, channel_id, latency_ms)。"""
    start_ts = time.monotonic()
    exclude_key_ids: set[str] = set()
    last_error: Exception | None = None
    raw_response: dict[str, Any] | None = None
    route: RouteCandidate | None = None
    adapter = None
    channel_id: UUID | None = None

    for _attempt in range(_DEFAULT_MAX_UPSTREAM_ATTEMPTS):
        try:
            route = await routing_service.pick_route(
                db,
                body.model,
                exclude_key_ids=exclude_key_ids,
                estimated_tokens=tokens_est,
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
            async with _get_embed_semaphore():
                async with httpx.AsyncClient() as client:
                    resp = await client.post(
                        adapter.build_embeddings_url(route.base_url),
                        json=upstream_payload,
                        headers=adapter.build_headers(route.upstream_key, request_id),
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
                    estimated_tokens=tokens_est,
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
                estimated_tokens=tokens_est,
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
                estimated_tokens=tokens_est,
            )
            continue

    latency_ms = int((time.monotonic() - start_ts) * 1000)

    if raw_response is None:
        if last_error is None:
            last_error = NoAvailableChannelError()
        raise last_error

    assert adapter is not None
    std_response = adapter.parse_embeddings_response(raw_response)
    return std_response, channel_id, latency_ms


class InternalEmbeddingService:
    """内部调用方 embedding：跳过 Key 限流，个人钱包计费，落 source。"""

    async def embed_texts(
        self,
        db: AsyncSession,
        *,
        caller: InternalCaller,
        model_alias: str,
        texts: list[str],
        request_id: str | None = None,
        settle: bool = True,
        expected_dim: int | None = None,
    ) -> InternalEmbedResult:
        """批量向量化。

        settle=True：本请求内 hold → 上游 → capture/release（供 re_embed / kb_query）。
        settle=False：仅上游调用 + 写日志（charge_amount 记入 usage，钱包由调用方文档级结算）。
        """
        if not texts or any(t is None for t in texts):
            raise InvalidRequestError("'input' is required and must not be empty")
        if any(not isinstance(t, str) or t == "" for t in texts):
            raise InvalidRequestError("Each embedding input must be a non-empty string")

        req_id = request_id or str(uuid.uuid4())
        source = caller.source or USAGE_SOURCE_API

        # 无 api_key：跳过 Key 模型范围与 Key 级 RPM/TPM
        model_access = await permission_service.check_model_access(
            db,
            user_id=caller.user_id,
            model_alias=model_alias,
            api_key=None,
        )
        model = model_access.model
        if model.model_type != MODEL_TYPE_EMBEDDING:
            raise InvalidRequestError(
                f"Model '{model_alias}' is not an embedding model "
                f"(model_type={model.model_type})"
            )

        billing_rule = await billing_service.require_active_rule(db, model_id=model.id)
        input_tokens_est = estimate_embedding_input_tokens(texts)
        hold_amount = 0
        wallet = await wallet_service.get_or_create_personal_wallet(db, caller.user_id)
        billing_subject_ref = format_personal_subject_ref(caller.user_id)

        if settle:
            hold_amount = billing_service.estimate_hold_amount(
                billing_rule, input_tokens_est, max_output_tokens=0
            )
            if hold_amount > 0:
                await wallet_service.hold(
                    db,
                    wallet_id=wallet.id,
                    amount=hold_amount,
                    request_id=req_id,
                    operator_user_id=caller.user_id,
                )
                await db.flush()

        # 传入 dimensions，使上游按知识库/平台固定维度输出（如 DashScope 默认 1024）
        body = EmbeddingRequest(
            model=model_alias,
            input=texts,
            dimensions=expected_dim,
        )
        channel_id: UUID | None = None
        latency_ms = 0

        try:
            std_response, channel_id, latency_ms = await _call_upstream_embeddings(
                db,
                body=body,
                request_id=req_id,
                tokens_est=input_tokens_est,
            )
        except Exception as exc:
            if settle and hold_amount > 0:
                await wallet_service.release(
                    db,
                    wallet_id=wallet.id,
                    hold_amount=hold_amount,
                    request_id=req_id,
                    operator_user_id=caller.user_id,
                )
            await write_request_log(
                db,
                request_id=req_id,
                api_key_id=None,
                user_id=caller.user_id,
                billing_subject=billing_subject_ref,
                model_id=model.id,
                channel_id=channel_id,
                status="failed",
                http_status=getattr(exc, "status_code", 502),
                latency_ms=latency_ms or 0,
                is_stream=False,
                error_code=getattr(exc, "code", "internal_error"),
                source=source,
            )
            await db.flush()
            EMBEDDING_REQUESTS.labels(model=model_alias, status="failed").inc()
            EMBEDDING_LATENCY.labels(model=model_alias).observe(
                (latency_ms or 0) / 1000.0
            )
            raise

        usage = std_response.usage
        is_estimated = usage is None
        input_tokens = usage.prompt_tokens if usage else input_tokens_est
        charge_amount = billing_service.calculate_charge_amount(
            billing_rule, input_tokens, 0, 0
        )
        cost_amount = billing_service.calculate_cost_amount(
            billing_rule, input_tokens, 0, 0
        )

        if settle and hold_amount > 0:
            await wallet_service.capture(
                db,
                wallet_id=wallet.id,
                hold_amount=hold_amount,
                actual_amount=charge_amount,
                request_id=req_id,
                operator_user_id=caller.user_id,
            )

        # 按 index 排序取向量
        ordered = sorted(std_response.data, key=lambda d: d.index)
        embeddings: list[list[float]] = []
        for item in ordered:
            vec = item.embedding
            if isinstance(vec, str):
                raise InvalidRequestError(
                    "base64 embedding encoding is not supported for internal calls"
                )
            embeddings.append(list(vec))

        if len(embeddings) != len(texts):
            raise AppError(
                f"Upstream returned {len(embeddings)} embeddings for {len(texts)} inputs",
                code="upstream_error",
                error_type="upstream_error",
                status_code=502,
            )

        if expected_dim is not None:
            for i, vec in enumerate(embeddings):
                if len(vec) != expected_dim:
                    raise AppError(
                        f"Embedding dim mismatch at index {i}: "
                        f"got {len(vec)}, expected {expected_dim}",
                        code="upstream_error",
                        error_type="upstream_error",
                        status_code=502,
                    )

        await write_request_log(
            db,
            request_id=req_id,
            api_key_id=None,
            user_id=caller.user_id,
            billing_subject=billing_subject_ref,
            model_id=model.id,
            channel_id=channel_id,
            status="success",
            http_status=200,
            latency_ms=latency_ms,
            is_stream=False,
            source=source,
        )
        await write_usage_log(
            db,
            request_id=req_id,
            input_tokens=input_tokens,
            output_tokens=0,
            cached_tokens=0,
            billing_rule_version=billing_rule.version,
            cost_amount=cost_amount,
            charge_amount=charge_amount,
            is_estimated=is_estimated,
            source=source,
        )
        await db.flush()

        EMBEDDING_REQUESTS.labels(model=model_alias, status="success").inc()
        EMBEDDING_LATENCY.labels(model=model_alias).observe(latency_ms / 1000.0)
        if source in ("kb_ingest", "kb_query"):
            KB_EMBED_TOKENS.labels(source=source).inc(input_tokens)
        logger.info(
            "internal_embedding_completed",
            request_id=req_id,
            model=model_alias,
            source=source,
            input_tokens=input_tokens,
            charge_amount=charge_amount,
            settle=settle,
            batch_size=len(texts),
        )

        return InternalEmbedResult(
            embeddings=embeddings,
            input_tokens=input_tokens,
            is_estimated=is_estimated,
            request_id=req_id,
            model_id=model.id,
            model_alias=model_alias,
            billing_rule=billing_rule,
            channel_id=channel_id,
            charge_amount=charge_amount,
            cost_amount=cost_amount,
            hold_amount=hold_amount,
            latency_ms=latency_ms,
        )


internal_embedding_service = InternalEmbeddingService()
