"""路由服务与适配器测试。

覆盖验收标准：
  - 禁用渠道不参与路由。
  - 高优先级渠道优先。
  - 同优先级按权重分布。
  - OpenAI-compatible 响应可转换为平台标准响应。
  - 流式 chunk 正确解析。
  - 上游错误映射到平台业务异常。
  - Mock 上游测试，不调用真实厂商。
"""

from __future__ import annotations

import json
from collections import Counter
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.adapters.openai_compatible import OpenAICompatibleAdapter, openai_compatible_adapter
from app.core.channel_key_crypto import encrypt_channel_key
from app.core.constants import STATUS_DISABLED, STATUS_ENABLED
from app.core.errors import AppError, ModelNotFoundError
from app.schemas.openai import (
    ChatCompletionRequest,
    ChatMessage,
    ChatCompletionResponse,
    ChatCompletionStreamChunk,
    Usage,
)
from app.services.routing_service import (
    HEALTH_BROKEN,
    HEALTH_OK,
    NoAvailableChannelError,
    RoutingService,
    _weighted_random,
)


# =========================================================================
# 辅助 Mock 构建器
# =========================================================================


def _make_key(
    *,
    weight: int = 1,
    status: int = STATUS_ENABLED,
    health_status: int = HEALTH_OK,
    plaintext: str = "sk-test-upstream-key",
    rpm_limit: int | None = None,
    tpm_limit: int | None = None,
) -> MagicMock:
    """构造模拟 ChannelKey 对象（不涉及 SQLAlchemy）。"""
    key = MagicMock()
    key.id = uuid4()
    key.weight = weight
    key.status = status
    key.health_status = health_status
    key.rpm_limit = rpm_limit
    key.tpm_limit = tpm_limit
    key.api_key_encrypted = encrypt_channel_key(plaintext)
    return key


def _make_channel(
    *,
    priority: int = 0,
    weight: int = 1,
    status: int = STATUS_ENABLED,
    health_status: int = HEALTH_OK,
    base_url: str = "https://api.openai.com/v1",
    adapter_type: str = "openai_compatible",
    keys: list[MagicMock] | None = None,
) -> MagicMock:
    """构造模拟 Channel 对象（含 channel_keys 列表）。"""
    channel = MagicMock()
    channel.id = uuid4()
    channel.name = f"channel-{channel.id}"
    channel.base_url = base_url
    channel.adapter_type = adapter_type
    channel.priority = priority
    channel.status = status
    channel.health_status = health_status
    channel.timeout_ms = 30_000
    channel.max_retries = 2
    channel.channel_keys = keys if keys is not None else [_make_key()]
    return channel


def _make_mapping(
    *,
    channel: MagicMock,
    weight: int = 1,
    status: int = STATUS_ENABLED,
    upstream_model_name: str = "gpt-4o",
) -> MagicMock:
    """构造模拟 ModelChannelMap 对象。"""
    mapping = MagicMock()
    mapping.id = uuid4()
    mapping.upstream_model_name = upstream_model_name
    mapping.weight = weight
    mapping.status = status
    mapping.channel = channel
    return mapping


def _make_model(alias: str = "gpt-4o") -> MagicMock:
    """构造模拟 LLMModel 对象。"""
    model = MagicMock()
    model.id = uuid4()
    model.alias = alias
    model.status = STATUS_ENABLED
    return model


# =========================================================================
# _weighted_random 单元测试（纯 Python，无依赖）
# =========================================================================


