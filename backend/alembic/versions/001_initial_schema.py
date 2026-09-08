"""initial schema

Revision ID: 001_initial
Revises:
Create Date: 2026-06-25

"""

from collections.abc import Sequence

import app.db.models  # noqa: F401
from alembic import op
from app.db.models import Base

revision: str = "001_initial"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)


def downgrade() -> None:
    bind = op.get_bind()
    Base.metadata.drop_all(bind=bind)
