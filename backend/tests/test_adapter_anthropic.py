"""Anthropic Messages API 适配器测试（Phase2 任务 12）。

覆盖验收标准：
  - OpenAI 格式请求 → Anthropic 请求映射（system / max_tokens / stop_sequences）。
  - Anthropic 响应 / SSE 事件流 → OpenAI 兼容响应 / chunk。
  - usage 字段换算（input/output_tokens + 缓存字段 → prompt/completion_tokens）
    并正确参与计费。
  - 流式 chunk 连续且以 [DONE] 结束。
  - 上游错误映射为平台标准错误结构。
  - tool use 不做完整映射：明确报错（invalid_request），且释放预扣冻结。

测试策略与一期一致：Mock httpx 上游，不调用真实厂商。
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.anthropic import AnthropicAdapter
from app.adapters.registry import ADAPTER_TYPE_ANTHROPIC, get_adapter
from app.core.channel_key_crypto import encrypt_channel_key
from app.core.constants import STATUS_ENABLED
from app.core.errors import (
    AppError,
    InvalidApiKeyError,
    InvalidRequestError,
    ModelNotFoundError,
)
from app.db.models.channel import Channel, ChannelKey, Provider
from app.db.models.log import RequestLog, UsageLog
from app.db.models.wallet import WalletTransaction
from app.schemas.openai import ChatCompletionRequest
from tests.test_openai_api import (
    _patch_httpx,
    _seed_api_key,
    _seed_billing_rule,
    _seed_mapping,
    _seed_model,
    _seed_user,
    _seed_wallet,
)
from tests.test_openai_api import api_client as api_client  # noqa: F401  fixture 复用
from tests.test_stream_api import _MockStreamResponse, _patch_httpx_stream

# ===========================================================================
# 单元测试：注册表
# ===========================================================================


class TestAnthropicRegistry:
    def test_anthropic_registered(self) -> None:
        adapter = get_adapter(ADAPTER_TYPE_ANTHROPIC)
        assert isinstance(adapter, AnthropicAdapter)

    def test_factory_returns_fresh_instance_per_request(self) -> None:
        """流式解析有状态，每次 get_adapter 必须返回独立实例。"""
        a1 = get_adapter(ADAPTER_TYPE_ANTHROPIC)
        a2 = get_adapter(ADAPTER_TYPE_ANTHROPIC)
        assert a1 is not a2

    def test_openai_compatible_still_singleton(self) -> None:
        from app.adapters.openai_compatible import openai_compatible_adapter
        from app.adapters.registry import ADAPTER_TYPE_OPENAI_COMPATIBLE

        assert get_adapter(ADAPTER_TYPE_OPENAI_COMPATIBLE) is openai_compatible_adapter


# ===========================================================================
# 单元测试：URL / 请求头 / 流式选项
# ===========================================================================


class TestAnthropicTransport:
    def test_build_url_uses_messages_endpoint(self) -> None:
        adapter = AnthropicAdapter()
        assert (
            adapter.build_url("https://api.anthropic.com/v1")
            == "https://api.anthropic.com/v1/messages"
        )
        # 末尾斜杠归一化
        assert (
            adapter.build_url("https://api.anthropic.com/v1/")
            == "https://api.anthropic.com/v1/messages"
        )

    def test_build_headers_uses_x_api_key(self) -> None:
        adapter = AnthropicAdapter()
        headers = adapter.build_headers("sk-ant-test", "req-1")
        assert headers["x-api-key"] == "sk-ant-test"
        assert headers["anthropic-version"] == AnthropicAdapter.API_VERSION
        assert "Authorization" not in headers

    def test_apply_stream_options_is_noop(self) -> None:
        adapter = AnthropicAdapter()
        payload: dict[str, Any] = {"stream": True}
        adapter.apply_stream_options(payload)
        assert "stream_options" not in payload


# ===========================================================================
# 单元测试：请求映射
# ===========================================================================


def _std_request(**overrides: Any) -> ChatCompletionRequest:
    base: dict[str, Any] = {
        "model": "platform-alias",
        "messages": [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "你好"},
        ],
    }
    base.update(overrides)
    return ChatCompletionRequest(**base)


class TestAnthropicBuildRequest:
    def test_system_message_moved_to_top_level(self) -> None:
        adapter = AnthropicAdapter()
        payload = adapter.build_request(_std_request(), "claude-3-5-sonnet")

        assert payload["model"] == "claude-3-5-sonnet"
        assert payload["system"] == "You are helpful."
        # messages 中不再包含 system
        assert all(m["role"] != "system" for m in payload["messages"])
        assert payload["messages"] == [{"role": "user", "content": "你好"}]

    def test_max_tokens_defaults_when_missing(self) -> None:
        """Anthropic 的 max_tokens 是必填字段。"""
        adapter = AnthropicAdapter()
        payload = adapter.build_request(_std_request(), "claude-3-5-sonnet")
        assert payload["max_tokens"] == AnthropicAdapter.DEFAULT_MAX_TOKENS

        payload2 = adapter.build_request(_std_request(max_tokens=128), "m")
        assert payload2["max_tokens"] == 128

    def test_stop_mapped_to_stop_sequences(self) -> None:
        adapter = AnthropicAdapter()
        payload = adapter.build_request(_std_request(stop="END"), "m")
        assert payload["stop_sequences"] == ["END"]

        payload2 = adapter.build_request(_std_request(stop=["A", "B"]), "m")
        assert payload2["stop_sequences"] == ["A", "B"]

    def test_temperature_clamped_to_anthropic_range(self) -> None:
        adapter = AnthropicAdapter()
        payload = adapter.build_request(_std_request(temperature=1.8), "m")
        assert payload["temperature"] == 1.0

    def test_multimodal_text_parts_joined(self) -> None:
        adapter = AnthropicAdapter()
        req = _std_request(
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "第一段"},
                        {"type": "text", "text": "第二段"},
                    ],
                }
            ]
        )
        payload = adapter.build_request(req, "m")
        assert payload["messages"] == [{"role": "user", "content": "第一段第二段"}]

    def test_non_text_content_part_rejected(self) -> None:
        adapter = AnthropicAdapter()
        req = _std_request(
            messages=[
                {
                    "role": "user",
                    "content": [{"type": "image_url", "image_url": {"url": "x"}}],
                }
            ]
        )
        with pytest.raises(InvalidRequestError):
            adapter.build_request(req, "m")

    def test_tools_rejected_with_clear_error(self) -> None:
        """tool use 不做完整映射：明确报错而非静默透传。"""
        adapter = AnthropicAdapter()
        req = _std_request(tools=[{"type": "function", "function": {"name": "f"}}])
        with pytest.raises(InvalidRequestError) as exc_info:
            adapter.build_request(req, "m")
        assert "tool" in exc_info.value.message.lower()

    def test_tool_role_message_rejected(self) -> None:
        adapter = AnthropicAdapter()
        req = _std_request(
            messages=[
                {"role": "user", "content": "hi"},
                {"role": "tool", "content": "result", "tool_call_id": "t1"},
            ]
        )
        with pytest.raises(InvalidRequestError):
            adapter.build_request(req, "m")


# ===========================================================================
# 单元测试：非流式响应 / usage 映射
# ===========================================================================


def _anthropic_response(
    *,
    content: str = "Hello!",
    stop_reason: str = "end_turn",
    input_tokens: int = 25,
    output_tokens: int = 12,
    extra_usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    usage = {"input_tokens": input_tokens, "output_tokens": output_tokens}
    usage.update(extra_usage or {})
    return {
        "id": "msg_01ABC",
        "type": "message",
        "role": "assistant",
        "model": "claude-3-5-sonnet-20241022",
        "content": [{"type": "text", "text": content}],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": usage,
    }


class TestAnthropicParseResponse:
    def test_maps_to_openai_structure(self) -> None:
        adapter = AnthropicAdapter()
        resp = adapter.parse_response(_anthropic_response(content="你好！"))

        assert resp.object == "chat.completion"
        assert resp.id == "msg_01ABC"
        assert len(resp.choices) == 1
        assert resp.choices[0].message.role == "assistant"
        assert resp.choices[0].message.content == "你好！"
        assert resp.choices[0].finish_reason == "stop"

    def test_stop_reason_mapping(self) -> None:
        adapter = AnthropicAdapter()
        assert (
            adapter.parse_response(
                _anthropic_response(stop_reason="max_tokens")
            ).choices[0].finish_reason
            == "length"
        )
        assert (
            adapter.parse_response(
                _anthropic_response(stop_reason="stop_sequence")
            ).choices[0].finish_reason
            == "stop"
        )

    def test_usage_conversion(self) -> None:
        adapter = AnthropicAdapter()
        resp = adapter.parse_response(
            _anthropic_response(input_tokens=25, output_tokens=12)
        )
        assert resp.usage is not None
        assert resp.usage.prompt_tokens == 25
        assert resp.usage.completion_tokens == 12
        assert resp.usage.total_tokens == 37

    def test_usage_with_cache_fields(self) -> None:
        """缓存命中计入 prompt_tokens，并写入 details.cached_tokens 供计费折算。"""
        adapter = AnthropicAdapter()
        usage = adapter.parse_usage(
            _anthropic_response(
                input_tokens=10,
                output_tokens=5,
                extra_usage={
                    "cache_read_input_tokens": 100,
                    "cache_creation_input_tokens": 20,
                },
            )
        )
        assert usage is not None
        assert usage.prompt_tokens == 130
        assert usage.completion_tokens == 5
        assert usage.cached_tokens == 100


# ===========================================================================
# 单元测试：流式事件解析
# ===========================================================================


def _anthropic_stream_events(
    *,
    content_parts: tuple[str, ...] = ("你", "好"),
    input_tokens: int = 25,
    output_tokens: int = 12,
    stop_reason: str = "end_turn",
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = [
        {
            "type": "message_start",
            "message": {
                "id": "msg_stream_01",
                "type": "message",
                "role": "assistant",
                "model": "claude-3-5-sonnet-20241022",
                "content": [],
                "usage": {"input_tokens": input_tokens, "output_tokens": 1},
            },
        },
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": ""},
        },
    ]
    for part in content_parts:
        events.append(
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": part},
            }
        )
    events.extend(
        [
            {"type": "content_block_stop", "index": 0},
            {
                "type": "message_delta",
                "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                "usage": {"output_tokens": output_tokens},
            },
            {"type": "message_stop"},
        ]
    )
    return events


class TestAnthropicParseStreamChunk:
    def test_full_event_sequence(self) -> None:
        adapter = AnthropicAdapter()
        events = _anthropic_stream_events(
            content_parts=("你", "好"), input_tokens=25, output_tokens=12
        )
        chunks = [adapter.parse_stream_chunk(json.dumps(e)) for e in events]

        # message_start → role chunk
        assert chunks[0] is not None
        assert chunks[0].choices[0].delta.role == "assistant"
        assert chunks[0].id == "msg_stream_01"
        assert chunks[0].object == "chat.completion.chunk"

        # content_block_start → 跳过
        assert chunks[1] is None

        # content_block_delta → 文本增量
        assert chunks[2] is not None and chunks[2].choices[0].delta.content == "你"
        assert chunks[3] is not None and chunks[3].choices[0].delta.content == "好"
        # 后续 chunk 沿用 message_start 的 id
        assert chunks[2].id == "msg_stream_01"

        # content_block_stop → 跳过
        assert chunks[4] is None

        # message_delta → finish_reason + 合成完整 usage
        final = chunks[5]
        assert final is not None
        assert final.choices[0].finish_reason == "stop"
        assert final.usage is not None
        assert final.usage.prompt_tokens == 25
        assert final.usage.completion_tokens == 12
        assert final.usage.total_tokens == 37

        # message_stop → 跳过
        assert chunks[6] is None

    def test_max_tokens_stop_reason_maps_to_length(self) -> None:
        adapter = AnthropicAdapter()
        events = _anthropic_stream_events(stop_reason="max_tokens")
        final = None
        for e in events:
            chunk = adapter.parse_stream_chunk(json.dumps(e))
            if chunk is not None and chunk.choices[0].finish_reason:
                final = chunk
        assert final is not None
        assert final.choices[0].finish_reason == "length"

    def test_ping_and_unknown_events_skipped(self) -> None:
        adapter = AnthropicAdapter()
        assert adapter.parse_stream_chunk('{"type": "ping"}') is None
        assert adapter.parse_stream_chunk('{"type": "future_event"}') is None
        assert adapter.parse_stream_chunk("not-json") is None

    def test_error_event_raises_app_error(self) -> None:
        adapter = AnthropicAdapter()
        data = json.dumps(
            {
                "type": "error",
                "error": {"type": "overloaded_error", "message": "Overloaded"},
            }
        )
        with pytest.raises(AppError) as exc_info:
            adapter.parse_stream_chunk(data)
        assert exc_info.value.error_type == "upstream_error"
        assert "Overloaded" in exc_info.value.message


# ===========================================================================
# 单元测试：错误归一化
# ===========================================================================


class TestAnthropicNormalizeError:
    def _err_body(self, err_type: str, message: str) -> dict[str, Any]:
        return {"type": "error", "error": {"type": err_type, "message": message}}

    def test_401_maps_to_invalid_api_key(self) -> None:
        adapter = AnthropicAdapter()
        err = adapter.normalize_error(
            401, self._err_body("authentication_error", "invalid x-api-key")
        )
        assert isinstance(err, InvalidApiKeyError)
        assert "invalid x-api-key" in err.message

    def test_404_maps_to_model_not_found(self) -> None:
        adapter = AnthropicAdapter()
        err = adapter.normalize_error(
            404, self._err_body("not_found_error", "model: no-such-model")
        )
        assert isinstance(err, ModelNotFoundError)

    def test_400_maps_to_invalid_request(self) -> None:
        adapter = AnthropicAdapter()
        err = adapter.normalize_error(
            400, self._err_body("invalid_request_error", "max_tokens required")
        )
        assert isinstance(err, InvalidRequestError)

    def test_429_keeps_status_for_retry(self) -> None:
        adapter = AnthropicAdapter()
        err = adapter.normalize_error(
            429, self._err_body("rate_limit_error", "rate limited")
        )
        assert isinstance(err, AppError)
        assert err.status_code == 429
        assert err.error_type == "upstream_error"

    def test_unexpected_status_becomes_502(self) -> None:
        adapter = AnthropicAdapter()
        err = adapter.normalize_error(418, {})
        assert isinstance(err, AppError)
        assert err.status_code == 502


# ===========================================================================
# 集成测试：通过网关调用 anthropic 协议渠道（Mock 上游）
# ===========================================================================


async def _seed_anthropic_channel(
    session: AsyncSession,
) -> tuple[Provider, Channel, ChannelKey]:
    provider = Provider(
        name=f"ant-prov-{uuid4().hex[:6]}",
        adapter_type="anthropic",
        default_base_url="https://api.anthropic.com/v1",
    )
    session.add(provider)
    await session.flush()

    channel = Channel(
        provider_id=provider.id,
        name=f"ant-chan-{uuid4().hex[:6]}",
        base_url="https://api.anthropic.com/v1",
        adapter_type="anthropic",
        priority=0,
        weight=1,
        status=STATUS_ENABLED,
        health_status=0,
        timeout_ms=30_000,
        max_retries=2,
    )
    session.add(channel)
    await session.flush()

    channel_key = ChannelKey(
        channel_id=channel.id,
        api_key_encrypted=encrypt_channel_key("sk-ant-upstream-test"),
        key_masked="****ant",
        weight=1,
        status=STATUS_ENABLED,
        health_status=0,
    )
    session.add(channel_key)
    await session.flush()
    return provider, channel, channel_key


async def _seed_anthropic_env(db_session: AsyncSession) -> tuple[str, str, Any]:
    """返回 (api_key_plaintext, model_alias, user)。"""
    user = await _seed_user(db_session)
    _, plaintext = await _seed_api_key(db_session, user)
    model = await _seed_model(db_session)
    await _seed_billing_rule(db_session, model)
    _, channel, _ = await _seed_anthropic_channel(db_session)
    await _seed_mapping(
        db_session, model, channel, upstream_model_name="claude-3-5-sonnet-20241022"
    )
    await _seed_wallet(db_session, user, balance=100_000_000)
    return plaintext, model.alias, user


def _mock_anthropic_success(**kwargs: Any):
    from unittest.mock import MagicMock

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = _anthropic_response(**kwargs)
    return mock_resp


@pytest.mark.asyncio
async def test_non_stream_via_gateway_returns_openai_structure(
    api_client, db_session: AsyncSession
) -> None:
    """非流式：Anthropic 渠道返回结构可被 OpenAI SDK 解析，model 为平台 alias。"""
    plaintext, model_alias, _ = await _seed_anthropic_env(db_session)
    http_patch, mock_client = _patch_httpx(
        _mock_anthropic_success(content="你好！", input_tokens=25, output_tokens=12)
    )

    with http_patch:
        resp = await api_client.post(
            "/v1/chat/completions",
            json={
                "model": model_alias,
                "messages": [
                    {"role": "system", "content": "You are helpful."},
                    {"role": "user", "content": "你好"},
                ],
                "max_tokens": 100,
            },
            headers={"Authorization": f"Bearer {plaintext}"},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    # OpenAI 兼容结构
    assert body["object"] == "chat.completion"
    assert body["model"] == model_alias
    assert body["choices"][0]["message"]["role"] == "assistant"
    assert body["choices"][0]["message"]["content"] == "你好！"
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["usage"]["prompt_tokens"] == 25
    assert body["usage"]["completion_tokens"] == 12

    # 上游调用协议正确：/messages 端点 + x-api-key 头 + Anthropic 请求体
    call = mock_client.post.call_args
    assert call.args[0].endswith("/messages")
    headers = call.kwargs["headers"]
    assert headers["x-api-key"] == "sk-ant-upstream-test"
    assert headers["anthropic-version"] == AnthropicAdapter.API_VERSION
    payload = call.kwargs["json"]
    assert payload["system"] == "You are helpful."
    assert payload["max_tokens"] == 100
    assert payload["model"] == "claude-3-5-sonnet-20241022"
    assert "stream_options" not in payload


@pytest.mark.asyncio
async def test_non_stream_usage_participates_in_billing(
    api_client, db_session: AsyncSession
) -> None:
    """非流式：换算后的 usage 正确写入 usage_log 并产生 capture 流水。"""
    plaintext, model_alias, user = await _seed_anthropic_env(db_session)
    http_patch, _ = _patch_httpx(
        _mock_anthropic_success(input_tokens=30, output_tokens=15)
    )

    with http_patch:
        resp = await api_client.post(
            "/v1/chat/completions",
            json={
                "model": model_alias,
                "messages": [{"role": "user", "content": "你好"}],
                "max_tokens": 100,
            },
            headers={"Authorization": f"Bearer {plaintext}"},
        )
    assert resp.status_code == 200, resp.text

    req_log = (
        await db_session.execute(
            select(RequestLog).where(
                RequestLog.user_id == user.id, RequestLog.status == "success"
            )
        )
    ).scalar_one()

    usage_log = (
        await db_session.execute(
            select(UsageLog).where(UsageLog.request_id == req_log.request_id)
        )
    ).scalar_one()
    assert usage_log.input_tokens == 30
    assert usage_log.output_tokens == 15
    assert usage_log.is_estimated is False

    capture = (
        await db_session.execute(
            select(WalletTransaction).where(
                WalletTransaction.type == "capture",
                WalletTransaction.ref_request_id == req_log.request_id,
            )
        )
    ).scalar_one_or_none()
    assert capture is not None


def _anthropic_sse_lines(events: list[dict[str, Any]]) -> list[str]:
    """按 Anthropic SSE 格式生成行（event: 行 + data: 行，无 [DONE]）。"""
    lines: list[str] = []
    for event in events:
        lines.append(f"event: {event['type']}")
        lines.append(f"data: {json.dumps(event, ensure_ascii=False)}")
        lines.append("")
    return lines


@pytest.mark.asyncio
async def test_stream_via_gateway_emits_openai_chunks_and_done(
    api_client, db_session: AsyncSession
) -> None:
    """流式：事件流转为 OpenAI chunk，连续输出并以 [DONE] 结束。"""
    plaintext, model_alias, _ = await _seed_anthropic_env(db_session)
    lines = _anthropic_sse_lines(
        _anthropic_stream_events(content_parts=("你", "好", "！"))
    )
    http_patch, _ = _patch_httpx_stream([_MockStreamResponse(200, lines)])

    with http_patch:
        resp = await api_client.post(
            "/v1/chat/completions",
            json={
                "model": model_alias,
                "messages": [{"role": "user", "content": "你好"}],
                "stream": True,
                "max_tokens": 100,
            },
            headers={"Authorization": f"Bearer {plaintext}"},
        )

    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers.get("content-type", "")

    body = resp.text
    assert body.rstrip().endswith("data: [DONE]")

    # 每个 data 行都是合法的 OpenAI chunk JSON
    contents: list[str] = []
    finish_reasons: list[str] = []
    for line in body.splitlines():
        if not line.startswith("data:") or "[DONE]" in line:
            continue
        chunk = json.loads(line[len("data:") :])
        assert chunk["object"] == "chat.completion.chunk"
        assert chunk["model"] == model_alias
        for choice in chunk["choices"]:
            if choice["delta"].get("content"):
                contents.append(choice["delta"]["content"])
            if choice.get("finish_reason"):
                finish_reasons.append(choice["finish_reason"])
    assert "".join(contents) == "你好！"
    assert finish_reasons == ["stop"]


@pytest.mark.asyncio
async def test_stream_usage_participates_in_billing(
    api_client, db_session: AsyncSession
) -> None:
    """流式：message_start + message_delta 合成的 usage 正确参与计费。"""
    plaintext, model_alias, user = await _seed_anthropic_env(db_session)
    lines = _anthropic_sse_lines(
        _anthropic_stream_events(input_tokens=40, output_tokens=18)
    )
    http_patch, _ = _patch_httpx_stream([_MockStreamResponse(200, lines)])

    with http_patch:
        await api_client.post(
            "/v1/chat/completions",
            json={
                "model": model_alias,
                "messages": [{"role": "user", "content": "你好"}],
                "stream": True,
                "max_tokens": 100,
            },
            headers={"Authorization": f"Bearer {plaintext}"},
        )

    req_log = (
        await db_session.execute(
            select(RequestLog).where(
                RequestLog.user_id == user.id,
                RequestLog.status == "success",
                RequestLog.is_stream.is_(True),
            )
        )
    ).scalar_one()

    usage_log = (
        await db_session.execute(
            select(UsageLog).where(UsageLog.request_id == req_log.request_id)
        )
    ).scalar_one()
    assert usage_log.input_tokens == 40
    assert usage_log.output_tokens == 18
    assert usage_log.is_estimated is False

    capture = (
        await db_session.execute(
            select(WalletTransaction).where(
                WalletTransaction.type == "capture",
                WalletTransaction.ref_request_id == req_log.request_id,
            )
        )
    ).scalar_one_or_none()
    assert capture is not None


@pytest.mark.asyncio
async def test_upstream_error_mapped_to_platform_error(
    api_client, db_session: AsyncSession
) -> None:
    """上游 400 错误映射为平台标准错误结构，释放冻结且不计费。"""
    from unittest.mock import MagicMock

    plaintext, model_alias, user = await _seed_anthropic_env(db_session)

    mock_resp = MagicMock()
    mock_resp.status_code = 400
    mock_resp.json.return_value = {
        "type": "error",
        "error": {"type": "invalid_request_error", "message": "max_tokens required"},
    }
    http_patch, _ = _patch_httpx(mock_resp)

    with http_patch:
        resp = await api_client.post(
            "/v1/chat/completions",
            json={
                "model": model_alias,
                "messages": [{"role": "user", "content": "你好"}],
                "max_tokens": 100,
            },
            headers={"Authorization": f"Bearer {plaintext}"},
        )

    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["code"] == "invalid_request"
    assert "max_tokens required" in error["message"]

    req_log = (
        await db_session.execute(
            select(RequestLog).where(
                RequestLog.user_id == user.id, RequestLog.status == "failed"
            )
        )
    ).scalar_one()
    usage_log = (
        await db_session.execute(
            select(UsageLog).where(UsageLog.request_id == req_log.request_id)
        )
    ).scalar_one_or_none()
    assert usage_log is None

    release = (
        await db_session.execute(
            select(WalletTransaction).where(
                WalletTransaction.type == "release",
                WalletTransaction.ref_request_id == req_log.request_id,
            )
        )
    ).scalar_one_or_none()
    assert release is not None


@pytest.mark.asyncio
async def test_tool_use_request_rejected_and_hold_released(
    api_client, db_session: AsyncSession
) -> None:
    """携带 tools 的请求走 anthropic 渠道：明确报错 + 释放冻结，不打上游。"""
    plaintext, model_alias, user = await _seed_anthropic_env(db_session)
    http_patch, mock_client = _patch_httpx(_mock_anthropic_success())

    with http_patch:
        resp = await api_client.post(
            "/v1/chat/completions",
            json={
                "model": model_alias,
                "messages": [{"role": "user", "content": "你好"}],
                "max_tokens": 100,
                "tools": [
                    {
                        "type": "function",
                        "function": {"name": "get_weather", "parameters": {}},
                    }
                ],
            },
            headers={"Authorization": f"Bearer {plaintext}"},
        )

    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["code"] == "invalid_request"
    assert "tool" in error["message"].lower()
    # 未调用上游
    mock_client.post.assert_not_called()

    req_log = (
        await db_session.execute(
            select(RequestLog).where(
                RequestLog.user_id == user.id, RequestLog.status == "failed"
            )
        )
    ).scalar_one()
    release = (
        await db_session.execute(
            select(WalletTransaction).where(
                WalletTransaction.type == "release",
                WalletTransaction.ref_request_id == req_log.request_id,
            )
        )
    ).scalar_one_or_none()
    assert release is not None
