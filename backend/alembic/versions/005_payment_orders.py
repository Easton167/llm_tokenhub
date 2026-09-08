"""phase2 payment_orders table

Revision ID: 005_payment_orders
Revises: 004_api_key_billing_subject
Create Date: 2026-07-23

说明：
- 新增 payment_orders：支付单、状态机、渠道外部单号与回调原文
- 入账幂等依赖 wallet_transactions 既有部分唯一索引
  (wallet_id, ref_request_id, type) WHERE ref_request_id IS NOT NULL
- 空库上 create_all 已建表时本迁移幂等跳过
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.db.models  # noqa: F401
from app.db.models import Base

revision: str = "005_payment_orders"
down_revision: str | Sequence[str] | None = "004_api_key_billing_subject"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(
        bind=bind,
        tables=[Base.metadata.tables["payment_orders"]],
    )


def downgrade() -> None:
    op.drop_table("payment_orders")
