"""后台管理账号与其 RBAC 授权。

与前台 ``users`` / ``roles`` 完全隔离：前台角色管的是「能调哪些模型、配额多少」，
后台角色管的是「能进哪些菜单、能做哪些操作」，两者语义正交，共用一张表只会
让前台注册接口变成提权入口。

权限码不建表：可授予的码由 ``app.core.admin_permissions`` 的资源目录派生，
数据库只存「某角色被授予了哪些码」。
"""

from uuid import UUID

from sqlalchemy import Boolean, ForeignKey, SmallInteger, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.constants import DATA_SCOPE_ALL, STATUS_ENABLED
from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class AdminUser(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """后台管理员账号。"""

    __tablename__ = "admin_users"

    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=STATUS_ENABLED
    )  # 0 正常，1 禁用
    # 超管跳过一切权限校验；仅超管可授予/回收该标记，否则任何能改账号的人都能自我提权
    is_superadmin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    user_roles: Mapped[list["AdminUserRole"]] = relationship(
        back_populates="admin_user", cascade="all, delete-orphan"
    )


class AdminRole(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """后台角色：一组权限码的集合。"""

    __tablename__ = "admin_roles"

    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 预留字段，当前所有查询仍是全量；取值见 core.constants.DATA_SCOPES
    data_scope: Mapped[str] = mapped_column(
        String(32), nullable=False, default=DATA_SCOPE_ALL
    )

    permissions: Mapped[list["AdminRolePermission"]] = relationship(
        back_populates="role", cascade="all, delete-orphan"
    )
    user_roles: Mapped[list["AdminUserRole"]] = relationship(
        back_populates="role", cascade="all, delete-orphan"
    )


class AdminUserRole(Base, TimestampMixin):
    """后台账号-角色多对多关联表。"""

    __tablename__ = "admin_user_roles"
    __table_args__ = (
        UniqueConstraint("admin_user_id", "role_id", name="uq_admin_user_roles"),
    )

    admin_user_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("admin_users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    role_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("admin_roles.id", ondelete="CASCADE"),
        primary_key=True,
    )

    admin_user: Mapped["AdminUser"] = relationship(back_populates="user_roles")
    role: Mapped["AdminRole"] = relationship(back_populates="user_roles")


class AdminRolePermission(Base, TimestampMixin):
    """角色被授予的权限码；code 的合法取值见 admin_permissions.ALL_PERMISSIONS。"""

    __tablename__ = "admin_role_permissions"

    role_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("admin_roles.id", ondelete="CASCADE"),
        primary_key=True,
    )
    code: Mapped[str] = mapped_column(String(64), primary_key=True)

    role: Mapped["AdminRole"] = relationship(back_populates="permissions")
