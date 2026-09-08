"""Chat RAG：会话挂载知识库与消息引用快照

Revision ID: 011_chat_rag_fields
Revises: 010_reqlog_key_nullable
Create Date: 2026-07-27

- chat_sessions：kb_ids / miss_strategy / retrieval_top_k / retrieval_score_threshold
- chat_session_messages：citations（JSON 引用分块快照）
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "011_chat_rag_fields"
down_revision: str | Sequence[str] | None = "010_reqlog_key_nullable"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "chat_sessions",
        sa.Column(
            "kb_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "chat_sessions",
        sa.Column(
            "miss_strategy",
            sa.String(length=16),
            nullable=False,
            server_default="fallback",
        ),
    )
    op.add_column(
        "chat_sessions",
        sa.Column("retrieval_top_k", sa.Integer(), nullable=True),
    )
    op.add_column(
        "chat_sessions",
        sa.Column("retrieval_score_threshold", sa.Float(), nullable=True),
    )
    op.create_check_constraint(
        "ck_chat_sessions_miss_strategy",
        "chat_sessions",
        "miss_strategy IN ('strict', 'fallback')",
    )

    op.add_column(
        "chat_session_messages",
        sa.Column(
            "citations",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("chat_session_messages", "citations")
    op.drop_constraint("ck_chat_sessions_miss_strategy", "chat_sessions", type_="check")
    op.drop_column("chat_sessions", "retrieval_score_threshold")
    op.drop_column("chat_sessions", "retrieval_top_k")
    op.drop_column("chat_sessions", "miss_strategy")
    op.drop_column("chat_sessions", "kb_ids")
