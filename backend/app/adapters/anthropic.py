"""Anthropic Messages API 适配器（私有协议，非 OpenAI-compatible）。

协议差异与处理方式：
  - 端点：``{base_url}/messages``（非 ``/chat/completions``）。
  - 鉴权：``x-api-key`` + ``anthropic-version`` 头（非 Bearer）。
  - system 消息：OpenAI 放在 messages 里，Anthropic 是顶层 ``system`` 字段。
  - max_tokens：Anthropic 必填，未指定时使用适配器默认值。
  - stop → ``stop_sequences``；temperature 范围 0~1（OpenAI 0~2，超出截断）。
  - 流式：Anthropic SSE 是带类型的事件流（message_start / content_block_delta /
    message_delta / message_stop ...），无 ``[DONE]``；本适配器把事件流转成
    OpenAI 兼容 chunk，网关在上游流结束后自行补发 ``data: [DONE]``。
  - usage：input_tokens / output_tokens 分散在 message_start 与 message_delta
    两个事件中，适配器实例内暂存 message_start 的输入侧数据，在最终
    message_delta 时合成完整 Usage 供计费。**因此流式解析有状态，
    本适配器在注册表中以工厂方式注册，每个请求独立实例。**
  - tool use：按任务要求不做完整映射——请求携带 tools / tool 消息时
    明确报错（invalid_request，400），不静默透传。
"""

from __future__ import annotations

import json
import time
from typing import Any

from app.adapters.base import BaseAdapter
from app.core.errors import AppError, InvalidRequestError
from app.schemas.openai import (
    ChatCompletionChoice,
    ChatCompletionChunkChoice,
    ChatCompletionChunkDelta,
    ChatCompletionMessage,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatCompletionStreamChunk,
    Usage,
)

# Anthropic stop_reason → OpenAI finish_reason
_STOP_REASON_MAP = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
}


def _map_stop_reason(stop_reason: str | None) -> str | None:
    if stop_reason is None:
        return None
    return _STOP_REASON_MAP.get(stop_reason, "stop")


