"""平台 API Key 鉴权：Bearer 校验与用户上下文解析。

校验失败一律 InvalidApiKeyError，不区分「不存在 / 禁用 / 过期 / 用户被封」，
避免给攻击者枚举有效 Key。明文从不落库，只存 SHA-256(pepper+plaintext)。
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.constants import BILLING_SUBJECT_PERSONAL, STATUS_ENABLED
from app.core.datetime_utils import to_utc_aware, utc_now
from app.core.errors import InvalidApiKeyError
from app.core.security import generate_api_key_material, hash_api_key, is_valid_api_key_format
from app.db.models.api_key import ApiKey
from app.schemas.auth import AuthContext


def _normalize_allowed_models(models: list[str] | None) -> list[str]:
    """去空白、去重、保序。空列表表示「不限制，继承角色授权」。"""
    if not models:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for m in models:
        alias = (m or "").strip()
        if alias and alias not in seen:
            seen.add(alias)
            out.append(alias)
    return out


class AuthService:
    """API Key 的校验与 CRUD。密码登录不在这里，见 user_service + security JWT。"""

    async def verify_api_key(self, session: AsyncSession, raw_key: str) -> AuthContext:
        """校验明文 API Key，返回用户与 Key 上下文。

        热路径：格式预检 → 哈希 → 等值查找 → 状态/过期。
        AuthContext.billing_subject 只是 Key 上的绑定快照；真正扣哪只钱包
        由 BillingSubjectService 结合请求头、账户默认、组织开关再决策。
        """
        if not is_valid_api_key_format(raw_key):
            raise InvalidApiKeyError()

        key_hash = hash_api_key(raw_key)
        stmt = (
            select(ApiKey)
            .options(selectinload(ApiKey.user))
            .where(ApiKey.key_hash == key_hash)
        )
        result = await session.execute(stmt)
        api_key = result.scalar_one_or_none()

        if api_key is None:
            raise InvalidApiKeyError()
        if api_key.status != STATUS_ENABLED:
            raise InvalidApiKeyError()
        if api_key.user.status != STATUS_ENABLED:
            raise InvalidApiKeyError()
        if api_key.expires_at is not None and to_utc_aware(api_key.expires_at) <= utc_now():
            raise InvalidApiKeyError("API key has expired")

        return AuthContext(
            user=api_key.user,
            api_key=api_key,
            # Key 绑定快照；实际扣费主体由 BillingSubjectService 在网关决策
            billing_subject=api_key.billing_subject or BILLING_SUBJECT_PERSONAL,
        )

    async def create_api_key(
        self,
        session: AsyncSession,
        *,
        user_id: UUID,
        name: str,
        billing_subject: str | None = None,
        allowed_models: list[str] | None = None,
        rpm_limit: int | None = None,
        tpm_limit: int | None = None,
        expires_at: datetime | None = None,
    ) -> tuple[ApiKey, str]:
        """创建 API Key；明文仅在此返回值中出现一次。

        billing_subject=None 表示未绑定，决策时回退账户默认策略。
        allowed_models 空 = 继承角色授权。
        """
        plaintext, key_hash, key_prefix = generate_api_key_material()
        api_key = ApiKey(
            user_id=user_id,
            key_hash=key_hash,
            key_prefix=key_prefix,
            name=name,
            billing_subject=billing_subject,
            allowed_models=_normalize_allowed_models(allowed_models),
            rpm_limit=rpm_limit,
            tpm_limit=tpm_limit,
            expires_at=to_utc_aware(expires_at) if expires_at is not None else None,
            status=STATUS_ENABLED,
        )
        session.add(api_key)
        await session.flush()
        return api_key, plaintext

    async def update_api_key(
        self,
        session: AsyncSession,
        api_key: ApiKey,
        *,
        name: str | None = None,
        update_billing_subject: bool = False,
        billing_subject: str | None = None,
        update_allowed_models: bool = False,
        allowed_models: list[str] | None = None,
        update_rpm_limit: bool = False,
        rpm_limit: int | None = None,
        update_tpm_limit: bool = False,
        tpm_limit: int | None = None,
        update_expires_at: bool = False,
        expires_at: datetime | None = None,
    ) -> ApiKey:
        """更新 Key 字段；带 update_* 标志的字段在为 True 时写入（可为 None）。"""
        if name is not None:
            api_key.name = name.strip()
        if update_billing_subject:
            api_key.billing_subject = billing_subject
        if update_allowed_models:
            api_key.allowed_models = _normalize_allowed_models(allowed_models)
        if update_rpm_limit:
            api_key.rpm_limit = rpm_limit
        if update_tpm_limit:
            api_key.tpm_limit = tpm_limit
        if update_expires_at:
            api_key.expires_at = (
                to_utc_aware(expires_at) if expires_at is not None else None
            )
        await session.flush()
        await session.refresh(api_key)
        return api_key


auth_service = AuthService()
