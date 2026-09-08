"""chat sessions and messages

Revision ID: 002_chat_sessions
Revises: 001_initial
Create Date: 2026-07-21

"""

from collections.abc import Sequence

import app.db.models  # noqa: F401
from alembic import op
from app.db.models import Base

revision: str = "002_chat_sessions"
down_revision: str | Sequence[str] | None = "001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)


def downgrade() -> None:
    op.drop_table("chat_session_messages")
    op.drop_table("chat_sessions")
