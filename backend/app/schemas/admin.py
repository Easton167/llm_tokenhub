"""管理 API 请求/响应 Pydantic 模型。"""

from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator, model_validator

from app.core.datetime_utils import UtcDateTime


class AdminSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --- Provider ---


class ProviderCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    adapter_type: str = Field(..., min_length=1, max_length=64)
    default_base_url: str | None = Field(default=None, max_length=512)


class ProviderUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    adapter_type: str | None = Field(default=None, min_length=1, max_length=64)
    default_base_url: str | None = Field(default=None, max_length=512)


class ProviderOut(AdminSchema):
    id: UUID
    name: str
    adapter_type: str
    default_base_url: str | None
    created_at: UtcDateTime
    updated_at: UtcDateTime


# --- Channel ---


class ChannelCreate(BaseModel):
    """创建渠道；未传 adapter_type / base_url 时继承所属 Provider。"""

    provider_id: UUID
    name: str = Field(..., min_length=1, max_length=255)
    base_url: str | None = Field(
        default=None,
        min_length=1,
        max_length=512,
        description="省略则使用 Provider.default_base_url",
    )
    adapter_type: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        description="省略则使用 Provider.adapter_type",
    )
    priority: int = 0
    weight: int = Field(default=1, ge=1)
    timeout_ms: int = Field(default=120_000, ge=1)
    max_retries: int = Field(default=2, ge=0)
    status: int = 0


class ChannelUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    base_url: str | None = Field(default=None, min_length=1, max_length=512)
    adapter_type: str | None = Field(default=None, min_length=1, max_length=64)
    priority: int | None = None
    weight: int | None = Field(default=None, ge=1)
    timeout_ms: int | None = Field(default=None, ge=1)
    max_retries: int | None = Field(default=None, ge=0)
    status: int | None = None


class ChannelOut(AdminSchema):
    id: UUID
    provider_id: UUID
    name: str
    base_url: str
    adapter_type: str
    priority: int
    weight: int
    status: int
    health_status: int
    fail_count: int
    timeout_ms: int
    max_retries: int
    created_at: UtcDateTime
    updated_at: UtcDateTime


class ChannelTestOut(BaseModel):
    status: str
    message: str


# --- Channel Key ---


class ChannelKeyCreate(BaseModel):
    api_key: str = Field(..., min_length=1, max_length=512)
    weight: int = Field(default=1, ge=1)
    rpm_limit: int | None = Field(default=None, ge=1)
    tpm_limit: int | None = Field(default=None, ge=1)


class ChannelKeyUpdate(BaseModel):
    weight: int | None = Field(default=None, ge=1)
    rpm_limit: int | None = Field(default=None, ge=1)
    tpm_limit: int | None = Field(default=None, ge=1)
    status: int | None = None


class ChannelKeyOut(AdminSchema):
    id: UUID
    channel_id: UUID
    key_masked: str
    weight: int
    rpm_limit: int | None
    tpm_limit: int | None
    status: int
    health_status: int
    created_at: UtcDateTime
    updated_at: UtcDateTime


# --- Model ---


class ModelCreate(BaseModel):
    alias: str = Field(..., min_length=1, max_length=128)
    display_name: str = Field(..., min_length=1, max_length=255)
    model_type: str = Field(default="chat", max_length=64)
    capabilities: dict = Field(default_factory=dict)
    visibility: str = Field(default="role_restricted", max_length=64)
    status: int = 0


class ModelUpdate(BaseModel):
    alias: str | None = Field(default=None, min_length=1, max_length=128)
    display_name: str | None = Field(default=None, min_length=1, max_length=255)
    model_type: str | None = Field(default=None, max_length=64)
    capabilities: dict | None = None
    visibility: str | None = Field(default=None, max_length=64)
    status: int | None = None


class ModelOut(AdminSchema):
    id: UUID
    alias: str
    display_name: str
    model_type: str
    capabilities: dict
    visibility: str
    status: int
    created_at: UtcDateTime
    updated_at: UtcDateTime


