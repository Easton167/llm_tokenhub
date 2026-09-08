"""用户端 Chat 会话 API schema。"""

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.constants import CHAT_MISS_STRATEGIES, CHAT_MISS_STRATEGY_FALLBACK
from app.core.datetime_utils import UtcDateTime


class CitationOut(BaseModel):
    """消息上持久化的引用来源快照。"""

    chunk_id: UUID
    kb_id: UUID | None = None
    document_id: UUID | None = None
    document_name: str
    content_summary: str = ""
    score: float = 0.0
    index: int | None = None


class ChatMessageIn(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str = Field(default="", max_length=200_000)
    citations: list[CitationOut] | None = None


class ChatMessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    role: str
    content: str
    seq: int
    citations: list[CitationOut] | None = None
    created_at: UtcDateTime


class ChatSessionCreate(BaseModel):
    title: str = Field(default="新对话", min_length=1, max_length=255)
    model: str | None = Field(default=None, max_length=128)
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, ge=1, le=128_000)
    kb_ids: list[UUID] = Field(default_factory=list, max_length=50)
    miss_strategy: Literal["strict", "fallback"] = CHAT_MISS_STRATEGY_FALLBACK
    retrieval_top_k: int | None = Field(default=None, ge=1, le=50)
    retrieval_score_threshold: float | None = Field(default=None, ge=0.0, le=1.0)


class ChatSessionUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    model: str | None = Field(default=None, max_length=128)
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, ge=1, le=128_000)
    kb_ids: list[UUID] | None = Field(default=None, max_length=50)
    miss_strategy: Literal["strict", "fallback"] | None = None
    retrieval_top_k: int | None = Field(default=None, ge=1, le=50)
    retrieval_score_threshold: float | None = Field(default=None, ge=0.0, le=1.0)


class ChatSessionSummaryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    model: str | None
    temperature: float | None
    max_tokens: int | None
    kb_ids: list[UUID] = Field(default_factory=list)
    miss_strategy: str = CHAT_MISS_STRATEGY_FALLBACK
    retrieval_top_k: int | None = None
    retrieval_score_threshold: float | None = None
    message_count: int = 0
    created_at: UtcDateTime
    updated_at: UtcDateTime


class ChatSessionDetailOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    model: str | None
    temperature: float | None
    max_tokens: int | None
    kb_ids: list[UUID] = Field(default_factory=list)
    miss_strategy: str = CHAT_MISS_STRATEGY_FALLBACK
    retrieval_top_k: int | None = None
    retrieval_score_threshold: float | None = None
    messages: list[ChatMessageOut]
    created_at: UtcDateTime
    updated_at: UtcDateTime


class ChatMessagesReplace(BaseModel):
    """整表替换会话消息（多轮结束后由前端同步）。"""

    messages: list[ChatMessageIn] = Field(default_factory=list, max_length=500)
    title: str | None = Field(default=None, min_length=1, max_length=255)
    model: str | None = Field(default=None, max_length=128)
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, ge=1, le=128_000)


class KbCompletionsRequest(BaseModel):
    """前台 BFF：带知识库的对话编排（JWT + 平台 API Key）。"""

    model: str = Field(..., min_length=1, max_length=128)
    messages: list[ChatMessageIn] = Field(..., min_length=1, max_length=500)
    session_id: UUID | None = None
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, ge=1, le=128_000)
    # 以下可覆盖会话挂载设置；均空则用 session / 默认
    kb_ids: list[UUID] | None = Field(default=None, max_length=50)
    miss_strategy: Literal["strict", "fallback"] | None = None
    top_k: int | None = Field(default=None, ge=1, le=50)
    score_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    stream: bool = True

    @field_validator("miss_strategy")
    @classmethod
    def _check_strategy(cls, v: str | None) -> str | None:
        if v is not None and v not in CHAT_MISS_STRATEGIES:
            raise ValueError("miss_strategy must be 'strict' or 'fallback'")
        return v


def parse_kb_ids(raw: Any) -> list[UUID]:
    """将库内 JSONB（UUID 字符串列表）转为 UUID 列表。"""
    if not raw:
        return []
    out: list[UUID] = []
    for item in raw:
        if isinstance(item, UUID):
            out.append(item)
        else:
            out.append(UUID(str(item)))
    return out


def kb_ids_to_storage(ids: list[UUID]) -> list[str]:
    return [str(i) for i in ids]
