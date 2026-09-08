"""api_keys.billing_subject nullable for account-default fallback

Revision ID: 004_api_key_billing_subject
Revises: 003_org_domain
Create Date: 2026-07-23

说明：
- api_keys.billing_subject 改为可空：NULL 表示 Key 未绑定主体，
  决策时回退到 users.default_billing_subject（再默认 personal）
- 已有 'personal' 等非空值保持不变（显式绑定）
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "004_api_key_billing_subject"
down_revision: str | Sequence[str] | None = "003_org_domain"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "api_keys",
        "billing_subject",
        existing_type=sa.String(length=64),
        nullable=True,
        server_default=None,
    )


def downgrade() -> None:
    op.execute(
        "UPDATE api_keys SET billing_subject = 'personal' WHERE billing_subject IS NULL"
    )
    op.alter_column(
        "api_keys",
        "billing_subject",
        existing_type=sa.String(length=64),
        nullable=False,
        server_default="personal",
    )