# --- Model Mapping ---


class ModelMappingCreate(BaseModel):
    channel_id: UUID
    upstream_model_name: str = Field(..., min_length=1, max_length=255)
    weight: int = Field(default=1, ge=1)
    status: int = 0


class ModelMappingUpdate(BaseModel):
    upstream_model_name: str | None = Field(default=None, min_length=1, max_length=255)
    weight: int | None = Field(default=None, ge=1)
    status: int | None = None


class ModelMappingOut(AdminSchema):
    id: UUID
    model_id: UUID
    channel_id: UUID
    upstream_model_name: str
    weight: int
    status: int
    created_at: UtcDateTime
    updated_at: UtcDateTime


# --- Billing Rule ---


class BillingRuleCreate(BaseModel):
    price_input: int = Field(..., ge=0)
    price_output: int = Field(..., ge=0)
    price_cached_input: int = Field(default=0, ge=0)
    price_per_call: int = Field(default=0, ge=0)
    cost_input: int = Field(default=0, ge=0)
    cost_output: int = Field(default=0, ge=0)
    multiplier: Decimal = Field(default=Decimal("1.000"))
    min_charge_unit: int = Field(default=1, ge=1)
    currency: str = Field(default="CNY", max_length=16)
    effective_at: datetime

    @field_validator(
        "price_cached_input",
        "price_per_call",
        "cost_input",
        "cost_output",
        mode="before",
    )
    @classmethod
    def _null_int_to_default(cls, v: object) -> object:
        # 前端 InputNumber 清空时会传 null，按缺省 0 处理
        return 0 if v is None else v

    @field_validator("multiplier", mode="before")
    @classmethod
    def _empty_multiplier_to_default(cls, v: object) -> object:
        if v is None or v == "":
            return Decimal("1.000")
        return v

    @field_validator("min_charge_unit", mode="before")
    @classmethod
    def _null_min_charge_to_default(cls, v: object) -> object:
        return 1 if v is None else v

    @field_validator("currency", mode="before")
    @classmethod
    def _empty_currency_to_default(cls, v: object) -> object:
        if v is None or v == "":
            return "CNY"
        return v


class BillingRuleUpdate(BaseModel):
    status: int


class BillingRuleOut(AdminSchema):
    id: UUID
    model_id: UUID
    version: int
    price_input: int
    price_output: int
    price_cached_input: int
    price_per_call: int
    cost_input: int
    cost_output: int
    multiplier: Decimal
    min_charge_unit: int
    currency: str
    effective_at: UtcDateTime
    status: int
    created_at: UtcDateTime
    updated_at: UtcDateTime


# --- Role ---


class RoleCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    description: str | None = None
    # 知识库配额；NULL/省略 = 使用系统默认
    kb_max_kb_count: int | None = Field(default=None, ge=0)
    kb_max_docs_per_kb: int | None = Field(default=None, ge=0)
    kb_max_storage_bytes: int | None = Field(default=None, ge=0)
    kb_max_file_size_bytes: int | None = Field(default=None, ge=0)


class RoleUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    description: str | None = None
    kb_max_kb_count: int | None = Field(default=None, ge=0)
    kb_max_docs_per_kb: int | None = Field(default=None, ge=0)
    kb_max_storage_bytes: int | None = Field(default=None, ge=0)
    kb_max_file_size_bytes: int | None = Field(default=None, ge=0)


class RoleOut(AdminSchema):
    id: UUID
    name: str
    description: str | None
    kb_max_kb_count: int | None
    kb_max_docs_per_kb: int | None
    kb_max_storage_bytes: int | None
    kb_max_file_size_bytes: int | None
    created_at: UtcDateTime
    updated_at: UtcDateTime


class RoleModelGrantCreate(BaseModel):
    model_id: UUID


class RoleModelGrantOut(AdminSchema):
    role_id: UUID
    model_id: UUID
    created_at: UtcDateTime
    updated_at: UtcDateTime


