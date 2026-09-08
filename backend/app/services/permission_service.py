"""模型访问权限：基于角色授权矩阵与模型可见性，并与 Key 模型范围取交集。

判定顺序：
  1. 模型存在且 status=启用，否则 404（不暴露「有这个模型但你不能用」）
  2. visibility=public → 所有登录用户可用；role_restricted → 用户任一角色有 RoleModelGrant
  3. Key.allowed_models 非空则必须包含该 alias，否则 403

/v1/models 列表走同一套交集，避免「列表看得到、调用 403」。
"""

from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import STATUS_ENABLED, VISIBILITY_PUBLIC, VISIBILITY_ROLE_RESTRICTED
from app.core.errors import ModelNotAuthorizedError, ModelNotFoundError
from app.db.models.api_key import ApiKey
from app.db.models.llm_model import LLMModel
from app.db.models.user import RoleModelGrant, UserRole
from app.schemas.auth import ModelAccess
from app.schemas.openai import ModelInfo


class PermissionService:
    """无状态权限查询。不缓存授权矩阵（角色变更需要立即生效）。"""
    async def get_model_by_alias(self, session: AsyncSession, alias: str) -> LLMModel | None:
        stmt = select(LLMModel).where(LLMModel.alias == alias)
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def user_has_model_grant(
        self, session: AsyncSession, *, user_id: UUID, model_id: UUID
    ) -> bool:
        stmt = (
            select(RoleModelGrant.model_id)
            .join(UserRole, UserRole.role_id == RoleModelGrant.role_id)
            .where(UserRole.user_id == user_id, RoleModelGrant.model_id == model_id)
            .limit(1)
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none() is not None

    @staticmethod
    def _key_allows_alias(api_key: ApiKey | None, model_alias: str) -> bool:
        """空 allowed_models = 继承角色；非空则必须命中 alias。"""
        if api_key is None:
            return True
        allowed = api_key.allowed_models or []
        if not allowed:
            return True
        return model_alias in allowed

    async def check_model_access(
        self,
        session: AsyncSession,
        *,
        user_id: UUID,
        model_alias: str,
        api_key: ApiKey | None = None,
    ) -> ModelAccess:
        """校验用户是否可调用目标模型；未授权返回 403，不存在或禁用返回 404。

        api_key 非空时：allowed_models 非空则与角色授权取交集。
        """
        model = await self.get_model_by_alias(session, model_alias)
        if model is None or model.status != STATUS_ENABLED:
            raise ModelNotFoundError()

        role_ok = False
        if model.visibility == VISIBILITY_PUBLIC:
            role_ok = True
        elif model.visibility == VISIBILITY_ROLE_RESTRICTED:
            role_ok = await self.user_has_model_grant(
                session, user_id=user_id, model_id=model.id
            )

        if not role_ok:
            raise ModelNotAuthorizedError()

        if not self._key_allows_alias(api_key, model_alias):
            raise ModelNotAuthorizedError(
                f"API key is not allowed to use model '{model_alias}'"
            )

        return ModelAccess(model=model)

    async def list_authorized_models(
        self,
        session: AsyncSession,
        *,
        user_id: UUID,
        api_key: ApiKey | None = None,
    ) -> list[LLMModel]:
        """返回用户可见且已启用的模型列表，供 /v1/models 使用。

        Key 配置了 allowed_models 时只返回与角色授权的交集。
        """
        granted_model_ids = (
            select(RoleModelGrant.model_id)
            .join(UserRole, UserRole.role_id == RoleModelGrant.role_id)
            .where(UserRole.user_id == user_id)
        )
        stmt = (
            select(LLMModel)
            .where(LLMModel.status == STATUS_ENABLED)
            .where(
                or_(
                    LLMModel.visibility == VISIBILITY_PUBLIC,
                    (LLMModel.visibility == VISIBILITY_ROLE_RESTRICTED)
                    & (LLMModel.id.in_(granted_model_ids)),
                )
            )
            .order_by(LLMModel.alias)
        )
        result = await session.execute(stmt)
        models = list(result.scalars().all())

        allowed = (api_key.allowed_models or []) if api_key is not None else []
        if allowed:
            allow_set = set(allowed)
            models = [m for m in models if m.alias in allow_set]
        return models

    def to_model_info(self, model: LLMModel, *, created: int) -> ModelInfo:
        """将 LLMModel 转为 OpenAI 兼容的 ModelInfo。"""
        return ModelInfo(
            id=model.alias,
            object="model",
            created=created,
            owned_by="platform",
            model_type=model.model_type,
            capabilities=model.capabilities or {},
        )

    async def list_authorized_model_infos(
        self,
        session: AsyncSession,
        *,
        user_id: UUID,
        created: int,
        api_key: ApiKey | None = None,
    ) -> list[ModelInfo]:
        """返回用户可见且已启用的模型列表（OpenAI ModelInfo 格式）。"""
        models = await self.list_authorized_models(
            session, user_id=user_id, api_key=api_key
        )
        return [self.to_model_info(m, created=created) for m in models]


permission_service = PermissionService()
