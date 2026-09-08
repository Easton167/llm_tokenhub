"""适配器抽象基类：定义平台标准格式与上游协议之间转换的接口契约。

每种 Provider 类型（openai_compatible / anthropic / ...）各自实现一个子类，
核心路由与计费逻辑只依赖本接口，无需感知具体厂商协议。
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, AsyncIterator

if TYPE_CHECKING:
    from app.schemas.openai import (
        ChatCompletionRequest,
        ChatCompletionResponse,
        ChatCompletionStreamChunk,
        EmbeddingRequest,
        EmbeddingResponse,
        Usage,
    )


@dataclass
class RouteCandidate:
    """路由结果：包含选定的渠道信息与解密后的上游 Key。

    生命周期短：只存在于单次网关调用。upstream_key 是明文，禁止写入日志/Redis。
    adapter_type 决定 get_adapter() 拿哪套协议；upstream_model_name 替换请求体里的平台 alias。
    """

    channel_id: str
    channel_name: str
    base_url: str
    adapter_type: str
    timeout_ms: int
    max_retries: int
    upstream_key: str           # 解密后的明文上游 API Key
    upstream_model_name: str    # 上游真实模型名
    channel_key_id: str         # 供健康状态标记使用


@dataclass
class UpstreamResponse:
    """适配器返回给调用方的统一响应封装。"""

    response: "ChatCompletionResponse"
    usage: "Usage"
    raw: dict = field(default_factory=dict)


@dataclass
class UpstreamStreamChunk:
    """流式场景中每个 chunk 的统一封装。"""

    chunk: "ChatCompletionStreamChunk"
    raw: dict = field(default_factory=dict)
    is_final: bool = False


class BaseAdapter(abc.ABC):
    """适配器抽象接口。

    子类必须实现：
    - ``build_request``：将平台标准请求转换为上游协议请求体。
    - ``parse_response``：将上游非流式响应体解析为平台标准响应。
    - ``parse_stream_chunk``：将上游 SSE 行（data 部分）解析为平台标准流式 chunk。
    - ``parse_usage``：从上游响应体（或流式最终 chunk）中提取 Usage。
    - ``normalize_error``：将上游错误响应转换为平台标准 AppError。

    子类可选重写：
    - ``forward``：执行实际 HTTP 请求，默认实现依赖 httpx。
    - ``forward_stream``：执行流式 HTTP 请求，默认实现依赖 httpx。
    """

    # -----------------------------------------------------------------
    # 必须实现
    # -----------------------------------------------------------------

    @abc.abstractmethod
    def build_request(
        self,
        std_req: "ChatCompletionRequest",
        upstream_model: str,
    ) -> dict:
        """将平台标准请求转换为上游协议请求体（字典，供 httpx 发送）。"""

    @abc.abstractmethod
    def parse_response(self, raw: dict) -> "ChatCompletionResponse":
        """将上游非流式 JSON 响应解析为平台标准响应。"""

    @abc.abstractmethod
    def parse_stream_chunk(self, data: str) -> "ChatCompletionStreamChunk | None":
        """将 SSE ``data:`` 行的内容解析为平台标准流式 chunk；[DONE] 时返回 None。"""

    @abc.abstractmethod
    def parse_usage(self, raw: dict) -> "Usage | None":
        """从完整响应体（或流式最终 chunk）中提取 Usage，不存在时返回 None。"""

    @abc.abstractmethod
    def normalize_error(self, status_code: int, raw: dict) -> Exception:
        """将上游错误响应转换为平台标准 AppError 子类并返回（不 raise）。"""

    # -----------------------------------------------------------------
    # 可选重写：协议差异（端点路径 / 鉴权头 / 流式选项）
    #
    # 默认实现对应 OpenAI Chat Completions 协议；私有协议适配器
    # （如 Anthropic Messages API）按需重写，网关不感知具体差异。
    # -----------------------------------------------------------------

    def build_url(self, base_url: str) -> str:
        """构造上游请求 URL。默认为 OpenAI 风格 ``{base_url}/chat/completions``。

        base_url 在渠道配置里通常已含版本路径（如 https://api.openai.com/v1）。
        """
        return f"{base_url.rstrip('/')}/chat/completions"

    def build_headers(self, upstream_key: str, request_id: str) -> dict[str, str]:
        """构造上游请求头。默认为 OpenAI 风格 Bearer 鉴权。"""
        return {
            "Authorization": f"Bearer {upstream_key}",
            "Content-Type": "application/json",
            "X-Request-ID": request_id,
        }

    def apply_stream_options(self, payload: dict) -> None:
        """流式请求时对请求体做协议特定调整（原地修改）。

        默认追加 OpenAI 的 ``stream_options.include_usage``，使最终 chunk
        携带 usage 供计费；不支持该字段的协议重写为 no-op 即可。
        注意：网关已在调用本方法前设置 ``payload["stream"] = True``。
        """
        payload["stream_options"] = {"include_usage": True}

    # -----------------------------------------------------------------
    # Embeddings（可选能力）
    #
    # 默认不支持；OpenAI-compatible 等协议适配器按需实现。
    # -----------------------------------------------------------------

    def supports_embeddings(self) -> bool:
        """该适配器是否支持 embeddings 上游调用。"""
        return False

    def build_embeddings_url(self, base_url: str) -> str:
        """构造 embeddings 上游 URL。默认抛出，子类按协议实现。"""
        raise NotImplementedError(
            f"{type(self).__name__} does not support embeddings"
        )

    def build_embeddings_request(
        self,
        std_req: "EmbeddingRequest",
        upstream_model: str,
    ) -> dict:
        """将平台标准 embeddings 请求转换为上游请求体。"""
        raise NotImplementedError(
            f"{type(self).__name__} does not support embeddings"
        )

    def parse_embeddings_response(self, raw: dict) -> "EmbeddingResponse":
        """将上游 embeddings JSON 响应解析为平台标准响应。"""
        raise NotImplementedError(
            f"{type(self).__name__} does not support embeddings"
        )