class UserOut(AdminSchema):
    id: UUID
    email: str
    nickname: str | None
    status: int
    default_billing_subject: str
    created_at: UtcDateTime
    updated_at: UtcDateTime


class UserPasswordReset(BaseModel):
    password: str = Field(..., min_length=8, max_length=128)


class UserRoleAssign(BaseModel):
    role_id: UUID


class UserRoleOut(AdminSchema):
    user_id: UUID
    role_id: UUID
    created_at: UtcDateTime
    updated_at: UtcDateTime


# --- Wallet ---


class WalletOut(AdminSchema):
    id: UUID
    owner_type: str
    owner_id: UUID
    balance: int  # 现金池可用；语义已收窄，不再表示全部余额
    held: int  # 现金池冻结
    gift_balance: int = 0  # 赠送池可用，不可退不可提
    gift_held: int = 0  # 赠送池冻结
    currency: str
    credit_limit: int
    created_at: UtcDateTime
    updated_at: UtcDateTime

    @computed_field
    @property
    def total_available(self) -> int:
        """总可用额 = 赠送池 + 现金池 + 授信。"""
        return self.gift_balance + self.balance + self.credit_limit


class WalletRecharge(BaseModel):
    amount: int = Field(..., ge=1, description="充值金额（最小货币单位，必须 > 0）")
    remark: str | None = Field(default=None, max_length=500)


class WalletAdjust(BaseModel):
    amount: int = Field(..., description="调账金额（正=增加，负=减少），绝对值不超过当前余额")
    remark: str | None = Field(default=None, max_length=500)


GiftReason = Literal["promotion", "compensation", "internal", "partnership", "other"]


class GiftGrantCreate(BaseModel):
    """后台赠送请求。expires_in_days / expires_at / never_expires 三选一。"""

    amount: int = Field(..., gt=0, description="赠送金额（最小货币单位，必须 > 0）")
    expires_in_days: int | None = Field(default=None, ge=1, description="N 天后到期（业务时区当日 23:59:59）")
    expires_at: datetime | None = Field(default=None, description="指定到期时刻（存 UTC）")
    never_expires: bool = Field(default=False, description="永久有效；不传到期字段时必须显式为 true")
    reason: GiftReason
    remark: str | None = Field(default=None, max_length=500)
    idempotency_key: str = Field(..., min_length=1, max_length=128)

    @model_validator(mode="after")
    def _require_explicit_expiry(self) -> "GiftGrantCreate":
        specified = [
            self.expires_in_days is not None,
            self.expires_at is not None,
            self.never_expires,
        ]
        if sum(1 for flag in specified if flag) > 1:
            raise ValueError("expires_in_days、expires_at、never_expires 只能选其一")
        if not any(specified):
            raise ValueError("必须指定 expires_in_days 或 expires_at，或显式传入 never_expires=true")
        return self


class GiftRevoke(BaseModel):
    remark: str | None = Field(default=None, max_length=500)


class GiftGrantOut(AdminSchema):
    id: UUID
    wallet_id: UUID
    amount: int
    remaining: int
    reason: str
    expires_at: UtcDateTime | None
    status: str
    granted_by: UUID
    revoked_by: UUID | None
    revoked_at: UtcDateTime | None
    idempotency_key: str
    remark: str | None
    created_at: UtcDateTime
    updated_at: UtcDateTime


class GiftGrantPublicOut(AdminSchema):
    """前台只读批次：不暴露操作人。"""

    id: UUID
    amount: int
    remaining: int
    reason: str
    expires_at: UtcDateTime | None
    status: str
    created_at: UtcDateTime


class WalletTransactionOut(AdminSchema):
    id: UUID
    wallet_id: UUID
    type: str
    amount: int
    gift_amount: int = 0  # 其中赠送池部分，符号与 amount 一致；纯现金为 0
    balance_after: int
    held_after: int
    ref_request_id: str | None
    org_id: UUID | None = None
    operator_user_id: UUID | None
    remark: str | None
    created_at: UtcDateTime


# --- Usage / Request logs ---


