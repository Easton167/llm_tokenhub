"""内部调用方：request_logs.api_key_id 可空

Revision ID: 010_reqlog_key_nullable
Revises: 009_kb_tables_pgvector
Create Date: 2026-07-26

知识库向量化 / 检索 query 走内部 embedding 链路，不占用用户 API Key，
故 request_logs.api_key_id 改为可空；存量行不受影响。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "010_reqlog_key_nullable"
down_revision: str | Sequence[str] | None = "009_kb_tables_pgvector"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "request_logs",
        "api_key_id",
        existing_type=sa.UUID(),
        nullable=True,
    )


def downgrade() -> None:
    # 若存在 api_key_id IS NULL 的内部调用日志，downgrade 会失败；需先清理。
    op.alter_column(
        "request_logs",
        "api_key_id",
        existing_type=sa.UUID(),
        nullable=False,
    )
