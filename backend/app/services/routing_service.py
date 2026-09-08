"""路由服务：按模型别名 → 渠道映射 → 优先级/权重选择具体渠道和上游 Key。

路由规则（参考 TECH_DESIGN.md 8.2）：
  1. 过滤未启用的模型、Mapping、Channel、ChannelKey。
  2. 过滤熔断（health_status != 0）的 Channel 和 Key。
  3. 过滤 RPM/TPM 已打满的渠道 Key（本轮跳过）。
  4. 按 Channel.priority 从高到低分组。
  5. 从最高优先级组内按权重加权随机选出一个 Channel（含 Mapping）。
  6. 在选定 Channel 内按权重加权随机选出一个 ChannelKey，消费限额并解密。
  7. 若当前优先级组选出的渠道/Key 均不可用，下沉到次级优先级组继续选择。
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.adapters.base import RouteCandidate
from app.core.channel_key_crypto import decrypt_channel_key
from app.core.constants import STATUS_ENABLED
from app.core.errors import AppError
from app.db.models.channel import Channel, ChannelKey
from app.db.models.llm_model import LLMModel, ModelChannelMap
from app.services.rate_limit_service import rate_limit_service

# health_status 常量（与 Channel/ChannelKey 字段保持一致）
HEALTH_OK = 0        # 健康
HEALTH_BROKEN = 1    # 熔断中
HEALTH_HALF_OPEN = 2  # 半开（可试探性流量）

_AVAILABLE_HEALTH_STATUSES = {HEALTH_OK, HEALTH_HALF_OPEN}
_MAX_RATE_SKIP_ATTEMPTS = 16


class NoAvailableChannelError(AppError):
    """无满足条件的可用渠道时抛出。"""

    def __init__(self, message: str = "No available channel for this model") -> None:
        super().__init__(
            message,
            code="no_available_channel",
            error_type="routing_error",
            status_code=503,
        )


@dataclass
class _MappingWithChannel:
    """路由候选项：包含映射关系 + 渠道对象。"""

    mapping: ModelChannelMap
    channel: Channel


def _weighted_random(candidates: list[tuple[any, int]]) -> any:
    """加权随机选择。

    Args:
        candidates: [(item, weight), ...]，weight 必须 > 0。

    Returns:
        被选中的 item。
    """
    total = sum(w for _, w in candidates)
    r = random.uniform(0, total)
    cumulative = 0.0
    for item, weight in candidates:
        cumulative += weight
        if r <= cumulative:
            return item
    return candidates[-1][0]  # 浮点边界兜底


class RoutingService:
    """根据模型别名与路由规则选出目标渠道和上游 Key。

    设计为无状态服务单例；健康状态由数据库字段（health_status）持久化，
    渠道 Key 限额由 rate_limit_service 判定。
    """

    async def pick_route(
        self,
        session: AsyncSession,
        model_alias: str,
        *,
        exclude_key_ids: set[str] | None = None,
        estimated_tokens: int = 1,
    ) -> RouteCandidate:
        """为指定模型别名选出可用的渠道 + 上游 Key。

        选中后会 ``try_consume_channel_key`` 占用该 Key 的 RPM/TPM。
        失败重试时由调用方把 channel_key_id 放进 exclude_key_ids。

        Args:
            exclude_key_ids: 已失败、需跳过的 ChannelKey ID 集合（故障转移重试时使用）。
            estimated_tokens: 用于渠道 Key TPM 限额判断的估算 token。

        Raises:
            NoAvailableChannelError: 无健康可用渠道时抛出（503）。
            ModelNotFoundError: alias 不存在或模型已禁用。
        """
        model = await self._get_enabled_model(session, model_alias)
        candidates = await self._load_candidates(session, model.id)

        if not candidates:
            raise NoAvailableChannelError(
                f"Model '{model_alias}' has no enabled channel mappings."
            )

        excluded = set(exclude_key_ids or set())
        tokens = max(1, estimated_tokens)

        for _ in range(_MAX_RATE_SKIP_ATTEMPTS):
            selected_mapping_chan = await self._select_channel(
                candidates,
                exclude_key_ids=excluded,
                estimated_tokens=tokens,
            )
            selected_key = await self._select_and_consume_key(
                selected_mapping_chan.channel,
                exclude_key_ids=excluded,
                estimated_tokens=tokens,
            )
            if selected_key is None:
                # 本渠道 Key 均打满或 peek 后被并发抢走：排除该渠道全部 Key，促使换渠道。
                # 循环上限 _MAX_RATE_SKIP_ATTEMPTS，避免限额打满时死循环。
                for k in selected_mapping_chan.channel.channel_keys:
                    excluded.add(str(k.id))
                continue

            upstream_key_plain = decrypt_channel_key(selected_key.api_key_encrypted)
            # 明文只活在这次 RouteCandidate 里，随请求结束丢弃，不写日志
            return RouteCandidate(
                channel_id=str(selected_mapping_chan.channel.id),
                channel_name=selected_mapping_chan.channel.name,
                base_url=selected_mapping_chan.channel.base_url,
                adapter_type=selected_mapping_chan.channel.adapter_type,
                timeout_ms=selected_mapping_chan.channel.timeout_ms,
                max_retries=selected_mapping_chan.channel.max_retries,
                upstream_key=upstream_key_plain,
                upstream_model_name=selected_mapping_chan.mapping.upstream_model_name,
                channel_key_id=str(selected_key.id),
            )

        raise NoAvailableChannelError(
            "No channel with available keys under rate limits for this model."
        )

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    async def _get_enabled_model(
        self, session: AsyncSession, alias: str
    ) -> LLMModel:
        stmt = select(LLMModel).where(
            LLMModel.alias == alias,
            LLMModel.status == STATUS_ENABLED,
        )
        result = await session.execute(stmt)
        model = result.scalar_one_or_none()
        if model is None:
            from app.core.errors import ModelNotFoundError
            raise ModelNotFoundError(f"Model '{alias}' not found or disabled.")
        return model

    async def _load_candidates(
        self, session: AsyncSession, model_id: UUID
    ) -> list[_MappingWithChannel]:
        """加载模型对应的所有可用渠道候选（含预加载 channel_keys）。"""
        stmt = (
            select(ModelChannelMap)
            .where(
                ModelChannelMap.model_id == model_id,
                ModelChannelMap.status == STATUS_ENABLED,
            )
            .options(
                selectinload(ModelChannelMap.channel).selectinload(Channel.channel_keys)
            )
        )
        result = await session.execute(stmt)
        mappings = result.scalars().all()

        candidates: list[_MappingWithChannel] = []
        for mapping in mappings:
            channel = mapping.channel
            if not self._is_channel_available(channel):
                continue
            if not self._has_available_keys(channel):
                continue
            candidates.append(_MappingWithChannel(mapping=mapping, channel=channel))

        return candidates

    @staticmethod
    def _is_channel_available(channel: Channel) -> bool:
        return (
            channel.status == STATUS_ENABLED
            and channel.health_status in _AVAILABLE_HEALTH_STATUSES
        )

    @staticmethod
    def _has_available_keys(
        channel: Channel,
        exclude_key_ids: set[str] | None = None,
    ) -> bool:
        """仅按 status/health 判断（同步，供单测与初筛）。"""
        excluded = exclude_key_ids or set()
        return any(
            k.status == STATUS_ENABLED
            and k.health_status in _AVAILABLE_HEALTH_STATUSES
            and str(k.id) not in excluded
            for k in channel.channel_keys
        )

    async def _key_under_rate_limit(
        self, key: ChannelKey, *, estimated_tokens: int
    ) -> bool:
        return await rate_limit_service.peek_channel_key_available(
            channel_key_id=str(key.id),
            rpm_limit=key.rpm_limit,
            tpm_limit=key.tpm_limit,
            estimated_tokens=estimated_tokens,
        )

    async def _channel_has_routable_keys(
        self,
        channel: Channel,
        *,
        exclude_key_ids: set[str] | None = None,
        estimated_tokens: int = 1,
    ) -> bool:
        excluded = exclude_key_ids or set()
        for k in channel.channel_keys:
            if (
                k.status != STATUS_ENABLED
                or k.health_status not in _AVAILABLE_HEALTH_STATUSES
                or str(k.id) in excluded
            ):
                continue
            if await self._key_under_rate_limit(k, estimated_tokens=estimated_tokens):
                return True
        return False

    async def _select_channel(
        self,
        candidates: list[_MappingWithChannel],
        *,
        exclude_key_ids: set[str] | None = None,
        estimated_tokens: int = 1,
    ) -> _MappingWithChannel:
        """按优先级分组，从最高优先级组内加权随机选出仍有可用 Key 的 Channel。

        不会跨优先级混权重：priority=10 的渠道只要还有 Key，就不会落到 priority=1。
        组内权重来自 ModelChannelMap.weight（同一渠道映射到不同模型可以不同权重）。
        """
        eligible: list[_MappingWithChannel] = []
        for c in candidates:
            if await self._channel_has_routable_keys(
                c.channel,
                exclude_key_ids=exclude_key_ids,
                estimated_tokens=estimated_tokens,
            ):
                eligible.append(c)
        if not eligible:
            raise NoAvailableChannelError(
                "No channel with available keys for this model."
            )

        max_priority = max(c.channel.priority for c in eligible)
        top_group = [c for c in eligible if c.channel.priority == max_priority]
        weighted = [(c, c.mapping.weight) for c in top_group]
        return _weighted_random(weighted)

    async def _select_and_consume_key(
        self,
        channel: Channel,
        *,
        exclude_key_ids: set[str] | None = None,
        estimated_tokens: int = 1,
    ) -> ChannelKey | None:
        """在渠道内加权随机选出 Key 并尝试消费限额；失败返回 None。"""
        excluded = exclude_key_ids or set()
        available_keys: list[ChannelKey] = []
        for k in channel.channel_keys:
            if (
                k.status != STATUS_ENABLED
                or k.health_status not in _AVAILABLE_HEALTH_STATUSES
                or str(k.id) in excluded
            ):
                continue
            if await self._key_under_rate_limit(k, estimated_tokens=estimated_tokens):
                available_keys.append(k)
        if not available_keys:
            return None

        weighted = [(k, k.weight) for k in available_keys]
        selected = _weighted_random(weighted)
        ok = await rate_limit_service.try_consume_channel_key(
            channel_key_id=str(selected.id),
            rpm_limit=selected.rpm_limit,
            tpm_limit=selected.tpm_limit,
            estimated_tokens=estimated_tokens,
        )
        if not ok:
            return None
        return selected

    def _select_key(
        self,
        channel: Channel,
        *,
        exclude_key_ids: set[str] | None = None,
    ) -> ChannelKey:
        """在渠道内按权重加权随机选出一个可用 Key（不含限额，供兼容单测）。"""
        excluded = exclude_key_ids or set()
        available_keys = [
            k
            for k in channel.channel_keys
            if k.status == STATUS_ENABLED
            and k.health_status in _AVAILABLE_HEALTH_STATUSES
            and str(k.id) not in excluded
        ]
        if not available_keys:
            raise NoAvailableChannelError(
                f"Channel '{channel.name}' has no available keys."
            )
        weighted = [(k, k.weight) for k in available_keys]
        return _weighted_random(weighted)


routing_service = RoutingService()
