"""知识库域：4 张新表 + pgvector 扩展 + 既有表 source / 角色配额扩展

Revision ID: 009_kb_tables_pgvector
Revises: 008_api_key_scope_rate_limits
Create Date: 2026-07-26

说明：
- 启用 pgvector 扩展；kb_chunks.embedding 为 vector(KB_EMBEDDING_DIM) 列并建 HNSW（cosine）索引。
  HNSW 要求固定维度，首期全平台 embedding 维度统一（默认 1536，配置 KB_EMBEDDING_DIM），
  knowledge_bases.embedding_dim 记录各库维度供后续迁移。
- usage_logs / request_logs 增加 source（chat/api/kb_ingest/kb_query）：
  nullable + server default 'api'，存量行不受影响。
- roles 增加知识库配额字段组，全部 nullable（NULL = 系统默认值）。
- downgrade 删除本迁移创建的表与字段；不删除 vector 扩展（可能被其他对象使用，保留无副作用）。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

from app.core.config import settings

revision: str = "009_kb_tables_pgvector"
down_revision: str | Sequence[str] | None = "008_api_key_scope_rate_limits"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "knowledge_bases",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "owner_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "embedding_model_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("models.id"),
            nullable=False,
        ),
        sa.Column("embedding_dim", sa.Integer(), nullable=False),
        sa.Column("chunk_size", sa.Integer(), nullable=False, server_default="500"),
        sa.Column("chunk_overlap", sa.Integer(), nullable=False, server_default="50"),
        sa.Column(
            "separators",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("top_k", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("score_threshold", sa.Float(), nullable=False, server_default="0.35"),
        sa.Column("doc_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("chunk_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("storage_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("status IN ('active', 'disabled')", name="ck_kb_status"),
        sa.CheckConstraint(
            "chunk_size >= 100 AND chunk_size <= 2000", name="ck_kb_chunk_size_range"
        ),
        sa.CheckConstraint(
            "chunk_overlap >= 0 AND chunk_overlap <= 200 AND chunk_overlap < chunk_size",
            name="ck_kb_chunk_overlap_range",
        ),
    )
    op.create_index(
        "ix_knowledge_bases_owner_status", "knowledge_bases", ["owner_user_id", "status"]
    )

    op.create_table(
        "kb_documents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "kb_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("knowledge_bases.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("file_name", sa.String(512), nullable=False),
        sa.Column("file_type", sa.String(16), nullable=False),
        sa.Column("file_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("storage_path", sa.String(512), nullable=False),
        sa.Column("chunk_size", sa.Integer(), nullable=False, server_default="500"),
        sa.Column("chunk_overlap", sa.Integer(), nullable=False, server_default="50"),
        sa.Column(
            "separators",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("status", sa.String(32), nullable=False, server_default="uploaded"),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('uploaded', 'parsing', 'chunking', 'pending_embedding', "
            "'insufficient_balance', 'embedding', 'ready', 'partial', 'failed')",
            name="ck_kb_documents_status",
        ),
    )
    op.create_index("ix_kb_documents_kb_status", "kb_documents", ["kb_id", "status"])
    op.create_index("ix_kb_documents_kb_created", "kb_documents", ["kb_id", "created_at"])

    op.create_table(
        "kb_chunks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("kb_documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "kb_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("knowledge_bases.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("source_location", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "is_manually_edited", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("embedding", Vector(settings.kb_embedding_dim), nullable=True),
        sa.Column("embedding_status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "embedding_status IN ('pending', 'success', 'failed')",
            name="ck_kb_chunks_embedding_status",
        ),
    )
    op.create_index("ix_kb_chunks_document_seq", "kb_chunks", ["document_id", "seq"])
    op.create_index("ix_kb_chunks_kb_enabled", "kb_chunks", ["kb_id", "is_enabled"])
    op.create_index(
        "ix_kb_chunks_embedding_hnsw",
        "kb_chunks",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )

    op.create_table(
        "kb_ingest_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("job_type", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("progress", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("kb_documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "kb_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("knowledge_bases.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("hold_ref_request_id", sa.String(128), nullable=True),
        sa.Column("idempotency_key", sa.String(255), nullable=False, unique=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "job_type IN ('parse', 'chunk', 'embed', 'rechunk')", name="ck_kb_ingest_jobs_type"
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'success', 'failed', 'cancelled')",
            name="ck_kb_ingest_jobs_status",
        ),
        sa.CheckConstraint("progress >= 0 AND progress <= 100", name="ck_kb_ingest_jobs_progress"),
    )
    op.create_index("ix_kb_ingest_jobs_status_created", "kb_ingest_jobs", ["status", "created_at"])
    op.create_index(
        "ix_kb_ingest_jobs_document_created", "kb_ingest_jobs", ["document_id", "created_at"]
    )

    # 既有表扩展：source 字段（存量行由 server default 补为 'api'）
    op.add_column(
        "request_logs",
        sa.Column("source", sa.String(16), nullable=True, server_default="api"),
    )
    op.add_column(
        "usage_logs",
        sa.Column("source", sa.String(16), nullable=True, server_default="api"),
    )
    op.create_index("ix_usage_logs_source_created", "usage_logs", ["source", "created_at"])

    # 角色配额字段组（NULL = 系统默认值）
    op.add_column("roles", sa.Column("kb_max_kb_count", sa.Integer(), nullable=True))
    op.add_column("roles", sa.Column("kb_max_docs_per_kb", sa.Integer(), nullable=True))
    op.add_column("roles", sa.Column("kb_max_storage_bytes", sa.BigInteger(), nullable=True))
    op.add_column("roles", sa.Column("kb_max_file_size_bytes", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column("roles", "kb_max_file_size_bytes")
    op.drop_column("roles", "kb_max_storage_bytes")
    op.drop_column("roles", "kb_max_docs_per_kb")
    op.drop_column("roles", "kb_max_kb_count")

    op.drop_index("ix_usage_logs_source_created", table_name="usage_logs")
    op.drop_column("usage_logs", "source")
    op.drop_column("request_logs", "source")

    op.drop_table("kb_ingest_jobs")
    op.drop_table("kb_chunks")
    op.drop_table("kb_documents")
    op.drop_table("knowledge_bases")
    # 不删除 vector 扩展：CREATE EXTENSION IF NOT EXISTS 幂等，保留扩展无副作用