class TestWeightedRandom:
    def test_single_item_always_selected(self) -> None:
        item = object()
        assert _weighted_random([(item, 10)]) is item

    def test_returns_one_of_candidates(self) -> None:
        items = ["a", "b", "c"]
        result = _weighted_random([(i, 1) for i in items])
        assert result in items

    def test_weight_distribution_approximates_ratio(self) -> None:
        """权重 3:1 时，A 被选中的频率应约为 75%（误差 ±10%）。"""
        counts: Counter[str] = Counter()
        for _ in range(4000):
            r = _weighted_random([("A", 3), ("B", 1)])
            counts[r] += 1
        ratio_a = counts["A"] / 4000
        assert 0.65 <= ratio_a <= 0.85, f"ratio_A={ratio_a:.3f}"

    def test_zero_weight_edge_case(self) -> None:
        """权重为 0 时不应被选中（实际权重 1:0 时只有 A 可被选）。"""
        counts: Counter[str] = Counter()
        for _ in range(100):
            r = _weighted_random([("A", 1), ("B", 0)])
            counts[r] += 1
        # 权重 0 的 B 理论上永远不被选中（但由于浮点边界，允许极少数）
        assert counts["B"] == 0 or counts["B"] <= 2


# =========================================================================
# RoutingService 单元测试（Mock Session，不查数据库）
# =========================================================================