class AnthropicAdapter(BaseAdapter):
    """Anthropic Messages API 原生协议适配器（流式 + 非流式）。

    非流式方法无状态；流式解析（``parse_stream_chunk``）跨事件持有状态，
    因此每个请求必须使用独立实例（注册表以工厂方式注册本类）。
    """

    API_VERSION = "2023-06-01"
    DEFAULT_MAX_TOKENS = 4096

    def __init__(self) -> None:
        # 流式解析状态：由 message_start 填充，供后续 chunk 与最终 usage 使用
        self._stream_id: str = ""
        self._stream_model: str = ""
        self._stream_created: int = 0
        self._stream_prompt_tokens: int = 0
        self._stream_cached_tokens: int = 0

    # ------------------------------------------------------------------
    # 协议差异：URL / 请求头 / 流式选项
    # ------------------------------------------------------------------

    def build_url(self, base_url: str) -> str:
        """Anthropic 端点为 ``{base_url}/messages``。"""
        return f"{base_url.rstrip('/')}/messages"

    def build_headers(self, upstream_key: str, request_id: str) -> dict[str, str]:
        """Anthropic 使用 x-api-key + anthropic-version 头。"""
        return {
            "x-api-key": upstream_key,
            "anthropic-version": self.API_VERSION,
            "Content-Type": "application/json",
            "X-Request-ID": request_id,
        }

    def apply_stream_options(self, payload: dict) -> None:
        """Anthropic 不支持 stream_options；usage 由事件流自带，无需额外选项。"""

    # ------------------------------------------------------------------
    # 构建上游请求：OpenAI 格式 → Anthropic Messages 格式
    # ------------------------------------------------------------------

    def build_request(
        self,
        std_req: ChatCompletionRequest,
        upstream_model: str,
    ) -> dict[str, Any]:
        if std_req.tools or std_req.tool_choice:
            raise InvalidRequestError(
                "Tool use is not supported by the anthropic adapter yet; "
                "remove 'tools' / 'tool_choice' or use an openai_compatible channel."
            )

        system_parts: list[str] = []
        messages: list[dict[str, Any]] = []

        for msg in std_req.messages:
            if msg.role in ("tool", "function") or msg.tool_calls:
                raise InvalidRequestError(
                    "Tool/function messages are not supported by the anthropic "
                    "adapter yet; remove them or use an openai_compatible channel."
                )
            text = self._extract_text(msg.content)
            if msg.role == "system":
                if text:
                    system_parts.append(text)
                continue
            # user / assistant
            messages.append({"role": msg.role, "content": text or ""})

        payload: dict[str, Any] = {
            "model": upstream_model,
            "messages": messages,
            "max_tokens": std_req.max_tokens or self.DEFAULT_MAX_TOKENS,
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        if std_req.temperature is not None:
            # OpenAI 范围 0~2，Anthropic 0~1，超出部分截断
            payload["temperature"] = min(std_req.temperature, 1.0)
        if std_req.top_p is not None:
            payload["top_p"] = std_req.top_p
        if std_req.stop is not None:
            payload["stop_sequences"] = (
                [std_req.stop] if isinstance(std_req.stop, str) else list(std_req.stop)
            )
        if std_req.user:
            payload["metadata"] = {"user_id": std_req.user}
        return payload

    @staticmethod
    def _extract_text(content: str | list[Any] | None) -> str:
        """提取消息文本；多模态分段仅支持 text 类型，其余明确报错。"""
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(str(part.get("text", "")))
            else:
                raise InvalidRequestError(
                    "Only text content parts are supported by the anthropic adapter."
                )
        return "".join(parts)

    # ------------------------------------------------------------------
    # 非流式响应解析：Anthropic → OpenAI 格式
    # ------------------------------------------------------------------

    def parse_response(self, raw: dict[str, Any]) -> ChatCompletionResponse:
        text = "".join(
            block.get("text", "")
            for block in raw.get("content", [])
            if isinstance(block, dict) and block.get("type") == "text"
        )
        return ChatCompletionResponse(
            id=raw.get("id", ""),
            object="chat.completion",
            created=int(time.time()),  # Anthropic 响应无 created 字段
            model=raw.get("model", ""),
            choices=[
                ChatCompletionChoice(
                    index=0,
                    message=ChatCompletionMessage(role="assistant", content=text),
                    finish_reason=_map_stop_reason(raw.get("stop_reason")),
                )
            ],
            usage=self.parse_usage(raw),
        )

    # ------------------------------------------------------------------
    # Usage 换算：input/output_tokens → prompt/completion_tokens
    # ------------------------------------------------------------------

    def parse_usage(self, raw: dict[str, Any]) -> Usage | None:
        """Anthropic 的 input_tokens 不含缓存部分，换算为 OpenAI 口径：

        prompt_tokens = input + cache_read + cache_creation（与 OpenAI 一致，
        prompt 含缓存命中部分），缓存命中数放入 prompt_tokens_details.cached_tokens
        供计费按缓存单价折算。
        """
        usage_raw = raw.get("usage")
        if not usage_raw:
            return None
        input_tokens = usage_raw.get("input_tokens", 0)
        output_tokens = usage_raw.get("output_tokens", 0)
        cache_read = usage_raw.get("cache_read_input_tokens", 0) or 0
        cache_creation = usage_raw.get("cache_creation_input_tokens", 0) or 0
        prompt_tokens = input_tokens + cache_read + cache_creation
        return Usage(
            prompt_tokens=prompt_tokens,
            completion_tokens=output_tokens,
            total_tokens=prompt_tokens + output_tokens,
            prompt_tokens_details={"cached_tokens": cache_read} if cache_read else None,
        )

    # ------------------------------------------------------------------
    # 流式事件解析：Anthropic SSE 事件 → OpenAI chunk
    # ------------------------------------------------------------------

    def parse_stream_chunk(self, data: str) -> ChatCompletionStreamChunk | None:
        """按事件 ``type`` 字段分发（网关只透传 ``data:`` 行，event 行被忽略，
        而 Anthropic 每个 data 负载自带 type，无需依赖 event 行）。

        - message_start   → 暂存 id/model/输入侧 usage，产出 role chunk
        - content_block_delta(text_delta) → 文本增量 chunk
        - message_delta   → finish_reason + 合成完整 usage 的最终 chunk
        - ping / content_block_start / content_block_stop / message_stop → 跳过
        - error           → 抛出平台标准错误（网关按流中断处理）
        """
        stripped = data.strip()
        if stripped == "[DONE]":
            return None
        try:
            raw = json.loads(stripped)
        except json.JSONDecodeError:
            return None

        event_type = raw.get("type")

        if event_type == "message_start":
            message = raw.get("message", {})
            self._stream_id = message.get("id", "")
            self._stream_model = message.get("model", "")
            self._stream_created = int(time.time())
            usage_raw = message.get("usage", {}) or {}
            cache_read = usage_raw.get("cache_read_input_tokens", 0) or 0
            cache_creation = usage_raw.get("cache_creation_input_tokens", 0) or 0
            self._stream_prompt_tokens = (
                usage_raw.get("input_tokens", 0) + cache_read + cache_creation
            )
            self._stream_cached_tokens = cache_read
            return self._make_chunk(
                ChatCompletionChunkDelta(role="assistant"), finish_reason=None
            )

        if event_type == "content_block_delta":
            delta = raw.get("delta", {})
            if delta.get("type") != "text_delta":
                return None  # thinking / input_json 等增量不映射
            return self._make_chunk(
                ChatCompletionChunkDelta(content=delta.get("text", "")),
                finish_reason=None,
            )

        if event_type == "message_delta":
            output_tokens = (raw.get("usage") or {}).get("output_tokens", 0)
            usage = Usage(
                prompt_tokens=self._stream_prompt_tokens,
                completion_tokens=output_tokens,
                total_tokens=self._stream_prompt_tokens + output_tokens,
                prompt_tokens_details=(
                    {"cached_tokens": self._stream_cached_tokens}
                    if self._stream_cached_tokens
                    else None
                ),
            )
            stop_reason = (raw.get("delta") or {}).get("stop_reason")
            return self._make_chunk(
                ChatCompletionChunkDelta(),
                finish_reason=_map_stop_reason(stop_reason) or "stop",
                usage=usage,
            )

        if event_type == "error":
            # 流中途上游报错：抛出标准错误，网关按流异常结算 partial
            raise self.normalize_error(502, raw)

        # ping / content_block_start / content_block_stop / message_stop / 未知
        return None

    def _make_chunk(
        self,
        delta: ChatCompletionChunkDelta,
        *,
        finish_reason: str | None,
        usage: Usage | None = None,
    ) -> ChatCompletionStreamChunk:
        return ChatCompletionStreamChunk(
            id=self._stream_id,
            object="chat.completion.chunk",
            created=self._stream_created,
            model=self._stream_model,
            choices=[
                ChatCompletionChunkChoice(
                    index=0, delta=delta, finish_reason=finish_reason
                )
            ],
            usage=usage,
        )

    # ------------------------------------------------------------------
    # 错误归一化
    # ------------------------------------------------------------------

    def normalize_error(self, status_code: int, raw: dict[str, Any]) -> AppError:
        """Anthropic 错误结构：{"type": "error", "error": {"type": ..., "message": ...}}"""
        from app.core.errors import (
            InsufficientBalanceError,
            InvalidApiKeyError,
            ModelNotFoundError,
        )

        error_body = raw.get("error") or {}
        message = error_body.get("message", f"Upstream error (HTTP {status_code})")
        upstream_type = error_body.get("type", "")

        if status_code == 401:
            return InvalidApiKeyError(f"Upstream authentication failed: {message}")
        if status_code == 404:
            return ModelNotFoundError(f"Upstream model not found: {message}")
        if status_code == 402:
            return InsufficientBalanceError(f"Upstream billing error: {message}")
        if status_code == 400:
            return InvalidRequestError(f"Upstream rejected request: {message}")

        # 429 / 5xx → 通用上游错误，网关按重试策略处理
        return AppError(
            message,
            code=upstream_type or "upstream_error",
            error_type="upstream_error",
            status_code=status_code if status_code in (429, 500, 502, 503) else 502,
        )
