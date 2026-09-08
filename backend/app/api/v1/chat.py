"""POST /v1/chat/completions - OpenAI 兼容的聊天补全接口。

完整调用链路（参考 TECH_DESIGN.md 第 6 章）：
  鉴权 → 模型权限 → 获取计费规则 → 计费主体决策 → 预扣余额 → 路由选择 →
  上游 HTTP 调用 → 实扣结算（组织主体另累计成员额度）→ 写 request_log/usage_log → 返回

失败处理原则（PRD 10.6）：
  - 上游失败 / 路由无渠道：全额释放冻结，仅写 request_log（status=failed），不写 usage_log，不累计额度。
  - 成功：实扣结算，写 request_log（status=success）+ usage_log。
  - 流式中断：按已产出内容 partial 结算，写 request_log（status=partial）+ usage_log。
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import Annotated, Any
from collections.abc import AsyncIterator
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.base import BaseAdapter, RouteCandidate
from app.adapters.registry import AdapterNotConfiguredError, get_adapter
from app.api.deps import get_auth_context
from app.core.errors import AppError
from app.core.logging import get_logger
from app.core.metrics import CHAT_LATENCY, CHAT_REQUESTS
from app.db.models.billing import BillingRule
from app.db.session import get_db, get_session_factory
from app.schemas.auth import AuthContext
from app.schemas.openai import ChatCompletionRequest, ChatCompletionResponse, Usage
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

_DEFAULT_MAX_OUTPUT_TOKENS = 2048  # 请求未带 max_tokens 时的预扣上界，宁可多冻、结算再退
_DEFAULT_MAX_UPSTREAM_ATTEMPTS = 3  # 含首次；换 Key/渠道，不是同一请求无脑重放

# 429 / 5xx / 超时 / Key 失效 → 换 Key 或渠道重试；400 类协议错误不重试
_RETRYABLE_STATUS_CODES = {401, 403, 429, 500, 502, 503, 504}


def _record_chat_metrics(
    *,
    model: str,
    status: str,
    stream: bool,
    latency_ms: int,
) -> None:
    """打点 chat 次数与延迟。status=success/failed/partial；stream 用 0/1 降基数。"""
    stream_label = "1" if stream else "0"
    CHAT_REQUESTS.labels(model=model, status=status, stream=stream_label).inc()
    CHAT_LATENCY.labels(model=model, stream=stream_label).observe(latency_ms / 1000.0)


def _estimate_input_tokens(request: ChatCompletionRequest) -> int:
    """粗略估算消息列表的输入 token 数（4 字符 ≈ 1 token）。

    仅用于预扣与 TPM 限流，不是计费依据。结算优先用上游 usage；
    缺 usage 时（部分上游/流式中断）才把估算值写入 usage_log 并标 is_estimated。
    """
    chars = 0
    for msg in request.messages:
        if isinstance(msg.content, str):
            chars += len(msg.content)
        elif isinstance(msg.content, list):
            for part in msg.content:
                if isinstance(part, dict):
                    chars += len(str(part.get("text", "")))
        chars += 10
    return max(1, chars // 4)


def _estimate_output_tokens_from_text(text: str) -> int:
    """流式中断时按已产出文本估算输出 token。"""
    return max(1, len(text) // 4) if text else 0


async def _advance_retry_exclusion(
    db: AsyncSession,
    model_alias: str,
    exclude_key_ids: set[str],
    route: RouteCandidate,
    *,
    estimated_tokens: int = 1,
) -> None:
    """将失败 Key 加入排除集；若排除后已无备用 Key，则清空排除集以允许同 Key 重试。

    清空是为了「只有一把 Key」时仍能吃满 max_attempts（例如上游抖动 502）。
    pick_route 只做探测，真正消费限额发生在下一轮正式 pick。
    """
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
    """超时一律重试；HTTP 仅 401/403/429/5xx 重试。其它（如 400）视为请求本身有问题。"""
    if is_timeout:
        return True
    return status_code in _RETRYABLE_STATUS_CODES


def _build_upstream_payload(
    adapter: BaseAdapter,
    body: ChatCompletionRequest,
    route: RouteCandidate,
    *,
    stream: bool,
) -> dict[str, Any]:
    """平台请求 → 上游协议。model 换成 mapping.upstream_model_name，stream 由网关强制写入。"""
    payload = adapter.build_request(body, route.upstream_model_name)
    payload["stream"] = stream
    if stream:
        adapter.apply_stream_options(payload)
    return payload


@dataclass
class _PreparedChatContext:
    """预扣完成后的不可变快照。后续路由/上游/结算只读这些字段，避免再决策一次主体。"""

    request_id: str
    start_ts: float  # monotonic，算 latency 用，不受墙钟回拨影响
    model_id: Any
    model_alias: str
    billing_rule: BillingRule  # 预扣时锁定的规则版本，结算必须用同一条，禁止中途换价
    wallet_id: UUID
    hold_amount: int
    input_tokens_est: int
    tokens_est: int  # 输入估算 + max_output，给路由 TPM / 渠道限额
    billing_subject_ref: str
    org_id: UUID | None
    org_member_id: UUID | None  # 非空才在 capture 时 quota_consume
    actor_user_id: UUID | None
    operator_user_id: UUID


async def _prepare_chat_context(
    db: AsyncSession,
    body: ChatCompletionRequest,
    auth: AuthContext,
    *,
    request_id: str,
    header_subject: str | None,
) -> _PreparedChatContext:
    """鉴权后共用：权限、限流、计费规则、主体决策、预扣。

    顺序不能乱：
      1. 模型权限 —— 无权则 403/404，无资金变动
      2. 计费规则 —— 没有启用规则直接拒，防漏计费
      3. Key 级 RPM/TPM —— 超限 429，此时尚未 hold
      4. 计费主体 —— 请求头 > Key 绑定 > 账户默认；组织额度不足按回退策略
      5. hold 并 commit —— 把冻结落盘后再打上游，避免长事务占行锁

    hold_amount==0（免费规则）跳过钱包操作。
    """
    start_ts = time.monotonic()

    model_access = await permission_service.check_model_access(
        db,
        user_id=auth.user_id,
        model_alias=body.model,
        api_key=auth.api_key,
    )
    model = model_access.model
    billing_rule = await billing_service.require_active_rule(db, model_id=model.id)

    input_tokens_est = _estimate_input_tokens(body)
    max_output_est = body.max_tokens or _DEFAULT_MAX_OUTPUT_TOKENS
    tokens_est = input_tokens_est + max_output_est

    await rate_limit_service.enforce_api_key_limits(
        api_key_id=str(auth.api_key_id),
        rpm_limit=auth.api_key.rpm_limit,
        tpm_limit=auth.api_key.tpm_limit,
        estimated_tokens=tokens_est,
    )

    hold_amount = billing_service.estimate_hold_amount(
        billing_rule, input_tokens_est, max_output_est
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
    # 预扣落盘后再打上游，避免长事务占钱包行锁；失败靠 request_id 幂等 release
    await db.commit()

    org_member_id = subject.org_member.id if subject.org_member is not None else None
    return _PreparedChatContext(
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
    ctx: _PreparedChatContext,
    charge_amount: int,
) -> None:
    """实扣对应钱包；组织主体另累计成员额度。失败路径不调用本函数。

    capture 按 hold 流水的赠送/现金拆分结算；quota_consume 只在组织成员扣费时发生。
    失败必须走 ``_on_failure``（release），否则冻结会一直占到对账任务超时释放。
    """
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


@router.post("/chat/completions", response_model=None)
async def chat_completions(
    body: ChatCompletionRequest,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
    x_billing_subject: Annotated[str | None, Header(alias="X-Billing-Subject")] = None,
) -> ChatCompletionResponse | StreamingResponse:
    """OpenAI 兼容聊天补全；stream=true 时返回 SSE 流式响应。

    X-Billing-Subject 可覆盖 Key/账户默认主体（personal / org:{uuid}）。
    预扣在分流之前完成，流式与非流式共享同一钱包冻结。
    """
    request_id = getattr(request.state, "request_id", None) or str(uuid.uuid4())
    ctx = await _prepare_chat_context(
        db,
        body,
        auth,
        request_id=request_id,
        header_subject=x_billing_subject,
    )

    if body.stream:
        return await _chat_completions_stream(request, body, auth, ctx, db)

    return await _chat_completions_non_stream(db, body, auth, ctx)


async def _chat_completions_non_stream(
    db: AsyncSession,
    body: ChatCompletionRequest,
    auth: AuthContext,
    ctx: _PreparedChatContext,
) -> ChatCompletionResponse:
    """非流式：选路 → 调上游 → 解析 usage → capture。最多换 Key 重试 N 次。"""
    exclude_key_ids: set[str] = set()
    last_error: Exception | None = None
    raw_response: dict[str, Any] | None = None
    route: RouteCandidate | None = None
    adapter: BaseAdapter | None = None
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
            # 首次就无渠道：直接失败；若已有上游错误则保留那个错误（更贴近真实原因）
            if last_error is not None:
                break
            last_error = exc
            break

        channel_id = uuid.UUID(route.channel_id)
        try:
            adapter = get_adapter(route.adapter_type)
        except AdapterNotConfiguredError as exc:
            # 渠道协议类型配置错误：明确报错，不做无意义重试
            last_error = exc
            break

        try:
            upstream_payload = _build_upstream_payload(adapter, body, route, stream=False)
        except AppError as exc:
            # 请求无法映射到该协议（如 anthropic 不支持 tool use）：明确报错，不重试
            last_error = exc
            break
        timeout_s = (route.timeout_ms or 120_000) / 1000.0

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    adapter.build_url(route.base_url),
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
        # 三次都没拿到 200：释放冻结，只记 request_log，不记 usage、不扣额度
        if last_error is None:
            last_error = NoAvailableChannelError()
        await _on_failure(
            db,
            wallet_id=ctx.wallet_id if ctx.hold_amount > 0 else None,
            hold_amount=ctx.hold_amount,
            request_id=ctx.request_id,
            auth=auth,
            ctx=ctx,
            model_id=ctx.model_id,
            model_alias=ctx.model_alias,
            channel_id=channel_id,
            latency_ms=latency_ms,
            error_exc=last_error,
            http_status=getattr(last_error, "status_code", 502),
            is_stream=False,
        )
        raise last_error

    assert adapter is not None  # raw_response 非空时必然已成功取到适配器
    std_response = adapter.parse_response(raw_response)
    usage = std_response.usage
    is_estimated = usage is None

    # 上游没回 usage 时用预扣估算的输入、输出按 0，并标记 is_estimated 供对账
    input_tokens = usage.prompt_tokens if usage else ctx.input_tokens_est
    output_tokens = usage.completion_tokens if usage else 0
    cached_tokens = usage.cached_tokens if usage else 0

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
    _record_chat_metrics(
        model=ctx.model_alias,
        status="success",
        stream=False,
        latency_ms=latency_ms,
    )
    logger.info(
        "chat_completed",
        request_id=ctx.request_id,
        model=ctx.model_alias,
        status="success",
        latency_ms=latency_ms,
        charge_amount=charge_amount,
    )

    return std_response.model_copy(update={"model": body.model})


async def _chat_completions_stream(
    request: Request,
    body: ChatCompletionRequest,
    auth: AuthContext,
    ctx: _PreparedChatContext,
    db: AsyncSession,
) -> StreamingResponse:
    """流式 SSE 转发。

    与非流式的关键差别：必须在 return StreamingResponse 之前把上游 HTTP 流建好。
    否则 FastAPI 已经把 200 SSE 交给客户端，再失败就没法映射成 JSON 错误体。
    结算放在 event_generator 的 finally：无论客户端断开、上游中断还是正常 [DONE]，
    都要 capture 或（零 chunk 时）由外层 _on_failure 已经 release。
    """

    exclude_key_ids: set[str] = set()
    last_error: Exception | None = None
    route: RouteCandidate | None = None
    adapter: BaseAdapter | None = None
    channel_id = None
    client = httpx.AsyncClient()
    active_resp: httpx.Response | None = None
    stream_ctx: Any = None

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
            # 渠道协议类型配置错误：明确报错，不做无意义重试
            last_error = exc
            break

        try:
            upstream_payload = _build_upstream_payload(adapter, body, route, stream=True)
        except AppError as exc:
            # 请求无法映射到该协议（如 anthropic 不支持 tool use）：明确报错，不重试
            last_error = exc
            break
        timeout_s = (route.timeout_ms or 120_000) / 1000.0

        if stream_ctx is not None:
            await stream_ctx.__aexit__(None, None, None)
            stream_ctx = None

        stream_ctx = client.stream(
            "POST",
            adapter.build_url(route.base_url),
            json=upstream_payload,
            headers=adapter.build_headers(route.upstream_key, ctx.request_id),
            timeout=timeout_s,
        )
        try:
            active_resp = await stream_ctx.__aenter__()
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
            stream_ctx = None
            continue

        if active_resp.status_code != 200:
            try:
                err_body = json.loads(await active_resp.aread())
            except Exception:
                err_body = {}
            last_error = adapter.normalize_error(active_resp.status_code, err_body)
            await stream_ctx.__aexit__(None, None, None)
            stream_ctx = None
            if _should_retry_upstream(active_resp.status_code, is_timeout=False):
                await _advance_retry_exclusion(
                    db,
                    body.model,
                    exclude_key_ids,
                    route,
                    estimated_tokens=ctx.tokens_est,
                )
                continue
            break

        last_error = None
        break

    if active_resp is None or active_resp.status_code != 200:
        await client.aclose()
        latency_ms = int((time.monotonic() - ctx.start_ts) * 1000)
        err = last_error or NoAvailableChannelError()
        await _on_failure(
            db,
            wallet_id=ctx.wallet_id if ctx.hold_amount > 0 else None,
            hold_amount=ctx.hold_amount,
            request_id=ctx.request_id,
            auth=auth,
            ctx=ctx,
            model_id=ctx.model_id,
            model_alias=ctx.model_alias,
            channel_id=channel_id,
            latency_ms=latency_ms,
            error_exc=err,
            http_status=getattr(err, "status_code", 502),
            is_stream=True,
        )
        raise err

    async def event_generator() -> AsyncIterator[str]:
        """把上游 SSE 转成 OpenAI 兼容 chunk；model 改回平台 alias。"""
        nonlocal stream_ctx, client
        collected_usage: Usage | None = None
        output_text_parts: list[str] = []  # 无 usage 时按已产出文本估输出 token
        chunks_sent = 0
        stream_error: Exception | None = None

        try:
            async for line in active_resp.aiter_lines():  # type: ignore[union-attr]
                if await request.is_disconnected():
                    # 客户端掐了：停读上游，finally 按 partial 结算已产出内容
                    break

                if not line.startswith("data:"):
                    continue

                data = line[len("data:") :].strip()
                if data == "[DONE]":
                    yield "data: [DONE]\n\n"
                    break

                chunk = adapter.parse_stream_chunk(data)  # type: ignore[union-attr]
                if chunk is None:
                    continue

                if chunk.usage is not None:
                    collected_usage = chunk.usage

                for choice in chunk.choices:
                    content = choice.delta.content
                    if content:
                        output_text_parts.append(content)

                out_chunk = chunk.model_copy(update={"model": body.model})
                yield f"data: {out_chunk.model_dump_json(exclude_none=True)}\n\n"
                chunks_sent += 1

            if chunks_sent > 0 and not await request.is_disconnected():
                yield "data: [DONE]\n\n"
        except Exception as exc:
            stream_error = exc
        finally:
            if stream_ctx is not None:
                await stream_ctx.__aexit__(None, None, None)
            await client.aclose()

            latency_ms = int((time.monotonic() - ctx.start_ts) * 1000)
            client_disconnected = await request.is_disconnected()
            await _finalize_stream(
                ctx=ctx,
                auth=auth,
                channel_id=channel_id,
                latency_ms=latency_ms,
                collected_usage=collected_usage,
                output_text="".join(output_text_parts),
                chunks_sent=chunks_sent,
                client_disconnected=client_disconnected,
                last_error=stream_error if chunks_sent > 0 else None,
                db=db,
            )

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # 关掉 nginx 缓冲，否则客户端收不到逐 token
        },
    )


async def _finalize_stream(
    *,
    ctx: _PreparedChatContext,
    auth: AuthContext,
    channel_id: Any,
    latency_ms: int,
    collected_usage: Usage | None,
    output_text: str,
    chunks_sent: int,
    client_disconnected: bool,
    last_error: Exception | None,
    db: AsyncSession | None = None,
) -> None:
    """流式结束后结算：成功 / partial / 失败（零 chunk）。

    - chunks_sent==0：这里空操作。连接建立失败已在外层 _on_failure；
      若流已建立但一个 chunk 都没发出，预扣仍在，依赖对账任务超时 release
      （event_generator 此时不会再走 _on_failure）。
    - 有 chunk + 客户端断开或中途异常 → status=partial，http 499，按已产出计费。
    - 正常结束 → success。缺 usage 则用估算 token 并标 is_estimated。
    """

    async def _settle(session: AsyncSession) -> None:
        if chunks_sent == 0:
            return

        is_partial = client_disconnected or (last_error is not None and chunks_sent > 0)
        is_estimated = collected_usage is None

        if collected_usage is not None:
            input_tokens = collected_usage.prompt_tokens
            output_tokens = collected_usage.completion_tokens
            cached_tokens = collected_usage.cached_tokens
        else:
            input_tokens = ctx.input_tokens_est
            output_tokens = _estimate_output_tokens_from_text(output_text)
            cached_tokens = 0

        charge_amount = billing_service.calculate_charge_amount(
            ctx.billing_rule, input_tokens, output_tokens, cached_tokens
        )
        cost_amount = billing_service.calculate_cost_amount(
            ctx.billing_rule, input_tokens, output_tokens, cached_tokens
        )

        await _settle_charge(session, ctx=ctx, charge_amount=charge_amount)

        status = "partial" if is_partial else "success"
        http_status = 200 if not is_partial else 499

        await write_request_log(
            session,
            request_id=ctx.request_id,
            api_key_id=auth.api_key_id,
            user_id=auth.user_id,
            billing_subject=ctx.billing_subject_ref,
            model_id=ctx.model_id,
            channel_id=channel_id,
            status=status,
            http_status=http_status,
            latency_ms=latency_ms,
            is_stream=True,
            error_code="client_disconnected" if client_disconnected else None,
            org_id=ctx.org_id,
            actor_user_id=ctx.actor_user_id,
        )
        await write_usage_log(
            session,
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
        await session.commit()
        _record_chat_metrics(
            model=ctx.model_alias,
            status=status,
            stream=True,
            latency_ms=latency_ms,
        )
        logger.info(
            "chat_completed",
            request_id=ctx.request_id,
            model=ctx.model_alias,
            status=status,
            latency_ms=latency_ms,
            charge_amount=charge_amount,
            is_stream=True,
        )

    if db is not None:
        # 正常路径：复用请求级会话（get_db 稍后还会 commit，这里先提交避免流结束后会话已脏）
        try:
            await _settle(db)
        except Exception:
            await db.rollback()
            raise
        return

    session_factory = get_session_factory()
    async with session_factory() as session:
        try:
            await _settle(session)
        except Exception:
            await session.rollback()
            raise


async def _on_failure(
    db: AsyncSession,
    *,
    wallet_id: Any,
    hold_amount: int,
    request_id: str,
    auth: AuthContext,
    ctx: _PreparedChatContext,
    model_id: Any,
    model_alias: str,
    channel_id: Any,
    latency_ms: int,
    error_exc: Exception,
    http_status: int,
    is_stream: bool,
) -> None:
    """失败统一处理：释放冻结 + 写 request_log（不写 usage_log，不累计额度）。

    对应 PRD：上游失败 / 无渠道 / 适配器未配置，用户不应被扣费。
    release 按 hold 流水原路退赠送池和现金池，不核销 gift_grants。
    """
    if wallet_id is not None and hold_amount > 0:
        await wallet_service.release(
            db,
            wallet_id=wallet_id,
            hold_amount=hold_amount,
            request_id=request_id,
            operator_user_id=ctx.operator_user_id,
        )

    error_code = getattr(error_exc, "code", "internal_error")
    await write_request_log(
        db,
        request_id=request_id,
        api_key_id=auth.api_key_id,
        user_id=auth.user_id,
        billing_subject=ctx.billing_subject_ref,
        model_id=model_id,
        channel_id=channel_id,
        status="failed",
        http_status=http_status,
        latency_ms=latency_ms,
        is_stream=is_stream,
        error_code=error_code,
        org_id=ctx.org_id,
        actor_user_id=ctx.actor_user_id,
    )
    await db.commit()
    _record_chat_metrics(
        model=model_alias,
        status="failed",
        stream=is_stream,
        latency_ms=latency_ms or 0,
    )
    logger.info(
        "chat_failed",
        request_id=request_id,
        model=model_alias,
        status="failed",
        error_code=error_code,
        latency_ms=latency_ms,
        is_stream=is_stream,
    )
