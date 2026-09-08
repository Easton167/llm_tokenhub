"""OpenAI 兼容请求/响应 Pydantic Schema。

对外接口完全兼容 OpenAI Chat Completions API，内部适配器也使用本 Schema
作为"平台标准格式"中转。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# 请求体
# ---------------------------------------------------------------------------


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool", "function"] = "user"
    content: str | list[Any] | None = None
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[Any] | None = None


class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[ChatMessage]
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    top_p: float | None = Field(default=None, ge=0.0, le=1.0)
    n: int | None = Field(default=None, ge=1)
    max_tokens: int | None = Field(default=None, ge=1)
    stream: bool = False
    stop: str | list[str] | None = None
    presence_penalty: float | None = Field(default=None, ge=-2.0, le=2.0)
    frequency_penalty: float | None = Field(default=None, ge=-2.0, le=2.0)
    logit_bias: dict[str, float] | None = None
    user: str | None = None
    tools: list[Any] | None = None
    tool_choice: Any | None = None
    response_format: dict[str, Any] | None = None
    seed: int | None = None
    extra_fields: dict[str, Any] = Field(default_factory=dict, exclude=True)

    model_config = {"extra": "allow"}

    def build_upstream_payload(self, upstream_model: str) -> dict[str, Any]:
        """构造上游请求体，将 model 替换为上游真实模型名，忽略平台专有字段。"""
        data = self.model_dump(
            exclude_none=True,
            exclude={"extra_fields"},
        )
        data["model"] = upstream_model
        return data


# ---------------------------------------------------------------------------
# 非流式响应
# ---------------------------------------------------------------------------


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    prompt_tokens_details: dict[str, Any] | None = None
    completion_tokens_details: dict[str, Any] | None = None

    @property
    def cached_tokens(self) -> int:
        """从 prompt_tokens_details.cached_tokens 提取缓存命中 token 数。"""
        if self.prompt_tokens_details:
            return self.prompt_tokens_details.get("cached_tokens", 0)
        return 0


class ChatCompletionMessage(BaseModel):
    role: str
    content: str | None = None
    tool_calls: list[Any] | None = None
    function_call: Any | None = None


class ChatCompletionChoice(BaseModel):
    index: int
    message: ChatCompletionMessage
    finish_reason: str | None = None
    logprobs: Any | None = None


class ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: list[ChatCompletionChoice]
    usage: Usage | None = None
    system_fingerprint: str | None = None

    model_config = {"extra": "allow"}


# ---------------------------------------------------------------------------
# 流式响应 chunk
# ---------------------------------------------------------------------------


class ChatCompletionChunkDelta(BaseModel):
    role: str | None = None
    content: str | None = None
    tool_calls: list[Any] | None = None
    function_call: Any | None = None


class ChatCompletionChunkChoice(BaseModel):
    index: int
    delta: ChatCompletionChunkDelta
    finish_reason: str | None = None
    logprobs: Any | None = None


class ChatCompletionStreamChunk(BaseModel):
    id: str
    object: str = "chat.completion.chunk"
    created: int
    model: str
    choices: list[ChatCompletionChunkChoice]
    usage: Usage | None = None
    system_fingerprint: str | None = None

    model_config = {"extra": "allow"}


# ---------------------------------------------------------------------------
# /v1/embeddings
# ---------------------------------------------------------------------------


class EmbeddingRequest(BaseModel):
    """OpenAI 兼容 embeddings 请求。

    ``input`` 支持字符串或字符串数组（一期必选）；整数 token id 数组亦透传上游。
    """

    model: str
    input: str | list[str] | list[int] | list[list[int]]
    encoding_format: Literal["float", "base64"] | None = None
    dimensions: int | None = Field(default=None, ge=1)
    user: str | None = None

    model_config = {"extra": "allow"}

    def build_upstream_payload(self, upstream_model: str) -> dict[str, Any]:
        """构造上游请求体，将 model 替换为上游真实模型名。"""
        data = self.model_dump(exclude_none=True)
        data["model"] = upstream_model
        return data


class EmbeddingData(BaseModel):
    object: str = "embedding"
    embedding: list[float] | str
    index: int = 0


class EmbeddingUsage(BaseModel):
    """Embeddings 用量：仅输入侧 token（无 completion_tokens）。"""

    prompt_tokens: int = 0
    total_tokens: int = 0


class EmbeddingResponse(BaseModel):
    object: str = "list"
    data: list[EmbeddingData]
    model: str
    usage: EmbeddingUsage | None = None

    model_config = {"extra": "allow"}


# ---------------------------------------------------------------------------
# /v1/models 列表
# ---------------------------------------------------------------------------


class ModelInfo(BaseModel):
    id: str
    object: str = "model"
    created: int = 0
    owned_by: str = "platform"
    model_type: str = "chat"
    capabilities: dict = Field(default_factory=dict)


class ModelListResponse(BaseModel):
    object: str = "list"
    data: list[ModelInfo]
