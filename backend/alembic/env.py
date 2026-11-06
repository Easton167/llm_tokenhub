"""Alembic 运行时钩子：把迁移接到本项目的配置与 ORM metadata。

autogenerate 对比的两端是：
- 模型侧：``Base.metadata``（必须先 import 全部模型，表才会登记上去）
- 数据库侧：当前库的实际 schema

本文件由 ``alembic`` CLI 加载，不要被业务代码 import。
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.core.config import settings
# Base 提供 metadata；import models 包是为了把各表登记进 metadata。
# 新建模型文件必须同时在 app.db.models.__init__ 里导出，否则 autogenerate 看不见。
from app.db.models import Base  # noqa: F401
import app.db.models  # noqa: F401

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# alembic.ini 里的 sqlalchemy.url 只是占位符。
# 真正连接串走 .env -> settings，避免迁移和运行时连到不同的库。
config.set_main_option("sqlalchemy.url", settings.database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """离线模式：只根据 URL 生成 SQL，不真正连库（``alembic upgrade --sql``）。"""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,  # 字段类型变化（如 String(50)->String(100)）也纳入 autogenerate
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """在已有同步 Connection 上跑迁移。Alembic 本身是同步 API。"""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """在线模式：用 asyncpg 建连，再把同步的 Alembic 挂到这条连接上。"""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        # 迁移是一次性脚本，不需要连接池；NullPool 用完即关。
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        # Alembic 是同步的，asyncpg 连接必须通过 run_sync 切到同步上下文。
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """CLI 入口：``asyncio.run`` 包一层，因为 alembic 命令本身是同步的。"""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