class RequestLogOut(AdminSchema):
    id: UUID
    request_id: str
    # 内部调用（kb_ingest / kb_query）可不占用用户 API Key
    api_key_id: UUID | None
    user_id: UUID
    billing_subject: str
    model_id: UUID
    channel_id: UUID | None
    status: str
    http_status: int
    latency_ms: int | None
    is_stream: bool
    error_code: str | None
    created_at: UtcDateTime


class UsageLogOut(AdminSchema):
    id: UUID
    request_id: str
    user_id: UUID | None = None
    model_id: UUID | None = None
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    billing_rule_version: int
    cost_amount: int
    charge_amount: int
    gift_charge_amount: int = 0
    cash_charge_amount: int = 0
    is_estimated: bool
    # chat / api / kb_ingest / kb_query；存量可能为 null（按 api 展示）
    source: str | None = None
    created_at: UtcDateTime


class UsageSummaryOut(BaseModel):
    model_id: UUID | None = None
    user_id: UUID | None = None
    request_count: int
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    cost_amount: int
    charge_amount: int
    gift_charge_amount: int = 0
    cash_charge_amount: int = 0


# --- Channel Cost ---


class ChannelCostCreate(BaseModel):
    upstream_model_name: str = Field(..., min_length=1, max_length=255)
    cost_input: int = Field(..., ge=0, description="每 1M 输入 token 成本（最小货币单位）")
    cost_output: int = Field(..., ge=0, description="每 1M 输出 token 成本（最小货币单位）")
    currency: str = Field(default="CNY", max_length=16)
    effective_at: datetime


class ChannelCostUpdate(BaseModel):
    status: int


class ChannelCostOut(AdminSchema):
    id: UUID
    channel_id: UUID
    upstream_model_name: str
    version: int
    cost_input: int
    cost_output: int
    currency: str
    effective_at: UtcDateTime
    status: int
    created_at: UtcDateTime
    updated_at: UtcDateTime


# --- Daily aggregate ---


class DailyAggregateRequest(BaseModel):
    summary_date: date = Field(..., description="UTC 日历日 YYYY-MM-DD")
    dry_run: bool = False


class DailyAggregateOut(BaseModel):
    summary_date: date
    deleted: int
    inserted: int
    revenue_total: int
    cost_total: int
    cost_missing_rows: int


# --- Knowledge base admin ---


class KbGlobalSettingsOut(BaseModel):
    """管理端展示：覆盖值 + 生效值。"""

    default_embedding_model: str | None = None
    max_file_size_bytes: int | None = None
    upload_batch_limit: int | None = None
    embedding_batch_size: int | None = None
    embedding_max_concurrent: int | None = None
    effective_default_embedding_model: str
    effective_max_file_size_bytes: int
    effective_upload_batch_limit: int
    effective_embedding_batch_size: int
    effective_embedding_max_concurrent: int


class KbGlobalSettingsUpdate(BaseModel):
    default_embedding_model: str | None = Field(default=None, max_length=128)
    max_file_size_bytes: int | None = Field(default=None, ge=1)
    upload_batch_limit: int | None = Field(default=None, ge=1)
    embedding_batch_size: int | None = Field(default=None, ge=1)
    embedding_max_concurrent: int | None = Field(default=None, ge=1)


class AdminKnowledgeBaseOut(AdminSchema):
    """管理端知识库元数据；不含文档正文与分块内容。"""

    id: UUID
    owner_user_id: UUID
    owner_email: str | None = None
    name: str
    description: str | None
    embedding_model_id: UUID
    embedding_model_alias: str | None = None
    embedding_dim: int
    doc_count: int
    chunk_count: int
    storage_bytes: int
    status: str
    created_at: UtcDateTime
    updated_at: UtcDateTime


class AdminKbUserStatsOut(BaseModel):
    user_id: UUID
    email: str | None = None
    kb_count: int
    doc_count: int
    storage_bytes: int
    embed_input_tokens: int  # 向量化累计用量（kb_ingest）
