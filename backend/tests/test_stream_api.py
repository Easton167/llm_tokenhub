"""流式响应与故障处理集成测试（Task 8）。

覆盖验收标准：
  - stream=true 返回 text/event-stream
  - 上游 chunk 透传、[DONE]
  - 完整流式响应实扣
  - 客户端中断 partial 计费
  - 上游全部失败释放冻结
  - 429 有限重试后成功
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator, AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from app.db.models.log import RequestLog, UsageLog
from app.db.models.wallet import WalletTransaction
from app.db.session import get_db
from app.main import app
from tests.test_openai_api import (
    _patch_httpx,
    _seed_api_key,
    _seed_billing_rule,
    _seed_channel,
    _seed_mapping,
    _seed_model,
    _seed_user,
    _seed_wallet,
)


# ===========================================================================
# 流式 Mock 辅助
# ===========================================================================


class _MockStreamResponse:
    """模拟 httpx 流式响应。"""

    def __init__(
        self,
        status_code: int,
        lines: list[str] | None = None,
        *,
        error_body: dict | None = None,
    ) -> None:
        self.status_code = status_code
        self._lines = lines or []
        self._error_body = error_body or {"error": {"message": "error", "code": "upstream_error"}}

    async def aiter_lines(self) -> AsyncIterator[str]:
        for line in self._lines:
            yield line

    async def aread(self) -> bytes:
        return json.dumps(self._error_body).encode()


class _MockStreamContext:
    def __init__(self, response: _MockStreamResponse) -> None:
        self._response = response

    async def __aenter__(self) -> _MockStreamResponse:
        return self._response

    async def __aexit__(self, *args: Any) -> None:
        return None


def _make_sse_lines(
    *,
    chunks: list[dict[str, Any]],
    include_done: bool = True,
) -> list[str]:
    lines = [f"data: {json.dumps(c)}" for c in chunks]
    if include_done:
        lines.append("data: [DONE]")
    return lines


def _make_success_stream_chunks(
    *,
    content_parts: list[str] = ("你", "好"),
    prompt_tokens: int = 10,
    completion_tokens: int = 4,
) -> list[str]:
    chunks: list[dict[str, Any]] = []
    for part in content_parts:
        chunks.append(
            {
                "id": "chatcmpl-stream-test",
                "object": "chat.completion.chunk",
                "created": 1720000000,
                "model": "gpt-4o-2024-11-20",
                "choices": [{"index": 0, "delta": {"content": part}}],
            }
        )
    chunks.append(
        {
            "id": "chatcmpl-stream-test",
            "object": "chat.completion.chunk",
            "created": 1720000000,
            "model": "gpt-4o-2024-11-20",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        }
    )
    return _make_sse_lines(chunks=chunks)


def _patch_httpx_stream(
    responses: list[_MockStreamResponse],
) -> tuple[Any, list[int]]:
    """拦截 httpx.AsyncClient，按调用顺序返回预设流式响应。"""
    call_counter: list[int] = [0]

    def _stream_factory(*_args: Any, **_kwargs: Any) -> _MockStreamContext:
        idx = call_counter[0]
        call_counter[0] += 1
        resp = responses[min(idx, len(responses) - 1)]
        return _MockStreamContext(resp)

    mock_client = MagicMock()
    mock_client.stream = MagicMock(side_effect=_stream_factory)
    mock_client.aclose = AsyncMock()
    mock_client_cls = MagicMock(return_value=mock_client)
    return patch("app.api.v1.chat.httpx.AsyncClient", mock_client_cls), call_counter


# ===========================================================================
# Fixtures（复用 test_openai_api 的 api_client 模式）
# ===========================================================================


@pytest_asyncio.fixture
async def api_client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    original_commit = db_session.commit

    async def _fake_commit() -> None:
        await db_session.flush()

    db_session.commit = _fake_commit  # type: ignore[method-assign]

    async def override_get_db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    db_session.commit = original_commit  # type: ignore[method-assign]
    app.dependency_overrides.clear()


async def _seed_stream_fixture(
    db_session: AsyncSession,
) -> tuple[str, str, Any]:
    """创建流式测试所需的最小数据集，返回 (api_key_plaintext, model_alias, user)。"""
    user = await _seed_user(db_session)
    _, plaintext = await _seed_api_key(db_session, user)
    model = await _seed_model(db_session)
    await _seed_billing_rule(db_session, model)
    _, channel, _ = await _seed_channel(db_session)
    await _seed_mapping(db_session, model, channel)
    await _seed_wallet(db_session, user, balance=100_000_000)
    return plaintext, model.alias, user


# ===========================================================================
# 流式测试
# ===========================================================================


@pytest.mark.asyncio
async def test_stream_returns_event_stream_content_type(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    """STREAM-01: stream=true 返回 text/event-stream。"""
    plaintext, model_alias, _ = await _seed_stream_fixture(db_session)
    lines = _make_success_stream_chunks()
    http_patch, _ = _patch_httpx_stream([_MockStreamResponse(200, lines)])

    with http_patch:
        resp = await api_client.post(
            "/v1/chat/completions",
            json={
                "model": model_alias,
                "messages": [{"role": "user", "content": "你好"}],
                "stream": True,
            },
            headers={"Authorization": f"Bearer {plaintext}"},
        )

    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers.get("content-type", "")


@pytest.mark.asyncio
async def test_stream_chunks_passthrough_and_done(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    """STREAM-02/03: chunk 透传且以 [DONE] 结束。"""
    plaintext, model_alias, _ = await _seed_stream_fixture(db_session)
    lines = _make_success_stream_chunks(content_parts=("你", "好", "！"))
    http_patch, _ = _patch_httpx_stream([_MockStreamResponse(200, lines)])

    with http_patch:
        resp = await api_client.post(
            "/v1/chat/completions",
            json={
                "model": model_alias,
                "messages": [{"role": "user", "content": "你好"}],
                "stream": True,
            },
            headers={"Authorization": f"Bearer {plaintext}"},
        )

    body = resp.text
    assert "data: " in body
    assert "你" in body
    assert "好" in body
    assert "data: [DONE]" in body
    # model 字段应为平台 alias
    assert f'"model": "{model_alias}"' in body or f'"model":"{model_alias}"' in body


@pytest.mark.asyncio
async def test_stream_success_produces_capture_and_usage_log(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    """完整流式响应能正常实扣并写 usage_log。"""
    plaintext, model_alias, user = await _seed_stream_fixture(db_session)
    lines = _make_success_stream_chunks(prompt_tokens=12, completion_tokens=6)
    http_patch, _ = _patch_httpx_stream([_MockStreamResponse(200, lines)])

    with http_patch:
        await api_client.post(
            "/v1/chat/completions",
            json={
                "model": model_alias,
                "messages": [{"role": "user", "content": "你好"}],
                "stream": True,
                "max_tokens": 50,
            },
            headers={"Authorization": f"Bearer {plaintext}"},
        )

    req_log = await db_session.execute(
        select(RequestLog).where(
            RequestLog.user_id == user.id,
            RequestLog.status == "success",
            RequestLog.is_stream.is_(True),
        )
    )
    req_log_obj = req_log.scalar_one_or_none()
    assert req_log_obj is not None

    usage_log = await db_session.execute(
        select(UsageLog).where(UsageLog.request_id == req_log_obj.request_id)
    )
    usage_log_obj = usage_log.scalar_one_or_none()
    assert usage_log_obj is not None
    assert usage_log_obj.input_tokens == 12
    assert usage_log_obj.output_tokens == 6

    capture_txn = await db_session.execute(
        select(WalletTransaction).where(
            WalletTransaction.type == "capture",
            WalletTransaction.ref_request_id == req_log_obj.request_id,
        )
    )
    assert capture_txn.scalar_one_or_none() is not None


@pytest.mark.asyncio
async def test_stream_upstream_failure_before_output_releases_hold(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    """STREAM-05: 上游开始前失败，释放冻结，不计费。"""
    plaintext, model_alias, user = await _seed_stream_fixture(db_session)
    error_resp = _MockStreamResponse(
        500,
        error_body={"error": {"message": "Internal Server Error", "code": "upstream_error"}},
    )
    http_patch, _ = _patch_httpx_stream([error_resp, error_resp, error_resp])

    with http_patch:
        await api_client.post(
            "/v1/chat/completions",
            json={
                "model": model_alias,
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": True,
            },
            headers={"Authorization": f"Bearer {plaintext}"},
        )

    req_log = await db_session.execute(
        select(RequestLog).where(
            RequestLog.user_id == user.id,
            RequestLog.status == "failed",
            RequestLog.is_stream.is_(True),
        )
    )
    failed_log = req_log.scalar_one_or_none()
    assert failed_log is not None

    usage_log = await db_session.execute(
        select(UsageLog).where(UsageLog.request_id == failed_log.request_id)
    )
    assert usage_log.scalar_one_or_none() is None

    release_txn = await db_session.execute(
        select(WalletTransaction).where(
            WalletTransaction.type == "release",
            WalletTransaction.ref_request_id == failed_log.request_id,
        )
    )
    assert release_txn.scalar_one_or_none() is not None


@pytest.mark.asyncio
async def test_stream_client_disconnect_partial_billing(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    """STREAM-04: 客户端中断后 partial 计费。"""
    plaintext, model_alias, user = await _seed_stream_fixture(db_session)
    lines = _make_success_stream_chunks(
        content_parts=("部", "分", "输", "出", "很", "长"),
        prompt_tokens=8,
        completion_tokens=99,  # 不会收到最终 usage chunk
    )
    # 去掉带 usage 的最终 chunk，模拟中断前无 usage
    lines_without_final_usage = [
        ln for ln in lines if '"usage"' not in ln or "[DONE]" in ln
    ]
    http_patch, _ = _patch_httpx_stream(
        [_MockStreamResponse(200, lines_without_final_usage)]
    )

    disconnect_calls = 0

    async def _disconnect_after_first_chunk(self: Request) -> bool:  # type: ignore[no-untyped-def]
        nonlocal disconnect_calls
        disconnect_calls += 1
        return disconnect_calls > 2

    with http_patch, patch.object(Request, "is_disconnected", _disconnect_after_first_chunk):
        await api_client.post(
            "/v1/chat/completions",
            json={
                "model": model_alias,
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": True,
            },
            headers={"Authorization": f"Bearer {plaintext}"},
        )

    req_log = await db_session.execute(
        select(RequestLog).where(
            RequestLog.user_id == user.id,
            RequestLog.status == "partial",
            RequestLog.is_stream.is_(True),
        )
    )
    req_log_obj = req_log.scalar_one_or_none()
    assert req_log_obj is not None

    usage_log = await db_session.execute(
        select(UsageLog).where(UsageLog.request_id == req_log_obj.request_id)
    )
    usage_log_obj = usage_log.scalar_one_or_none()
    assert usage_log_obj is not None
    assert usage_log_obj.is_estimated is True
    assert usage_log_obj.output_tokens >= 1

    capture_txn = await db_session.execute(
        select(WalletTransaction).where(
            WalletTransaction.type == "capture",
            WalletTransaction.ref_request_id == req_log_obj.request_id,
        )
    )
    assert capture_txn.scalar_one_or_none() is not None


@pytest.mark.asyncio
async def test_stream_retry_on_429_then_success(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    """上游 429 后换 Key 重试成功。"""
    from app.core.channel_key_crypto import encrypt_channel_key
    from app.db.models.channel import ChannelKey

    user = await _seed_user(db_session)
    _, plaintext = await _seed_api_key(db_session, user)
    model = await _seed_model(db_session)
    await _seed_billing_rule(db_session, model)
    _, channel, first_key = await _seed_channel(db_session)

    second_key = ChannelKey(
        channel_id=channel.id,
        api_key_encrypted=encrypt_channel_key(f"sk-retry-{uuid4().hex[:8]}"),
        key_masked="****retry",
        weight=1,
        status=0,
        health_status=0,
    )
    db_session.add(second_key)
    await db_session.flush()

    await _seed_mapping(db_session, model, channel)
    await _seed_wallet(db_session, user, balance=100_000_000)

    error_resp = _MockStreamResponse(429, error_body={"error": {"message": "Rate limited"}})
    success_lines = _make_success_stream_chunks()
    http_patch, call_counter = _patch_httpx_stream([error_resp, _MockStreamResponse(200, success_lines)])

    with http_patch:
        resp = await api_client.post(
            "/v1/chat/completions",
            json={
                "model": model.alias,
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": True,
            },
            headers={"Authorization": f"Bearer {plaintext}"},
        )

    assert resp.status_code == 200
    assert call_counter[0] >= 2, "应在 429 后重试上游"
    assert "data: [DONE]" in resp.text

    req_log = await db_session.execute(
        select(RequestLog).where(
            RequestLog.user_id == user.id,
            RequestLog.status == "success",
            RequestLog.is_stream.is_(True),
        )
    )
    assert req_log.scalar_one_or_none() is not None


@pytest.mark.asyncio
async def test_non_stream_retry_on_500_then_success(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    """非流式上游 500 重试后成功（故障转移逻辑共用）。"""
    from tests.test_openai_api import _make_upstream_success_response

    user = await _seed_user(db_session)
    _, plaintext = await _seed_api_key(db_session, user)
    model = await _seed_model(db_session)
    await _seed_billing_rule(db_session, model)
    _, channel, _ = await _seed_channel(db_session)
    await _seed_mapping(db_session, model, channel)
    await _seed_wallet(db_session, user, balance=100_000_000)

    from tests.test_openai_api import _make_upstream_error_response

    error_resp = _make_upstream_error_response(status_code=500)
    success_resp = _make_upstream_success_response()

    call_counter = [0]

    def _post_factory(*_args: Any, **_kwargs: Any) -> MagicMock:
        call_counter[0] += 1
        return error_resp if call_counter[0] == 1 else success_resp

    mock_client = MagicMock()
    mock_client.post = AsyncMock(side_effect=_post_factory)
    mock_client.aclose = AsyncMock()
    mock_client_cls = MagicMock()
    mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
    http_patch = patch("app.api.v1.chat.httpx.AsyncClient", mock_client_cls)

    with http_patch:
        resp = await api_client.post(
            "/v1/chat/completions",
            json={
                "model": model.alias,
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": False,
            },
            headers={"Authorization": f"Bearer {plaintext}"},
        )

    assert resp.status_code == 200
    assert call_counter[0] >= 2
