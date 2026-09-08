"""用户、角色与模型授权相关表。"""

from uuid import UUID

from sqlalchemy import BigInteger, ForeignKey, Integer, SmallInteger, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class User(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """平台用户；MVP 阶段计费主体默认为 personal。"""

    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    nickname: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)  # 0 正常，1 禁用
    default_billing_subject: Mapped[str] = mapped_column(
        String(64), nullable=False, default="personal"
    )

    api_keys: Mapped[list["ApiKey"]] = relationship(back_populates="user")
    user_roles: Mapped[list["UserRole"]] = relationship(back_populates="user")
    wallet: Mapped["Wallet | None"] = relationship(
        back_populates="user",
        primaryjoin="and_(User.id==foreign(Wallet.owner_id), Wallet.owner_type=='personal')",
        uselist=False,
        viewonly=True,
    )


class Role(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """角色定义，通过 RoleModelGrant 控制可访问的模型。"""

    __tablename__ = "roles"

    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 知识库配额字段组；NULL = 使用系统默认值（见 docs/prd/knowledge-base.md「计费与配额」）
    kb_max_kb_count: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 最大知识库数/用户
    kb_max_docs_per_kb: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 单库最大文档数
    kb_max_storage_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)  # 存储上限字节/用户
    kb_max_file_size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)  # 单文件上限字节

    user_roles: Mapped[list["UserRole"]] = relationship(back_populates="role")
    model_grants: Mapped[list["RoleModelGrant"]] = relationship(back_populates="role")


class UserRole(Base, TimestampMixin):
    """用户-角色多对多关联表。"""

    __tablename__ = "user_roles"
    __table_args__ = (UniqueConstraint("user_id", "role_id", name="uq_user_roles"),)

    user_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id"), primary_key=True
    )
    role_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("roles.id"), primary_key=True
    )

    user: Mapped["User"] = relationship(back_populates="user_roles")
    role: Mapped["Role"] = relationship(back_populates="user_roles")


class RoleModelGrant(Base, TimestampMixin):
    """角色对模型的访问授权；visibility=role_restricted 时生效。"""

    __tablename__ = "role_model_grants"
    __table_args__ = (UniqueConstraint("role_id", "model_id", name="uq_role_model_grants"),)

    role_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("roles.id"), primary_key=True
    )
    model_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("models.id"), primary_key=True
    )

    role: Mapped["Role"] = relationship(back_populates="model_grants")
    model: Mapped["LLMModel"] = relationship(back_populates="role_grants")