class TestRoutingServiceUnit:
    """使用 Mock 模拟数据库层，验证路由逻辑的核心决策过程。"""

    def setup_method(self) -> None:
        self.svc = RoutingService()

    # ------------------------------------------------------------------
    # 辅助：构造 session mock 返回指定映射列表
    # ------------------------------------------------------------------

    def _patch_load(self, model: MagicMock, mappings: list[MagicMock]):
        """Patch _get_enabled_model 和 _load_candidates，使其直接返回 mock 数据。"""
        self.svc._get_enabled_model = AsyncMock(return_value=model)
        self.svc._load_candidates = AsyncMock(
            return_value=[
                type("MC", (), {"mapping": m, "channel": m.channel})()
                for m in mappings
            ]
        )

    # ------------------------------------------------------------------
    # 禁用渠道不参与路由
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_disabled_channel_excluded_from_candidates(self) -> None:
        """status=DISABLED 的渠道不应出现在候选列表中。"""
        model = _make_model()
        disabled_channel = _make_channel(status=STATUS_DISABLED)
        mapping = _make_mapping(channel=disabled_channel)

        svc = RoutingService()
        # 直接测试 _is_channel_available 与 _has_available_keys 的过滤逻辑
        assert not svc._is_channel_available(disabled_channel)

    @pytest.mark.asyncio
    async def test_broken_channel_excluded(self) -> None:
        """health_status=BROKEN 的渠道不应参与路由。"""
        broken_channel = _make_channel(health_status=HEALTH_BROKEN)
        svc = RoutingService()
        assert not svc._is_channel_available(broken_channel)

    @pytest.mark.asyncio
    async def test_channel_with_all_disabled_keys_excluded(self) -> None:
        """渠道下所有 Key 均禁用时，该渠道视为不可用。"""
        disabled_key = _make_key(status=STATUS_DISABLED)
        channel = _make_channel(keys=[disabled_key])
        svc = RoutingService()
        assert not svc._has_available_keys(channel)

    @pytest.mark.asyncio
    async def test_channel_with_broken_keys_excluded(self) -> None:
        """渠道下所有 Key 均熔断时，该渠道视为不可用。"""
        broken_key = _make_key(health_status=HEALTH_BROKEN)
        channel = _make_channel(keys=[broken_key])
        svc = RoutingService()
        assert not svc._has_available_keys(channel)

    # ------------------------------------------------------------------
    # 高优先级渠道优先
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_high_priority_channel_preferred(self) -> None:
        """存在高低两个优先级渠道时，始终从高优先级组内选出结果。"""
        high_key = _make_key(plaintext="sk-high-key")
        low_key = _make_key(plaintext="sk-low-key")
        high_channel = _make_channel(priority=10, keys=[high_key])
        low_channel = _make_channel(priority=1, keys=[low_key])
        high_mapping = _make_mapping(channel=high_channel, upstream_model_name="model-high")
        low_mapping = _make_mapping(channel=low_channel, upstream_model_name="model-low")

        from app.services.routing_service import _MappingWithChannel
        candidates = [
            _MappingWithChannel(mapping=high_mapping, channel=high_channel),
            _MappingWithChannel(mapping=low_mapping, channel=low_channel),
        ]
        svc = RoutingService()
        # 多次选择，应始终选出高优先级渠道
        for _ in range(50):
            selected = await svc._select_channel(candidates)
            assert selected.channel.priority == 10

    # ------------------------------------------------------------------
    # 同优先级按权重分布
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_same_priority_weighted_distribution(self) -> None:
        """同优先级下，权重 9:1 时渠道 A 被选中约 90% 的次数。"""
        from app.services.routing_service import _MappingWithChannel

        key_a = _make_key(plaintext="sk-a")
        key_b = _make_key(plaintext="sk-b")
        channel_a = _make_channel(priority=0, keys=[key_a])
        channel_b = _make_channel(priority=0, keys=[key_b])
        mapping_a = _make_mapping(channel=channel_a, weight=9, upstream_model_name="model-a")
        mapping_b = _make_mapping(channel=channel_b, weight=1, upstream_model_name="model-b")

        candidates = [
            _MappingWithChannel(mapping=mapping_a, channel=channel_a),
            _MappingWithChannel(mapping=mapping_b, channel=channel_b),
        ]
        svc = RoutingService()

        counts: Counter[str] = Counter()
        for _ in range(1000):
            selected = await svc._select_channel(candidates)
            counts[selected.channel.id] += 1

        ratio_a = counts[channel_a.id] / 1000
        assert 0.80 <= ratio_a <= 0.97, f"ratio_A={ratio_a:.3f}"

    # ------------------------------------------------------------------
    # 无候选时抛出 503
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_no_available_channel_raises_503(self) -> None:
        """无可用候选渠道时应抛出 NoAvailableChannelError（503）。"""
        svc = RoutingService()
        model = _make_model()

        async def mock_session():
            pass

        svc._get_enabled_model = AsyncMock(return_value=model)
        svc._load_candidates = AsyncMock(return_value=[])

        with pytest.raises(NoAvailableChannelError) as exc_info:
            await svc.pick_route(MagicMock(), "gpt-4o")
        assert exc_info.value.status_code == 503
        assert exc_info.value.code == "no_available_channel"

    # ------------------------------------------------------------------
    # pick_route 端到端（完全 Mock）
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_pick_route_returns_correct_candidate(self) -> None:
        """pick_route 应返回包含正确 upstream_key 和 upstream_model_name 的 RouteCandidate。"""
        from app.services.routing_service import _MappingWithChannel

        plain_key = "sk-real-upstream-key-abc"
        key = _make_key(plaintext=plain_key)
        channel = _make_channel(
            base_url="https://api.deepseek.com/v1",
            adapter_type="openai_compatible",
            keys=[key],
        )
        mapping = _make_mapping(channel=channel, upstream_model_name="deepseek-chat")
        model = _make_model("deepseek-chat")

        svc = RoutingService()
        svc._get_enabled_model = AsyncMock(return_value=model)
        svc._load_candidates = AsyncMock(
            return_value=[_MappingWithChannel(mapping=mapping, channel=channel)]
        )

        candidate = await svc.pick_route(MagicMock(), "deepseek-chat")

        assert candidate.upstream_key == plain_key
        assert candidate.upstream_model_name == "deepseek-chat"
        assert candidate.base_url == "https://api.deepseek.com/v1"
        assert candidate.adapter_type == "openai_compatible"


# =========================================================================
# OpenAICompatibleAdapter 单元测试（纯 Python，无 HTTP 请求）
# =========================================================================


