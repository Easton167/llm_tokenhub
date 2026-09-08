"""后台账号 / 后台角色 / 权限的请求与响应模型。"""

from dataclasses import dataclass
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.core.constants import DATA_SCOPE_ALL, DATA_SCOPES
from app.core.datetime_utils import UtcDateTime
from app.db.models.admin import AdminUser


@dataclass(frozen=True, slots=True)
class AdminContext:
    """管理端 JWT 会话上下文。

    ``admin_user`` 为 None 表示走的是环境变量兜底超管（数据库尚无任何管理员记录），
    此时 ``username`` 取自 ``settings.admin_username``。
    """

    username: str
    is_superadmin: bool
    permissions: frozenset[str]
    admin_user: AdminUser | None = None

    @property
    def admin_user_id(self) -> UUID | None:
        return self.admin_user.id if self.admin_user else None

    def has(self, code: str) -> bool:
        return self.is_superadmin or code in self.permissions


# --- 后台角色 ---


class AdminRoleCreate(BaseModel):
    code: str = Field(..., min_length=1, max_length=64, pattern=r"^[a-z0-9_\-]+$")
    name: str = Field(..., min_length=1, max_length=64)
    description: str | None = None
    data_scope: str = Field(default=DATA_SCOPE_ALL)
    permissions: list[str] = Field(default_factory=list)


class AdminRoleUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    description: str | None = None
    data_scope: str | None = None
    # 省略 = 不改授权；给出列表 = 整体覆盖（前端是勾选后保存，增量接口没有用武之地）
    permissions: list[str] | None = None


class AdminRoleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    code: str
    name: str
    description: str | None
    data_scope: str
    permissions: list[str] = Field(default_factory=list)
    created_at: UtcDateTime
    updated_at: UtcDateTime


# --- 后台账号 ---


class AdminAccountCreate(BaseModel):
    username: str = Field(..., min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_\-.]+$")
    password: str = Field(..., min_length=8, max_length=128)
    display_name: str | None = Field(default=None, max_length=64)
    is_superadmin: bool = False
    role_ids: list[UUID] = Field(default_factory=list)


class AdminAccountUpdate(BaseModel):
    display_name: str | None = Field(default=None, max_length=64)
    status: int | None = Field(default=None, ge=0, le=1)
    is_superadmin: bool | None = None
    role_ids: list[UUID] | None = None


class AdminPasswordReset(BaseModel):
    password: str = Field(..., min_length=8, max_length=128)


class AdminAccountOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    username: str
    display_name: str | None
    status: int
    is_superadmin: bool
    role_ids: list[UUID] = Field(default_factory=list)
    created_at: UtcDateTime
    updated_at: UtcDateTime


# --- 权限目录 / 菜单 ---


class PermissionItem(BaseModel):
    code: str
    action: str
    name: str


class PermissionGroupOut(BaseModel):
    resource: str
    name: str
    permissions: list[PermissionItem]


class AdminMenuOut(BaseModel):
    key: str
    name: str
    path: str
    icon: str


class AdminMeOut(BaseModel):
    username: str
    is_superadmin: bool
    permissions: list[str]
    menus: list[AdminMenuOut]


def validate_data_scope(value: str) -> str:
    if value not in DATA_SCOPES:
        from app.core.errors import InvalidRequestError

        raise InvalidRequestError(f"Invalid data_scope: {value}")
    return value
