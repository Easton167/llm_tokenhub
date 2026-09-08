"""providers/channels 协议类型字段兜底迁移

Revision ID: 007_provider_adapter_type
Revises: 006_channel_costs_daily_summary
Create Date: 2026-07-23

说明：
- providers / channels 的 adapter_type（协议类型）字段在 001 初始迁移
  （metadata.create_all）中已随模型创建，本迁移用于两类兜底，保证幂等：
  1. 若历史库缺少该列，则补建，存量数据默认 'openai_compatible'；
  2. 将 NULL / 空串回填为 'openai_compatible'，保证网关按协议类型
     选择适配器时不会命中无效值。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "007_provider_adapter_type"
down_revision: str | Sequence[str] | None = "006_channel_costs_daily_summary"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DEFAULT_ADAPTER_TYPE = "openai_compatible"


def _ensure_adapter_type_column(table: str) -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {col["name"] for col in inspector.get_columns(table)}

    if "adapter_type" not in columns:
        op.add_column(
            table,
            sa.Column(
                "adapter_type",
                sa.String(64),
                nullable=False,
                server_default=_DEFAULT_ADAPTER_TYPE,
            ),
        )
        # 与 ORM 模型保持一致：默认值由应用层负责，去掉列级 server_default
        op.alter_column(table, "adapter_type", server_default=None)
        return

    # 列已存在：回填无效值
    op.execute(
        sa.text(
            f"UPDATE {table} SET adapter_type = :val "
            f"WHERE adapter_type IS NULL OR adapter_type = ''"
        ).bindparams(val=_DEFAULT_ADAPTER_TYPE)
    )


def upgrade() -> None:
    _ensure_adapter_type_column("providers")
    _ensure_adapter_type_column("channels")


def downgrade() -> None:
    # adapter_type 属于初始 schema 的一部分，回滚不删除该列
    pass
