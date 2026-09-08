"""知识库全局配置单例表

Revision ID: 012_kb_global_settings
Revises: 011_chat_rag_fields
Create Date: 2026-07-27

管理端可热改的默认 embedding、单文件上限、批量上传上限、向量化批大小/并发。
字段为 NULL 时回退到环境变量（settings）。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "012_kb_global_settings"
down_revision: str | Sequence[str] | None = "011_chat_rag_fields"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "kb_global_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("default_embedding_model", sa.String(length=128), nullable=True),
        sa.Column("max_file_size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("upload_batch_limit", sa.Integer(), nullable=True),
        sa.Column("embedding_batch_size", sa.Integer(), nullable=True),
        sa.Column("embedding_max_concurrent", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("id = 1", name="ck_kb_global_settings_singleton"),
        sa.CheckConstraint(
            "max_file_size_bytes IS NULL OR max_file_size_bytes > 0",
            name="ck_kb_gs_max_file_size",
        ),
        sa.CheckConstraint(
            "upload_batch_limit IS NULL OR upload_batch_limit > 0",
            name="ck_kb_gs_upload_batch",
        ),
        sa.CheckConstraint(
            "embedding_batch_size IS NULL OR embedding_batch_size > 0",
            name="ck_kb_gs_embed_batch",
        ),
        sa.CheckConstraint(
            "embedding_max_concurrent IS NULL OR embedding_max_concurrent > 0",
            name="ck_kb_gs_embed_concurrent",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute(
        sa.text(
            "INSERT INTO kb_global_settings (id) VALUES (1) "
            "ON CONFLICT DO NOTHING"
        )
    )


def downgrade() -> None:
    op.drop_table("kb_global_settings")
