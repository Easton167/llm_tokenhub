"""phase2 org domain tables and billing attribution columns

Revision ID: 003_org_domain
Revises: 002_chat_sessions
Create Date: 2026-07-23

说明：
- 新增 orgs / org_members / member_quotas
- wallets 去掉 owner_id→users FK，启用 owner_type='org'
- request_logs / usage_logs / wallet_transactions 增加可空 org_id 与发起成员字段
- 对已有一期库可执行；空库上 create_all 已建表时本迁移幂等跳过
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

import app.db.models  # noqa: F401
from app.db.models import Base

revision: str = "003_org_domain"
down_revision: str | Sequence[str] | None = "002_chat_sessions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_table(inspector: sa.Inspector, table: str) -> bool:
    return table in inspector.get_table_names()


def _has_column(inspector: sa.Inspector, table: str, column: str) -> bool:
    if not _has_table(inspector, table):
        return False
    return any(c["name"] == column for c in inspector.get_columns(table))


def _has_fk(inspector: sa.Inspector, table: str, constrained_columns: set[str]) -> str | None:
    for fk in inspector.get_foreign_keys(table):
        if set(fk.get("constrained_columns") or []) == constrained_columns:
            return fk.get("name")
    return None


def _has_check(inspector: sa.Inspector, table: str, name: str) -> bool:
    return any(c.get("name") == name for c in inspector.get_check_constraints(table))


def _has_index(inspector: sa.Inspector, table: str, name: str) -> bool:
    return any(i.get("name") == name for i in inspector.get_indexes(table))


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # 新表：与 001/002 的 create_all 策略兼容（已存在则跳过）
    Base.metadata.create_all(
        bind=bind,
        tables=[
            Base.metadata.tables["orgs"],
            Base.metadata.tables["org_members"],
            Base.metadata.tables["member_quotas"],
        ],
    )
    inspector = sa.inspect(bind)

    # wallets：去掉指向 users 的 FK，允许 owner_id 指向 orgs
    fk_name = _has_fk(inspector, "wallets", {"owner_id"})
    if fk_name:
        op.drop_constraint(fk_name, "wallets", type_="foreignkey")

    if not _has_check(inspector, "wallets", "ck_wallets_owner_type"):
        op.create_check_constraint(
            "ck_wallets_owner_type",
            "wallets",
            "owner_type IN ('personal', 'org')",
        )

    # request_logs
    if not _has_column(inspector, "request_logs", "org_id"):
        op.add_column(
            "request_logs",
            sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=True),
        )
        op.create_foreign_key(
            "fk_request_logs_org_id_orgs",
            "request_logs",
            "orgs",
            ["org_id"],
            ["id"],
        )
    if not _has_column(inspector, "request_logs", "actor_user_id"):
        op.add_column(
            "request_logs",
            sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        )
        op.create_foreign_key(
            "fk_request_logs_actor_user_id_users",
            "request_logs",
            "users",
            ["actor_user_id"],
            ["id"],
        )
    if not _has_index(inspector, "request_logs", "ix_request_logs_org_created"):
        op.create_index(
            "ix_request_logs_org_created",
            "request_logs",
            ["org_id", "created_at"],
        )

    # usage_logs
    if not _has_column(inspector, "usage_logs", "org_id"):
        op.add_column(
            "usage_logs",
            sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=True),
        )
        op.create_foreign_key(
            "fk_usage_logs_org_id_orgs",
            "usage_logs",
            "orgs",
            ["org_id"],
            ["id"],
        )
    if not _has_column(inspector, "usage_logs", "actor_user_id"):
        op.add_column(
            "usage_logs",
            sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        )
        op.create_foreign_key(
            "fk_usage_logs_actor_user_id_users",
            "usage_logs",
            "users",
            ["actor_user_id"],
            ["id"],
        )
    if not _has_index(inspector, "usage_logs", "ix_usage_logs_org_created"):
        op.create_index(
            "ix_usage_logs_org_created",
            "usage_logs",
            ["org_id", "created_at"],
        )

    # wallet_transactions（operator_user_id 一期已有）
    if not _has_column(inspector, "wallet_transactions", "org_id"):
        op.add_column(
            "wallet_transactions",
            sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=True),
        )
        op.create_foreign_key(
            "fk_wallet_transactions_org_id_orgs",
            "wallet_transactions",
            "orgs",
            ["org_id"],
            ["id"],
        )
    if not _has_index(inspector, "wallet_transactions", "ix_wallet_transactions_org_created"):
        op.create_index(
            "ix_wallet_transactions_org_created",
            "wallet_transactions",
            ["org_id", "created_at"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _has_index(inspector, "wallet_transactions", "ix_wallet_transactions_org_created"):
        op.drop_index("ix_wallet_transactions_org_created", table_name="wallet_transactions")
    fk = _has_fk(inspector, "wallet_transactions", {"org_id"})
    if fk:
        op.drop_constraint(fk, "wallet_transactions", type_="foreignkey")
    if _has_column(inspector, "wallet_transactions", "org_id"):
        op.drop_column("wallet_transactions", "org_id")

    if _has_index(inspector, "usage_logs", "ix_usage_logs_org_created"):
        op.drop_index("ix_usage_logs_org_created", table_name="usage_logs")
    fk = _has_fk(inspector, "usage_logs", {"actor_user_id"})
    if fk:
        op.drop_constraint(fk, "usage_logs", type_="foreignkey")
    if _has_column(inspector, "usage_logs", "actor_user_id"):
        op.drop_column("usage_logs", "actor_user_id")
    fk = _has_fk(inspector, "usage_logs", {"org_id"})
    if fk:
        op.drop_constraint(fk, "usage_logs", type_="foreignkey")
    if _has_column(inspector, "usage_logs", "org_id"):
        op.drop_column("usage_logs", "org_id")

    if _has_index(inspector, "request_logs", "ix_request_logs_org_created"):
        op.drop_index("ix_request_logs_org_created", table_name="request_logs")
    fk = _has_fk(inspector, "request_logs", {"actor_user_id"})
    if fk:
        op.drop_constraint(fk, "request_logs", type_="foreignkey")
    if _has_column(inspector, "request_logs", "actor_user_id"):
        op.drop_column("request_logs", "actor_user_id")
    fk = _has_fk(inspector, "request_logs", {"org_id"})
    if fk:
        op.drop_constraint(fk, "request_logs", type_="foreignkey")
    if _has_column(inspector, "request_logs", "org_id"):
        op.drop_column("request_logs", "org_id")

    if _has_check(inspector, "wallets", "ck_wallets_owner_type"):
        op.drop_constraint("ck_wallets_owner_type", "wallets", type_="check")

    # 回滚时恢复 personal 钱包的 users FK（仅当无 org 钱包时安全）
    if _has_table(inspector, "wallets") and not _has_fk(inspector, "wallets", {"owner_id"}):
        # 若存在 owner_type='org' 行，先拒绝回滚以免违反 FK
        org_wallet_count = bind.execute(
            sa.text("SELECT COUNT(*) FROM wallets WHERE owner_type = 'org'")
        ).scalar()
        if org_wallet_count and int(org_wallet_count) > 0:
            raise RuntimeError(
                "Cannot downgrade 003_org_domain while org wallets exist; "
                "delete org wallets first"
            )
        op.create_foreign_key(
            "wallets_owner_id_fkey",
            "wallets",
            "users",
            ["owner_id"],
            ["id"],
        )

    if _has_table(inspector, "member_quotas"):
        op.drop_table("member_quotas")
    if _has_table(inspector, "org_members"):
        op.drop_table("org_members")
    if _has_table(inspector, "orgs"):
        op.drop_table("orgs")
