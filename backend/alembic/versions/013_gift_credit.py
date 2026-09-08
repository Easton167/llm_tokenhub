"""赠送余额：双资金池字段 + gift_grants 批次账本

Revision ID: 013_gift_credit
Revises: 012_kb_global_settings
Create Date: 2026-08-16

说明：
- wallets 增加 gift_balance / gift_held（现金池 balance/held 语义收窄）
- 新建 gift_grants 赠送批次账本
- wallet_transactions 增加赠送池拆分快照与 gift_grant_id
- usage_logs / usage_daily_summary 增加资金来源拆分字段
- 存量一次性 UPDATE 回填；downgrade 删列删表
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "013_gift_credit"
down_revision: str | Sequence[str] | None = "012_kb_global_settings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "wallets",
        sa.Column("gift_balance", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.add_column(
        "wallets",
        sa.Column("gift_held", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.create_check_constraint(
        "ck_wallets_gift_balance_non_negative",
        "wallets",
        "gift_balance >= 0",
    )
    op.create_check_constraint(
        "ck_wallets_gift_held_non_negative",
        "wallets",
        "gift_held >= 0",
    )

    op.create_table(
        "gift_grants",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "wallet_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("wallets.id"),
            nullable=False,
        ),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("remaining", sa.BigInteger(), nullable=False),
        sa.Column("reason", sa.String(32), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column(
            "granted_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column(
            "revoked_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("remark", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "remaining >= 0 AND remaining <= amount",
            name="ck_gift_grants_remaining_range",
        ),
        sa.CheckConstraint(
            "reason IN ('promotion', 'compensation', 'internal', 'partnership', 'other')",
            name="ck_gift_grants_reason",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'used_up', 'expired', 'revoked')",
            name="ck_gift_grants_status",
        ),
        sa.UniqueConstraint(
            "wallet_id",
            "idempotency_key",
            name="uq_gift_grants_wallet_idempotency",
        ),
    )
    op.create_index(
        "ix_gift_grants_wallet_status_expires",
        "gift_grants",
        ["wallet_id", "status", "expires_at"],
    )

    op.add_column(
        "wallet_transactions",
        sa.Column("gift_amount", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.add_column(
        "wallet_transactions",
        sa.Column("gift_balance_after", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.add_column(
        "wallet_transactions",
        sa.Column("gift_held_after", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.add_column(
        "wallet_transactions",
        sa.Column("gift_grant_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_wallet_transactions_gift_grant_id_gift_grants",
        "wallet_transactions",
        "gift_grants",
        ["gift_grant_id"],
        ["id"],
    )

    op.add_column(
        "usage_logs",
        sa.Column("gift_charge_amount", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.add_column(
        "usage_logs",
        sa.Column("cash_charge_amount", sa.BigInteger(), nullable=False, server_default="0"),
    )

    op.add_column(
        "usage_daily_summary",
        sa.Column("gift_revenue_amount", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.add_column(
        "usage_daily_summary",
        sa.Column("cash_revenue_amount", sa.BigInteger(), nullable=False, server_default="0"),
    )

    # 存量一次性回填（不要逐行 Python 循环）
    op.execute(
        sa.text(
            "UPDATE usage_logs "
            "SET gift_charge_amount = 0, cash_charge_amount = charge_amount"
        )
    )
    op.execute(
        sa.text(
            "UPDATE usage_daily_summary "
            "SET gift_revenue_amount = 0, cash_revenue_amount = revenue_amount"
        )
    )
    op.execute(
        sa.text(
            "UPDATE wallet_transactions "
            "SET gift_amount = 0, gift_balance_after = 0, gift_held_after = 0"
        )
    )


def downgrade() -> None:
    op.drop_column("usage_daily_summary", "cash_revenue_amount")
    op.drop_column("usage_daily_summary", "gift_revenue_amount")

    op.drop_column("usage_logs", "cash_charge_amount")
    op.drop_column("usage_logs", "gift_charge_amount")

    op.drop_constraint(
        "fk_wallet_transactions_gift_grant_id_gift_grants",
        "wallet_transactions",
        type_="foreignkey",
    )
    op.drop_column("wallet_transactions", "gift_grant_id")
    op.drop_column("wallet_transactions", "gift_held_after")
    op.drop_column("wallet_transactions", "gift_balance_after")
    op.drop_column("wallet_transactions", "gift_amount")

    op.drop_index("ix_gift_grants_wallet_status_expires", table_name="gift_grants")
    op.drop_table("gift_grants")

    op.drop_constraint("ck_wallets_gift_held_non_negative", "wallets", type_="check")
    op.drop_constraint("ck_wallets_gift_balance_non_negative", "wallets", type_="check")
    op.drop_column("wallets", "gift_held")
    op.drop_column("wallets", "gift_balance")
