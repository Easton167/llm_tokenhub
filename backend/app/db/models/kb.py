"""知识库域模型：知识库、文档、分块（pgvector）与处理任务。

设计口径见 docs/DATABASE_DESIGN.md 第 7 节；本模块仅定义数据模型，
业务 API / 解析 / 向量化逻辑由知识库专项后续任务实现。
"""

from typing import TYPE_CHECKING
from uuid import UUID

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.config import settings
from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.db.models.llm_model import LLMModel
    from app.db.models.user import User


class KnowledgeBase(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """知识库：严格按 owner_user_id 隔离；embedding 模型创建后不可更换。"""

    __tablename__ = "knowledge_bases"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'disabled')", name="ck_kb_status"),
        CheckConstraint(
            "chunk_size >= 100 AND chunk_size <= 2000", name="ck_kb_chunk_size_range"
        ),
        CheckConstraint(
            "chunk_overlap >= 0 AND chunk_overlap <= 200 AND chunk_overlap < chunk_size",
            name="ck_kb_chunk_overlap_range",
        ),
        Index("ix_knowledge_bases_owner_status", "owner_user_id", "status"),
    )

    owner_user_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    embedding_model_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("models.id"), nullable=False
    )
    # 记录各库向量维度以便后续多维度/重建索引迁移；首期全平台统一为 settings.kb_embedding_dim
    embedding_dim: Mapped[int] = mapped_column(Integer, nullable=False)
    # 默认分块参数：新上传文档以此为快照，文档级可覆盖
    chunk_size: Mapped[int] = mapped_column(Integer, nullable=False, default=500)
    chunk_overlap: Mapped[int] = mapped_column(Integer, nullable=False, default=50)
    separators: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)  # 空数组=引擎内置默认
    # 默认检索参数
    top_k: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    score_threshold: Mapped[float] = mapped_column(Float, nullable=False, default=0.35)  # 非金额，允许浮点
    # 统计冗余，由业务层在文档/分块变更时维护
    doc_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    storage_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")

    owner: Mapped["User"] = relationship()
    embedding_model: Mapped["LLMModel"] = relationship()
    documents: Mapped[list["KbDocument"]] = relationship(
        back_populates="knowledge_base", cascade="all, delete-orphan", passive_deletes=True
    )


class KbDocument(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """知识库文档：文件元数据 + 分块参数快照 + 处理状态机。"""

    __tablename__ = "kb_documents"
    __table_args__ = (
        CheckConstraint(
            "status IN ('uploaded', 'parsing', 'chunking', 'pending_embedding', "
            "'insufficient_balance', 'embedding', 'ready', 'partial', 'failed')",
            name="ck_kb_documents_status",
        ),
        Index("ix_kb_documents_kb_status", "kb_id", "status"),
        Index("ix_kb_documents_kb_created", "kb_id", "created_at"),
    )

    kb_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"),
        nullable=False,
    )
    file_name: Mapped[str] = mapped_column(String(512), nullable=False)  # 仅展示，存储不用原名
    file_type: Mapped[str] = mapped_column(String(16), nullable=False)  # txt/md/pdf/docx/html/csv/xlsx
    file_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    storage_path: Mapped[str] = mapped_column(String(512), nullable=False)  # 随机不可预测标识
    # 分块参数快照（上传时取库默认）
    chunk_size: Mapped[int] = mapped_column(Integer, nullable=False, default=500)
    chunk_overlap: Mapped[int] = mapped_column(Integer, nullable=False, default=50)
    separators: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="uploaded")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # 预扣估算依据

    knowledge_base: Mapped["KnowledgeBase"] = relationship(back_populates="documents")
    chunks: Mapped[list["KbChunk"]] = relationship(
        back_populates="document", cascade="all, delete-orphan", passive_deletes=True
    )
    ingest_jobs: Mapped[list["KbIngestJob"]] = relationship(
        back_populates="document", cascade="all, delete-orphan", passive_deletes=True
    )


class KbChunk(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """文档分块；embedding 为 pgvector 向量列，按 cosine 建 HNSW 索引。

    HNSW 索引要求固定维度，列维度取 settings.kb_embedding_dim（默认 1536），
    首期全平台 embedding 模型维度统一，见 docs/DATABASE_DESIGN.md 7.3。
    """

    __tablename__ = "kb_chunks"
    __table_args__ = (
        CheckConstraint(
            "embedding_status IN ('pending', 'success', 'failed')",
            name="ck_kb_chunks_embedding_status",
        ),
        Index("ix_kb_chunks_document_seq", "document_id", "seq"),
        Index("ix_kb_chunks_kb_enabled", "kb_id", "is_enabled"),
        Index(
            "ix_kb_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    document_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("kb_documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    kb_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"),
        nullable=False,
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)  # 文档内序号
    content: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source_location: Mapped[dict | None] = mapped_column(JSONB, nullable=True)  # 页码/标题路径
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_manually_edited: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(settings.kb_embedding_dim), nullable=True
    )
    embedding_status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")

    document: Mapped["KbDocument"] = relationship(back_populates="chunks")


class KbIngestJob(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """知识库处理任务：解析/分块/向量化/重新分块；幂等键防止重复执行产生副作用。"""

    __tablename__ = "kb_ingest_jobs"
    __table_args__ = (
        CheckConstraint(
            "job_type IN ('parse', 'chunk', 'embed', 'rechunk')",
            name="ck_kb_ingest_jobs_type",
        ),
        CheckConstraint(
            "status IN ('pending', 'running', 'success', 'failed', 'cancelled')",
            name="ck_kb_ingest_jobs_status",
        ),
        CheckConstraint("progress >= 0 AND progress <= 100", name="ck_kb_ingest_jobs_progress"),
        Index("ix_kb_ingest_jobs_status_created", "status", "created_at"),
        Index("ix_kb_ingest_jobs_document_created", "document_id", "created_at"),
    )

    job_type: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    progress: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)  # 百分比
    document_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("kb_documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    kb_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"),
        nullable=False,
    )
    # 向量化预扣流水的 ref_request_id（wallet_transactions 关联键）
    hold_ref_request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    document: Mapped["KbDocument"] = relationship(back_populates="ingest_jobs")
