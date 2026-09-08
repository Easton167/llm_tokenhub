"""后台管理 RBAC：独立管理员账号 + 角色 + 权限码授予

Revision ID: 014_admin_rbac
Revises: 013_gift_credit
Create Date: 2026-08-27

说明：
- admin_users 独立于前台 users，避免前台注册成为提权入口
- admin_roles.data_scope 为预留字段，当前不参与任何查询过滤
- 权限码目录由代码维护（app.core.admin_permissions），此处只存授予关系
- 不预置任何账号：无 admin_users 记录时由环境变量 ADMIN_USERNAME/ADMIN_PASSWORD 兜底登录
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "014_admin_rbac"
down_revision: str | Sequence[str] | None = "013_gift_credit"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamp_columns() -> list[sa.Column]:
    return [
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
    ]


def upgrade() -> None:
    op.create_table(
        "admin_users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("username", sa.String(64), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("display_name", sa.String(64), nullable=True),
        sa.Column("status", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column(
            "is_superadmin", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        *_timestamp_columns(),
    )

    op.create_table(
        "admin_roles",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("code", sa.String(64), nullable=False, unique=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("data_scope", sa.String(32), nullable=False, server_default="all"),
        *_timestamp_columns(),
        sa.CheckConstraint(
            "data_scope IN ('all', 'self')", name="ck_admin_roles_data_scope"
        ),
    )

    op.create_table(
        "admin_user_roles",
        sa.Column(
            "admin_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("admin_users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "role_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("admin_roles.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        *_timestamp_columns(),
        sa.UniqueConstraint("admin_user_id", "role_id", name="uq_admin_user_roles"),
    )

    op.create_table(
        "admin_role_permissions",
        sa.Column(
            "role_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("admin_roles.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("code", sa.String(64), primary_key=True),
        *_timestamp_columns(),
    )


def downgrade() -> None:
    op.drop_table("admin_role_permissions")
    op.drop_table("admin_user_roles")
    op.drop_table("admin_roles")
    op.drop_table("admin_users")
