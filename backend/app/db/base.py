"""ORM 基类与可复用列混入：统一主键与时间戳约定。"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, func
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.core.datetime_utils import utc_now


class Base(DeclarativeBase):
    """所有模型的声明式基类，供 Alembic 与 metadata 发现表结构。

    不要在这里声明公共列；主键/时间戳用下面的 Mixin，避免非表模型被误加上。
    """

    pass


class UUIDPrimaryKeyMixin:
    """UUID 主键，应用层生成，避免依赖数据库序列。"""

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )


class TimestampMixin:
    """created_at / updated_at 时间戳约定。

    updated_at 使用 Python callable onupdate（而非 func.now()）：
    SQL 表达式 onupdate 会在 flush 后过期属性，异步会话下序列化易触发 MissingGreenlet。
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )
