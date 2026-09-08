"""OpenAI-compatible 适配器。

覆盖所有兼容 OpenAI Chat Completions 协议的上游厂商：
OpenAI、DeepSeek、阿里通义（兼容模式）、本地 vLLM/Ollama 等。

转换逻辑：
  - 请求体几乎透传，仅替换 model 字段。
  - 非流式响应直接反序列化为平台 Schema。
  - 流式 SSE 解析 ``data: {...}`` 行，透传到调用方。
  - Usage 从响应 ``usage`` 字段提取。
  - 错误码映射到平台业务异常。
"""

from __future__ import annotations
import json
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
    EmbeddingData,
    EmbeddingRequest,
    EmbeddingResponse,
    EmbeddingUsage,
    Usage,
)


class OpenAICompatibleAdapter(BaseAdapter):
    """适用于所有兼容 OpenAI Chat Completions API 的上游厂商。

    实例无状态，可复用（线程/协程安全）。
    """

    # ------------------------------------------------------------------
    # 构建上游请求
    # ------------------------------------------------------------------

    def build_request(
        self,
        std_req: ChatCompletionRequest,
        upstream_model: str,
    ) -> dict[str, Any]:
        """将平台标准请求转换为上游请求体，仅替换 model 字段。

        透传所有 OpenAI 兼容参数（temperature、tools、response_format 等），
        上游会忽略它不支持的字段。
        """
        return std_req.build_upstream_payload(upstream_model)

    # ------------------------------------------------------------------
    # 非流式响应解析
    # ------------------------------------------------------------------

    def parse_response(self, raw: dict[str, Any]) -> ChatCompletionResponse:
        """将上游 JSON 响应体解析为平台标准 ChatCompletionResponse。"""
        choices = []
        for c in raw.get("choices", []):
            msg_raw = c.get("message", {})
            choices.append(
                ChatCompletionChoice(
                    index=c.get("index", 0),
                    message=ChatCompletionMessage(
                        role=msg_raw.get("role", "assistant"),
                        content=msg_raw.get("content"),
                        tool_calls=msg_raw.get("tool_calls"),
                        function_call=msg_raw.get("function_call"),
                    ),
                    finish_reason=c.get("finish_reason"),
                    logprobs=c.get("logprobs"),
                )
            )

        usage = self.parse_usage(raw)

        return ChatCompletionResponse(
            id=raw.get("id", ""),
            object=raw.get("object", "chat.completion"),
            created=raw.get("created", 0),
            model=raw.get("model", ""),
            choices=choices,
            usage=usage,
            system_fingerprint=raw.get("system_fingerprint"),
        )

    # ------------------------------------------------------------------
    # 流式 chunk 解析
    # ------------------------------------------------------------------

    def parse_stream_chunk(self, data: str) -> ChatCompletionStreamChunk | None:
        """解析 SSE ``data:`` 行的 JSON 内容。

        - ``data: [DONE]`` → 返回 None（流结束标志）。
        - 其他 JSON → 解析为平台标准流式 chunk。
        - 解析失败 → 返回 None（跳过该行）。
        """
        stripped = data.strip()
        if stripped == "[DONE]":
            return None

        try:
            raw = json.loads(stripped)
        except json.JSONDecodeError:
            return None

        choices = []
        for c in raw.get("choices", []):
            delta_raw = c.get("delta", {})
            choices.append(
                ChatCompletionChunkChoice(
                    index=c.get("index", 0),
                    delta=ChatCompletionChunkDelta(
                        role=delta_raw.get("role"),
                        content=delta_raw.get("content"),
                        tool_calls=delta_raw.get("tool_calls"),
                        function_call=delta_raw.get("function_call"),
                    ),
                    finish_reason=c.get("finish_reason"),
                    logprobs=c.get("logprobs"),
                )
            )

        usage = self.parse_usage(raw)

        return ChatCompletionStreamChunk(
            id=raw.get("id", ""),
            object=raw.get("object", "chat.completion.chunk"),
            created=raw.get("created", 0),
            model=raw.get("model", ""),
            choices=choices,
            usage=usage,
            system_fingerprint=raw.get("system_fingerprint"),
        )

    # ------------------------------------------------------------------
    # Usage 提取
    # ------------------------------------------------------------------

    def parse_usage(self, raw: dict[str, Any]) -> Usage | None:
        """从响应体（非流式或流式最终 chunk）的 ``usage`` 字段提取 Usage。"""
        usage_raw = raw.get("usage")
        if not usage_raw:
            return None
        return Usage(
            prompt_tokens=usage_raw.get("prompt_tokens", 0),
            completion_tokens=usage_raw.get("completion_tokens", 0),
            total_tokens=usage_raw.get("total_tokens", 0),
            prompt_tokens_details=usage_raw.get("prompt_tokens_details"),
            completion_tokens_details=usage_raw.get("completion_tokens_details"),
        )

    # ------------------------------------------------------------------
    # Embeddings
    # ------------------------------------------------------------------

    def supports_embeddings(self) -> bool:
        return True

    def build_embeddings_url(self, base_url: str) -> str:
        """OpenAI 风格 ``{base_url}/embeddings``。"""
        return f"{base_url.rstrip('/')}/embeddings"

    def build_embeddings_request(
        self,
        std_req: EmbeddingRequest,
        upstream_model: str,
    ) -> dict[str, Any]:
        """透传 embeddings 请求，仅替换 model 字段。"""
        return std_req.build_upstream_payload(upstream_model)

    def parse_embeddings_response(self, raw: dict[str, Any]) -> EmbeddingResponse:
        """将上游 embeddings JSON 解析为平台标准 EmbeddingResponse。"""
        data = [
            EmbeddingData(
                object=item.get("object", "embedding"),
                embedding=item.get("embedding", []),
                index=item.get("index", i),
            )
            for i, item in enumerate(raw.get("data", []))
        ]
        usage = None
        usage_raw = raw.get("usage")
        if usage_raw:
            usage = EmbeddingUsage(
                prompt_tokens=usage_raw.get("prompt_tokens", 0),
                total_tokens=usage_raw.get("total_tokens", 0),
            )
        return EmbeddingResponse(
            object=raw.get("object", "list"),
            data=data,
            model=raw.get("model", ""),
            usage=usage,
        )

    # ------------------------------------------------------------------
    # 错误归一化
    # ------------------------------------------------------------------

    def normalize_error(self, status_code: int, raw: dict[str, Any]) -> AppError:
        """将上游错误响应映射为平台业务异常（不 raise，仅返回）。

        上游错误结构（OpenAI 标准）：
          {"error": {"message": "...", "type": "...", "code": "..."}}
        """
        from app.core.errors import (
            InsufficientBalanceError,
            InvalidApiKeyError,
            ModelNotFoundError,
        )

        error_body = raw.get("error", {})
        message = error_body.get("message", f"Upstream error (HTTP {status_code})")
        upstream_code = error_body.get("code", "")

        if status_code == 401:
            return InvalidApiKeyError(f"Upstream authentication failed: {message}")
        if status_code == 404:
            return ModelNotFoundError(f"Upstream model not found: {message}")
        if status_code == 402:
            return InsufficientBalanceError(f"Upstream billing error: {message}")

        # 429 / 5xx → 通用上游错误，调用方可选重试或直接返回
        return AppError(
            message,
            code=upstream_code or "upstream_error",
            error_type="upstream_error",
            status_code=status_code if status_code in (429, 500, 502, 503) else 502,
        )


# 全局单例，轻量无状态，可安全复用
openai_compatible_adapter = OpenAICompatibleAdapter()
