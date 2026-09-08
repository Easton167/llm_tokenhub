"""鉴权上下文：供 /v1/* 与用户端 REST 依赖注入复用。"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.core.datetime_utils import UtcDateTime
from app.db.models.api_key import ApiKey
from app.db.models.llm_model import LLMModel
from app.db.models.user import User


@dataclass(frozen=True, slots=True)
class AuthContext:
    """平台 API Key 鉴权上下文（模型调用 /v1/*）。

    user / api_key 是已 selectinload 的 ORM 对象，同请求会话内可继续用。
    billing_subject 是 Key 绑定快照，真正扣费主体以 BillingSubjectService 为准。
    """

    user: User
    api_key: ApiKey
    billing_subject: str

    @property
    def user_id(self) -> UUID:
        return self.user.id

    @property
    def api_key_id(self) -> UUID:
        return self.api_key.id


@dataclass(frozen=True, slots=True)
class UserContext:
    """前台 JWT 会话上下文（用户管理接口）。"""

    user: User

    @property
    def user_id(self) -> UUID:
        return self.user.id


@dataclass(frozen=True, slots=True)
class ModelAccess:
    """权限校验通过后的模型句柄。网关用 model.id 查计费规则、用 alias 打点。"""
    model: LLMModel

    @property
    def model_id(self) -> UUID:
        return self.model.id

    @property
    def model_alias(self) -> str:
        return self.model.alias


# --- Auth request/response ---


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=8, max_length=128)
    nickname: str | None = Field(default=None, max_length=255)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=1, max_length=128)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: str
    nickname: str | None
    status: int
    default_billing_subject: str
    created_at: UtcDateTime
    updated_at: UtcDateTime


# --- API Key ---


class ApiKeyCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    # None / 省略 = 未绑定，决策时走账户默认；personal 或 org:{uuid}
    billing_subject: str | None = Field(default=None, max_length=64)
    # 空列表 / 省略 = 继承角色授权；非空 = 与角色授权取交集
    allowed_models: list[str] = Field(default_factory=list)
    rpm_limit: int | None = Field(default=None, ge=1)
    tpm_limit: int | None = Field(default=None, ge=1)
    expires_at: datetime | None = None


class ApiKeyUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    # 字段省略=不改；显式 null=清除绑定
    billing_subject: str | None = Field(default=None, max_length=64)
    allowed_models: list[str] | None = None
    rpm_limit: int | None = Field(default=None, ge=1)
    tpm_limit: int | None = Field(default=None, ge=1)
    expires_at: datetime | None = None


class ApiKeyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: UUID
    name: str
    prefix: str = Field(validation_alias="key_prefix")
    status: int
    billing_subject: str | None
    allowed_models: list[str] = Field(default_factory=list)
    rpm_limit: int | None = None
    tpm_limit: int | None = None
    expires_at: UtcDateTime | None = None
    last_used_at: UtcDateTime | None
    created_at: UtcDateTime
    updated_at: UtcDateTime


class ApiKeyCreatedOut(ApiKeyOut):
    key: str
