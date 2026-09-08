"""知识库全局配置：DB 单例 + 环境变量回退。"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.constants import MODEL_TYPE_EMBEDDING, STATUS_ENABLED
from app.core.errors import InvalidRequestError
from app.db.models.llm_model import LLMModel
from app.db.models.system import KbGlobalSettings
from app.services.billing_service import billing_service
from app.services.embedding_gateway import reset_embed_semaphore_for_tests

SINGLETON_ID = 1


@dataclass(frozen=True, slots=True)
class EffectiveKbSettings:
    """对外生效的全局知识库参数（已合并 env 默认）。"""

    default_embedding_model: str
    max_file_size_bytes: int
    upload_batch_limit: int
    embedding_batch_size: int
    embedding_max_concurrent: int
    # 原始 DB 覆盖值（NULL = 使用 env），供管理端展示
    overrides: dict[str, str | int | None]


class KbSettingsService:
    async def ensure_row(self, db: AsyncSession) -> KbGlobalSettings:
        row = await db.get(KbGlobalSettings, SINGLETON_ID)
        if row is None:
            row = KbGlobalSettings(id=SINGLETON_ID)
            db.add(row)
            await db.flush()
        return row

    async def get_row(self, db: AsyncSession) -> KbGlobalSettings:
        return await self.ensure_row(db)

    async def get_effective(self, db: AsyncSession) -> EffectiveKbSettings:
        row = await self.ensure_row(db)
        return EffectiveKbSettings(
            default_embedding_model=(
                (row.default_embedding_model or settings.kb_default_embedding_model or "")
                .strip()
            ),
            max_file_size_bytes=(
                row.max_file_size_bytes
                if row.max_file_size_bytes is not None
                else settings.kb_default_max_file_size_bytes
            ),
            upload_batch_limit=(
                row.upload_batch_limit
                if row.upload_batch_limit is not None
                else settings.kb_upload_batch_limit
            ),
            embedding_batch_size=(
                row.embedding_batch_size
                if row.embedding_batch_size is not None
                else settings.kb_embedding_batch_size
            ),
            embedding_max_concurrent=(
                row.embedding_max_concurrent
                if row.embedding_max_concurrent is not None
                else settings.kb_embedding_max_concurrent
            ),
            overrides={
                "default_embedding_model": row.default_embedding_model,
                "max_file_size_bytes": row.max_file_size_bytes,
                "upload_batch_limit": row.upload_batch_limit,
                "embedding_batch_size": row.embedding_batch_size,
                "embedding_max_concurrent": row.embedding_max_concurrent,
            },
        )

    async def validate_embedding_model(
        self, db: AsyncSession, *, alias: str
    ) -> LLMModel:
        alias = (alias or "").strip()
        if not alias:
            raise InvalidRequestError("default_embedding_model must not be empty")
        from sqlalchemy import select

        model = (
            await db.execute(select(LLMModel).where(LLMModel.alias == alias))
        ).scalar_one_or_none()
        if model is None:
            raise InvalidRequestError(f"Embedding model '{alias}' not found")
        if model.model_type != MODEL_TYPE_EMBEDDING:
            raise InvalidRequestError(
                f"Model '{alias}' is not an embedding model (model_type={model.model_type})"
            )
        if model.status != STATUS_ENABLED:
            raise InvalidRequestError(f"Embedding model '{alias}' is disabled")
        await billing_service.require_active_rule(db, model_id=model.id)
        return model

    async def update(
        self,
        db: AsyncSession,
        *,
        patch: dict,
    ) -> tuple[KbGlobalSettings, EffectiveKbSettings, dict]:
        """应用部分更新；返回 (row, effective, changed_detail)。"""
        row = await self.ensure_row(db)
        before = {
            "default_embedding_model": row.default_embedding_model,
            "max_file_size_bytes": row.max_file_size_bytes,
            "upload_batch_limit": row.upload_batch_limit,
            "embedding_batch_size": row.embedding_batch_size,
            "embedding_max_concurrent": row.embedding_max_concurrent,
        }
        changed: dict = {}

        if "default_embedding_model" in patch:
            value = patch["default_embedding_model"]
            if value is not None:
                value = str(value).strip() or None
            if value:
                await self.validate_embedding_model(db, alias=value)
            if value != row.default_embedding_model:
                changed["default_embedding_model"] = {
                    "from": row.default_embedding_model,
                    "to": value,
                }
            row.default_embedding_model = value

        for key in (
            "max_file_size_bytes",
            "upload_batch_limit",
            "embedding_batch_size",
            "embedding_max_concurrent",
        ):
            if key not in patch:
                continue
            value = patch[key]
            if value is not None and int(value) <= 0:
                raise InvalidRequestError(f"{key} must be > 0")
            int_value = int(value) if value is not None else None
            if int_value != getattr(row, key):
                changed[key] = {"from": getattr(row, key), "to": int_value}
            setattr(row, key, int_value)

        await db.flush()
        effective = await self.get_effective(db)

        # 进程内生效：并发闸按新值重建；其余读取走 get_effective
        if "embedding_max_concurrent" in changed:
            reset_embed_semaphore_for_tests(
                max_concurrent=effective.embedding_max_concurrent
            )

        _ = before
        return row, effective, changed


kb_settings_service = KbSettingsService()
