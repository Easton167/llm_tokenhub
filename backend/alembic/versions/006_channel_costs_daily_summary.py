"""phase2 channel_costs and usage_daily_summary

Revision ID: 006_channel_costs_daily_summary
Revises: 005_payment_orders
Create Date: 2026-07-23

说明：
- 新增 channel_costs：渠道 × 上游模型成本单价（版本化生效）
- 新增 usage_daily_summary：日粒度预聚合（天×模型×用户×组织×渠道）
- 空库上 create_all 已建表时本迁移幂等跳过
"""

from collections.abc import Sequence

from alembic import op

import app.db.models  # noqa: F401
from app.db.models import Base

revision: str = "006_channel_costs_daily_summary"
down_revision: str | Sequence[str] | None = "005_payment_orders"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(
        bind=bind,
        tables=[
            Base.metadata.tables["channel_costs"],
            Base.metadata.tables["usage_daily_summary"],
        ],
    )


def downgrade() -> None:
    op.drop_table("usage_daily_summary")
    op.drop_table("channel_costs")
