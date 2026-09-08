"""api_keys: allowed_models / rpm_limit / tpm_limit / expires_at

Revision ID: 008_api_key_scope_rate_limits
Revises: 007_provider_adapter_type
Create Date: 2026-07-23

说明：
- allowed_models：JSON 数组，空/NULL = 继承角色授权；非空 = 与角色授权取交集
- rpm_limit / tpm_limit：Key 级每分钟请求/token 上限，NULL = 不限
- expires_at：可选过期时间，过期后鉴权返回 401
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "008_api_key_scope_rate_limits"
down_revision: str | Sequence[str] | None = "007_provider_adapter_type"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "api_keys",
        sa.Column(
            "allowed_models",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "api_keys",
        sa.Column("rpm_limit", sa.Integer(), nullable=True),
    )
    op.add_column(
        "api_keys",
        sa.Column("tpm_limit", sa.Integer(), nullable=True),
    )
    op.add_column(
        "api_keys",
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("api_keys", "expires_at")
    op.drop_column("api_keys", "tpm_limit")
    op.drop_column("api_keys", "rpm_limit")
    op.drop_column("api_keys", "allowed_models")
