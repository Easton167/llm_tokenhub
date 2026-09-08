"""知识库用户端 API schema。"""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.core.datetime_utils import UtcDateTime


class KnowledgeBaseCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=4000)
    # 省略则使用系统默认 embedding 模型
    embedding_model: str | None = Field(default=None, max_length=128)
    chunk_size: int | None = Field(default=None, ge=100, le=2000)
    chunk_overlap: int | None = Field(default=None, ge=0, le=200)
    separators: list[str] | None = None
    top_k: int | None = Field(default=None, ge=1, le=50)
    score_threshold: float | None = Field(default=None, ge=0.0, le=1.0)


class KnowledgeBaseUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=4000)
    chunk_size: int | None = Field(default=None, ge=100, le=2000)
    chunk_overlap: int | None = Field(default=None, ge=0, le=200)
    separators: list[str] | None = None
    top_k: int | None = Field(default=None, ge=1, le=50)
    score_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    # embedding_model 创建后不可更换，故意不开放


class KnowledgeBaseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    description: str | None
    embedding_model_id: UUID
    embedding_model: str | None = None  # alias，列表/详情填充
    embedding_dim: int
    chunk_size: int
    chunk_overlap: int
    separators: list
    top_k: int
    score_threshold: float
    doc_count: int
    chunk_count: int
    storage_bytes: int
    status: str
    created_at: UtcDateTime
    updated_at: UtcDateTime


class KbDocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    kb_id: UUID
    file_name: str
    file_type: str
    file_size_bytes: int
    status: str
    error_message: str | None
    total_tokens: int
    progress: int = 0  # 最新 ingest job 进度百分比
    chunk_size: int
    chunk_overlap: int
    separators: list
    created_at: UtcDateTime
    updated_at: UtcDateTime


class KbDocumentListOut(BaseModel):
    items: list[KbDocumentOut]
    total: int
    page: int
    page_size: int


class HitTestRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=4000)
    top_k: int | None = Field(default=None, ge=1, le=50)
    score_threshold: float | None = Field(default=None, ge=0.0, le=1.0)


class HitTestHitOut(BaseModel):
    chunk_id: UUID
    document_id: UUID
    document_name: str
    content: str
    score: float
    seq: int
    source_location: dict | None = None
    token_count: int


class HitTestOut(BaseModel):
    query: str
    top_k: int
    score_threshold: float
    hits: list[HitTestHitOut]
    # 本次 query 向量化费用（最小货币单位）；页面明示
    estimated_charge_amount: int
    request_id: str | None = None
    input_tokens: int = 0


class KbChunkOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    document_id: UUID
    kb_id: UUID
    seq: int
    content: str
    token_count: int
    source_location: dict | None = None
    is_enabled: bool
    is_manually_edited: bool
    embedding_status: str
    created_at: UtcDateTime
    updated_at: UtcDateTime


class KbChunkListOut(BaseModel):
    items: list[KbChunkOut]
    total: int
    page: int
    page_size: int


class KbChunkUpdate(BaseModel):
    content: str | None = Field(default=None, min_length=1, max_length=100_000)
    is_enabled: bool | None = None


class KbChunkCreate(BaseModel):
    content: str = Field(..., min_length=1, max_length=100_000)
    # 插入序号；省略则追加到末尾。该位置及之后的现有分块序号顺延。
    seq: int | None = Field(default=None, ge=0)


class KbChunkBatchRequest(BaseModel):
    chunk_ids: list[UUID] = Field(..., min_length=1, max_length=200)
    action: str = Field(..., pattern="^(enable|disable|delete)$")


class KbChunkBatchOut(BaseModel):
    action: str
    affected: int


class RechunkRequest(BaseModel):
    # 必须为 true：明确确认将删除现有分块（含人工编辑）并重新计费向量化
    confirm: bool = False
    chunk_size: int | None = Field(default=None, ge=100, le=2000)
    chunk_overlap: int | None = Field(default=None, ge=0, le=200)
    separators: list[str] | None = None
