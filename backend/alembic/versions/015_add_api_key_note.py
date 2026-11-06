"""add api_key note

Revision ID: 504dfeacb1a1
Revises: 014_admin_rbac
Create Date: 2026-09-21 10:29:57.300778

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '504dfeacb1a1'
down_revision: str | Sequence[str] | None = '014_admin_rbac'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('api_keys', sa.Column('note', sa.String(length=255), nullable=True, comment='备注'))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('api_keys', 'note')