class TestOpenAICompatibleAdapter:
    def setup_method(self) -> None:
        self.adapter = OpenAICompatibleAdapter()

    # ------------------------------------------------------------------
    # build_request
    # ------------------------------------------------------------------

    def test_build_request_replaces_model_name(self) -> None:
        """build_request 应将 model 替换为上游真实名称。"""
        req = ChatCompletionRequest(
            model="gpt-4o",
            messages=[ChatMessage(role="user", content="Hello")],
            temperature=0.7,
        )
        payload = self.adapter.build_request(req, upstream_model="gpt-4o-2024-11-20")
        assert payload["model"] == "gpt-4o-2024-11-20"
        assert payload["temperature"] == 0.7

    def test_build_request_excludes_none_fields(self) -> None:
        """未设置的可选字段不应出现在上游请求体中。"""
        req = ChatCompletionRequest(
            model="gpt-4o",
            messages=[ChatMessage(role="user", content="Hi")],
        )
        payload = self.adapter.build_request(req, upstream_model="gpt-4o")
        assert "temperature" not in payload
        assert "max_tokens" not in payload

    def test_build_request_passes_messages(self) -> None:
        """消息列表应原样透传到上游请求体。"""
        req = ChatCompletionRequest(
            model="gpt-4o",
            messages=[
                ChatMessage(role="system", content="You are helpful"),
                ChatMessage(role="user", content="Hello"),
            ],
        )
        payload = self.adapter.build_request(req, upstream_model="gpt-4o")
        assert len(payload["messages"]) == 2
        assert payload["messages"][0]["role"] == "system"

    def test_build_request_stream_flag(self) -> None:
        """stream=True 应透传到上游请求体。"""
        req = ChatCompletionRequest(
            model="gpt-4o",
            messages=[ChatMessage(role="user", content="Hi")],
            stream=True,
        )
        payload = self.adapter.build_request(req, upstream_model="gpt-4o")
        assert payload["stream"] is True

    # ------------------------------------------------------------------
    # parse_response（非流式）
    # ------------------------------------------------------------------

    def _mock_response_raw(
        self,
        content: str = "Hello!",
        prompt_tokens: int = 10,
        completion_tokens: int = 5,
    ) -> dict:
        return {
            "id": "chatcmpl-abc123",
            "object": "chat.completion",
            "created": 1720000000,
            "model": "gpt-4o-2024-11-20",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": content,
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        }

    def test_parse_response_basic(self) -> None:
        """parse_response 应正确提取 message.content 和 usage。"""
        raw = self._mock_response_raw(content="你好！", prompt_tokens=12, completion_tokens=8)
        resp = self.adapter.parse_response(raw)

        assert isinstance(resp, ChatCompletionResponse)
        assert resp.id == "chatcmpl-abc123"
        assert resp.choices[0].message.content == "你好！"
        assert resp.choices[0].finish_reason == "stop"
        assert resp.usage is not None
        assert resp.usage.prompt_tokens == 12
        assert resp.usage.completion_tokens == 8
        assert resp.usage.total_tokens == 20

    def test_parse_response_no_usage(self) -> None:
        """上游未返回 usage 时，响应中 usage 为 None。"""
        raw = self._mock_response_raw()
        del raw["usage"]
        resp = self.adapter.parse_response(raw)
        assert resp.usage is None

    def test_parse_response_multiple_choices(self) -> None:
        """n>1 时，多个 choices 均被正确解析。"""
        raw = {
            "id": "chatcmpl-multi",
            "object": "chat.completion",
            "created": 1720000000,
            "model": "gpt-4o",
            "choices": [
                {"index": 0, "message": {"role": "assistant", "content": "A"}, "finish_reason": "stop"},
                {"index": 1, "message": {"role": "assistant", "content": "B"}, "finish_reason": "stop"},
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 4, "total_tokens": 9},
        }
        resp = self.adapter.parse_response(raw)
        assert len(resp.choices) == 2
        assert resp.choices[1].message.content == "B"

    # ------------------------------------------------------------------
    # parse_stream_chunk（流式）
    # ------------------------------------------------------------------

    def test_parse_stream_chunk_done(self) -> None:
        """[DONE] 标记应返回 None（流结束）。"""
        result = self.adapter.parse_stream_chunk("[DONE]")
        assert result is None

    def test_parse_stream_chunk_content(self) -> None:
        """正常 chunk 应正确解析为 ChatCompletionStreamChunk。"""
        data = json.dumps({
            "id": "chatcmpl-stream001",
            "object": "chat.completion.chunk",
            "created": 1720000000,
            "model": "gpt-4o",
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": "Hello"},
                    "finish_reason": None,
                }
            ],
        })
        chunk = self.adapter.parse_stream_chunk(data)
        assert isinstance(chunk, ChatCompletionStreamChunk)
        assert chunk.choices[0].delta.content == "Hello"
        assert chunk.choices[0].finish_reason is None

    def test_parse_stream_chunk_finish_reason(self) -> None:
        """finish_reason='stop' 的最终 chunk 应被正确解析。"""
        data = json.dumps({
            "id": "chatcmpl-stream001",
            "object": "chat.completion.chunk",
            "created": 1720000000,
            "model": "gpt-4o",
            "choices": [
                {
                    "index": 0,
                    "delta": {},
                    "finish_reason": "stop",
                }
            ],
        })
        chunk = self.adapter.parse_stream_chunk(data)
        assert chunk is not None
        assert chunk.choices[0].finish_reason == "stop"

    def test_parse_stream_chunk_with_usage(self) -> None:
        """最终 chunk 可携带 usage（部分厂商实现）。"""
        data = json.dumps({
            "id": "chatcmpl-stream001",
            "object": "chat.completion.chunk",
            "created": 1720000000,
            "model": "gpt-4o",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
            },
        })
        chunk = self.adapter.parse_stream_chunk(data)
        assert chunk is not None
        assert chunk.usage is not None
        assert chunk.usage.prompt_tokens == 10

    def test_parse_stream_chunk_invalid_json_returns_none(self) -> None:
        """格式错误的 JSON 行应返回 None（跳过），不抛出异常。"""
        result = self.adapter.parse_stream_chunk("{invalid json}")
        assert result is None

    def test_parse_stream_chunk_empty_string(self) -> None:
        """空字符串（心跳行）应返回 None，不抛出异常。"""
        result = self.adapter.parse_stream_chunk("")
        assert result is None

    # ------------------------------------------------------------------
    # parse_usage
    # ------------------------------------------------------------------

    def test_parse_usage_present(self) -> None:
        raw = {"usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}}
        usage = self.adapter.parse_usage(raw)
        assert usage is not None
        assert usage.prompt_tokens == 100
        assert usage.completion_tokens == 50

    def test_parse_usage_missing(self) -> None:
        usage = self.adapter.parse_usage({})
        assert usage is None

    def test_parse_usage_cached_tokens(self) -> None:
        """prompt_tokens_details.cached_tokens 应能通过 Usage.cached_tokens 访问。"""
        raw = {
            "usage": {
                "prompt_tokens": 200,
                "completion_tokens": 50,
                "total_tokens": 250,
                "prompt_tokens_details": {"cached_tokens": 120},
            }
        }
        usage = self.adapter.parse_usage(raw)
        assert usage is not None
        assert usage.cached_tokens == 120

    # ------------------------------------------------------------------
    # normalize_error
    # ------------------------------------------------------------------

    def test_normalize_error_401(self) -> None:
        """上游 401 应映射为 InvalidApiKeyError。"""
        from app.core.errors import InvalidApiKeyError
        err = self.adapter.normalize_error(401, {"error": {"message": "Invalid API key"}})
        assert isinstance(err, InvalidApiKeyError)
        assert err.status_code == 401

    def test_normalize_error_404(self) -> None:
        """上游 404 应映射为 ModelNotFoundError。"""
        from app.core.errors import ModelNotFoundError
        err = self.adapter.normalize_error(404, {"error": {"message": "Model not found"}})
        assert isinstance(err, ModelNotFoundError)

    def test_normalize_error_429(self) -> None:
        """上游 429 应返回通用 AppError（code=upstream_error 或原始 code）。"""
        err = self.adapter.normalize_error(
            429,
            {"error": {"message": "Rate limit exceeded", "code": "rate_limit_exceeded"}},
        )
        assert isinstance(err, AppError)
        assert err.status_code == 429

    def test_normalize_error_500(self) -> None:
        """上游 500 应返回通用 AppError，状态码映射为 500。"""
        err = self.adapter.normalize_error(500, {"error": {"message": "Internal Server Error"}})
        assert isinstance(err, AppError)
        assert err.status_code == 500

    # ------------------------------------------------------------------
    # ChatCompletionRequest.build_upstream_payload
    # ------------------------------------------------------------------

    def test_upstream_payload_replaces_model(self) -> None:
        req = ChatCompletionRequest(
            model="gpt-4o",
            messages=[ChatMessage(role="user", content="Hello")],
        )
        payload = req.build_upstream_payload("gpt-4o-2024-08-06")
        assert payload["model"] == "gpt-4o-2024-08-06"

    def test_upstream_payload_excludes_none(self) -> None:
        req = ChatCompletionRequest(
            model="gpt-4o",
            messages=[ChatMessage(role="user", content="Hi")],
        )
        payload = req.build_upstream_payload("gpt-4o")
        assert "max_tokens" not in payload
        assert "stop" not in payload


# =========================================================================
# RoutingService 数据库集成测试（需要真实 PostgreSQL）
# =========================================================================


@pytest.mark.asyncio
async def test_pick_route_disabled_channel_excluded(db_session) -> None:
    """禁用渠道不参与路由——集成测试。"""
    from app.db.models.channel import Channel, ChannelKey, Provider
    from app.db.models.llm_model import LLMModel, ModelChannelMap

    provider = Provider(name=f"prov-{uuid4().hex[:6]}", adapter_type="openai_compatible")
    db_session.add(provider)
    await db_session.flush()

    # 创建对外模型
    model = LLMModel(
        alias=f"test-disabled-{uuid4().hex[:6]}",
        display_name="Test Model",
        status=STATUS_ENABLED,
    )
    db_session.add(model)
    await db_session.flush()

    # 创建一个 禁用 渠道
    disabled_channel = Channel(
        provider_id=provider.id,
        name="disabled-channel",
        base_url="https://api.example.com/v1",
        adapter_type="openai_compatible",
        priority=0,
        weight=1,
        status=STATUS_DISABLED,   # 禁用
        health_status=HEALTH_OK,
        timeout_ms=30_000,
        max_retries=2,
    )
    db_session.add(disabled_channel)
    await db_session.flush()

    key = ChannelKey(
        channel_id=disabled_channel.id,
        api_key_encrypted=encrypt_channel_key("sk-test"),
        key_masked="****test",
        weight=1,
        status=STATUS_ENABLED,
        health_status=HEALTH_OK,
    )
    db_session.add(key)
    await db_session.flush()

    mapping = ModelChannelMap(
        model_id=model.id,
        channel_id=disabled_channel.id,
        upstream_model_name="gpt-4o",
        weight=1,
        status=STATUS_ENABLED,
    )
    db_session.add(mapping)
    await db_session.flush()

    svc = RoutingService()
    with pytest.raises(NoAvailableChannelError):
        await svc.pick_route(db_session, model.alias)


@pytest.mark.asyncio
async def test_pick_route_priority_order(db_session) -> None:
    """高优先级渠道应始终优先被路由选中。"""
    from app.db.models.channel import Channel, ChannelKey, Provider
    from app.db.models.llm_model import LLMModel, ModelChannelMap

    provider = Provider(name=f"prov-prio-{uuid4().hex[:6]}", adapter_type="openai_compatible")
    db_session.add(provider)
    await db_session.flush()

    model = LLMModel(
        alias=f"test-prio-{uuid4().hex[:6]}",
        display_name="Test Priority Model",
        status=STATUS_ENABLED,
    )
    db_session.add(model)
    await db_session.flush()

    high_key_plain = "sk-high-priority"
    low_key_plain = "sk-low-priority"

    def _make_db_channel(priority: int, name: str) -> Channel:
        return Channel(
            provider_id=provider.id,
            name=name,
            base_url=f"https://{name}.example.com/v1",
            adapter_type="openai_compatible",
            priority=priority,
            weight=1,
            status=STATUS_ENABLED,
            health_status=HEALTH_OK,
            timeout_ms=30_000,
            max_retries=2,
        )

    high_channel = _make_db_channel(priority=10, name="high-channel")
    low_channel = _make_db_channel(priority=1, name="low-channel")
    db_session.add_all([high_channel, low_channel])
    await db_session.flush()

    for channel, plain in [(high_channel, high_key_plain), (low_channel, low_key_plain)]:
        ck = ChannelKey(
            channel_id=channel.id,
            api_key_encrypted=encrypt_channel_key(plain),
            key_masked=f"****{plain[-4:]}",
            weight=1,
            status=STATUS_ENABLED,
            health_status=HEALTH_OK,
        )
        db_session.add(ck)

    await db_session.flush()

    for channel, name in [(high_channel, "high-model"), (low_channel, "low-model")]:
        m = ModelChannelMap(
            model_id=model.id,
            channel_id=channel.id,
            upstream_model_name=name,
            weight=1,
            status=STATUS_ENABLED,
        )
        db_session.add(m)

    await db_session.flush()

    svc = RoutingService()
    for _ in range(5):
        candidate = await svc.pick_route(db_session, model.alias)
        assert candidate.upstream_key == high_key_plain, (
            "应始终路由到高优先级渠道"
        )
        assert candidate.upstream_model_name == "high-model"


@pytest.mark.asyncio
async def test_pick_route_weighted_distribution(db_session) -> None:
    """同优先级下，路由分布应符合渠道 mapping 的权重比例（集成测试）。"""
    from app.db.models.channel import Channel, ChannelKey, Provider
    from app.db.models.llm_model import LLMModel, ModelChannelMap

    provider = Provider(name=f"prov-weight-{uuid4().hex[:6]}", adapter_type="openai_compatible")
    db_session.add(provider)
    await db_session.flush()

    model = LLMModel(
        alias=f"test-weight-{uuid4().hex[:6]}",
        display_name="Test Weight Model",
        status=STATUS_ENABLED,
    )
    db_session.add(model)
    await db_session.flush()

    def _make_db_channel_with_key(name: str, plain_key: str) -> tuple[Channel, ChannelKey]:
        ch = Channel(
            provider_id=provider.id,
            name=name,
            base_url=f"https://{name}.example.com/v1",
            adapter_type="openai_compatible",
            priority=0,
            weight=1,
            status=STATUS_ENABLED,
            health_status=HEALTH_OK,
            timeout_ms=30_000,
            max_retries=2,
        )
        db_session.add(ch)
        return ch

    channel_a = _make_db_channel_with_key("channel-a", "sk-a")
    channel_b = _make_db_channel_with_key("channel-b", "sk-b")
    await db_session.flush()

    for channel, plain in [(channel_a, "sk-a"), (channel_b, "sk-b")]:
        ck = ChannelKey(
            channel_id=channel.id,
            api_key_encrypted=encrypt_channel_key(plain),
            key_masked=f"****{plain[-2:]}",
            weight=1,
            status=STATUS_ENABLED,
            health_status=HEALTH_OK,
        )
        db_session.add(ck)

    await db_session.flush()

    # mapping_a 权重 3，mapping_b 权重 1
    map_a = ModelChannelMap(
        model_id=model.id, channel_id=channel_a.id,
        upstream_model_name="model-a", weight=3, status=STATUS_ENABLED,
    )
    map_b = ModelChannelMap(
        model_id=model.id, channel_id=channel_b.id,
        upstream_model_name="model-b", weight=1, status=STATUS_ENABLED,
    )
    db_session.add_all([map_a, map_b])
    await db_session.flush()

    svc = RoutingService()
    counts: Counter[str] = Counter()
    trials = 200
    for _ in range(trials):
        candidate = await svc.pick_route(db_session, model.alias)
        counts[candidate.upstream_model_name] += 1

    ratio_a = counts["model-a"] / trials
    assert 0.60 <= ratio_a <= 0.95, (
        f"权重 3:1 时 A 应占约 75%，实际 ratio_A={ratio_a:.3f}"
    )
