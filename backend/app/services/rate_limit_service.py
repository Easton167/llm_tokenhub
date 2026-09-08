"""Key 级限流：Redis 滑动窗口；无 Redis / 连接失败时降级为进程内限流。

两层限额：
- 平台 API Key（enforce_*）：超限抛 429，发生在 hold 之前。
- 渠道 ChannelKey（peek / try_consume）：打满则本轮跳过该 Key，换渠道路由，不向用户 429。

窗口固定 60 秒。RPM 按请求次数；TPM 按估算 token 累加（预扣用的同一估算）。
Redis 用 Lua 保证「清理过期 + 判断 + 写入」原子，避免并发超卖。
进程内后端按 key 加 asyncio.Lock，仅单 worker 准确；多进程必须上 Redis。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass

from app.core.config import settings
from app.core.errors import RateLimitExceededError
from app.core.logging import get_logger

logger = get_logger(__name__)

_WINDOW_SECONDS = 60.0

# Redis Lua：原子清理过期成员 → 计数/求和 → 超限则返回剩余秒数，否则写入
_LUA_RPM = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local member = ARGV[4]
redis.call('ZREMRANGEBYSCORE', key, 0, now - window)
local count = redis.call('ZCARD', key)
if count >= limit then
  local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
  local retry = window
  if oldest[2] then
    retry = math.max(1, math.ceil(tonumber(oldest[2]) + window - now))
  end
  return {0, retry}
end
redis.call('ZADD', key, now, member)
redis.call('EXPIRE', key, math.ceil(window) + 1)
return {1, 0}
"""

_LUA_TPM = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local tokens = tonumber(ARGV[4])
local member = ARGV[5]
redis.call('ZREMRANGEBYSCORE', key, 0, now - window)
local members = redis.call('ZRANGE', key, 0, -1, 'WITHSCORES')
local total = 0
for i = 1, #members, 2 do
  local m = members[i]
  local sep = string.find(m, ':', 1, true)
  if sep then
    total = total + (tonumber(string.sub(m, sep + 1)) or 0)
  end
end
if total + tokens > limit then
  local retry = window
  if members[2] then
    retry = math.max(1, math.ceil(tonumber(members[2]) + window - now))
  end
  return {0, retry}
end
redis.call('ZADD', key, now, member .. ':' .. tostring(tokens))
redis.call('EXPIRE', key, math.ceil(window) + 1)
return {1, 0}
"""

_LUA_RPM_PEEK = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
redis.call('ZREMRANGEBYSCORE', key, 0, now - window)
local count = redis.call('ZCARD', key)
if count >= limit then return 0 else return 1 end
"""

_LUA_TPM_PEEK = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local tokens = tonumber(ARGV[4])
redis.call('ZREMRANGEBYSCORE', key, 0, now - window)
local members = redis.call('ZRANGE', key, 0, -1)
local total = 0
for i = 1, #members do
  local m = members[i]
  local sep = string.find(m, ':', 1, true)
  if sep then
    total = total + (tonumber(string.sub(m, sep + 1)) or 0)
  end
end
if total + tokens > limit then return 0 else return 1 end
"""


@dataclass(frozen=True, slots=True)
class _ConsumeResult:
    allowed: bool
    retry_after: int = 60


class _InMemoryWindow:
    """进程内滑动窗口；按 key 加锁保证并发安全。"""

    def __init__(self) -> None:
        self._rpm: dict[str, deque[float]] = defaultdict(deque)
        self._tpm: dict[str, deque[tuple[float, int]]] = defaultdict(deque)
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    def _purge_rpm(self, key: str, now: float) -> None:
        q = self._rpm[key]
        cutoff = now - _WINDOW_SECONDS
        while q and q[0] < cutoff:
            q.popleft()

    def _purge_tpm(self, key: str, now: float) -> None:
        q = self._tpm[key]
        cutoff = now - _WINDOW_SECONDS
        while q and q[0][0] < cutoff:
            q.popleft()

    async def consume_rpm(self, key: str, limit: int) -> _ConsumeResult:
        async with self._locks[f"rpm:{key}"]:
            now = time.monotonic()
            self._purge_rpm(key, now)
            q = self._rpm[key]
            if len(q) >= limit:
                retry = max(1, int(_WINDOW_SECONDS - (now - q[0])) + 1)
                return _ConsumeResult(False, retry)
            q.append(now)
            return _ConsumeResult(True)

    async def peek_rpm(self, key: str, limit: int) -> bool:
        async with self._locks[f"rpm:{key}"]:
            now = time.monotonic()
            self._purge_rpm(key, now)
            return len(self._rpm[key]) < limit

    async def consume_tpm(self, key: str, limit: int, tokens: int) -> _ConsumeResult:
        tokens = max(0, tokens)
        async with self._locks[f"tpm:{key}"]:
            now = time.monotonic()
            self._purge_tpm(key, now)
            q = self._tpm[key]
            used = sum(t for _, t in q)
            if used + tokens > limit:
                retry = 60
                if q:
                    retry = max(1, int(_WINDOW_SECONDS - (now - q[0][0])) + 1)
                return _ConsumeResult(False, retry)
            if tokens > 0:
                q.append((now, tokens))
            return _ConsumeResult(True)

    async def peek_tpm(self, key: str, limit: int, tokens: int) -> bool:
        tokens = max(0, tokens)
        async with self._locks[f"tpm:{key}"]:
            now = time.monotonic()
            self._purge_tpm(key, now)
            used = sum(t for _, t in self._tpm[key])
            return used + tokens <= limit

    def reset(self) -> None:
        """测试用：清空全部窗口。"""
        self._rpm.clear()
        self._tpm.clear()


class RateLimitService:
    """平台 API Key / 渠道 Key 限流。

    后端选择懒加载且失败后粘滞到 memory，避免每个请求都去 ping 挂掉的 Redis。
    测试用 reset_for_tests / force_memory_backend 隔离全局单例。
    """

    def __init__(self) -> None:
        self._memory = _InMemoryWindow()
        self._redis = None
        self._backend: str | None = None  # "redis" | "memory"
        self._init_lock = asyncio.Lock()
        self._fallback_warned = False

    def reset_for_tests(self) -> None:
        """重置进程内状态与后端选择（单测隔离）。"""
        self._memory.reset()
        self._redis = None
        self._backend = None
        self._fallback_warned = False

    def force_memory_backend(self) -> None:
        """测试强制使用进程内后端。"""
        self._backend = "memory"
        self._redis = None

    async def _ensure_backend(self) -> str:
        if self._backend is not None:
            return self._backend
        async with self._init_lock:
            if self._backend is not None:
                return self._backend
            redis_url = (settings.redis_url or "").strip()
            if not redis_url or "None" in redis_url or redis_url.endswith("@:/0"):
                self._warn_fallback("Redis URL not configured")
                self._backend = "memory"
                return self._backend
            try:
                import redis.asyncio as redis_async

                client = redis_async.from_url(
                    redis_url,
                    encoding="utf-8",
                    decode_responses=True,
                    socket_connect_timeout=0.5,
                    socket_timeout=0.5,
                )
                await client.ping()
                self._redis = client
                self._backend = "redis"
                logger.info("rate_limit_backend", backend="redis")
            except Exception as exc:  # noqa: BLE001 — 任意连接失败均降级
                self._warn_fallback(f"Redis unavailable: {exc}")
                self._backend = "memory"
            return self._backend

    def _warn_fallback(self, reason: str) -> None:
        if not self._fallback_warned:
            logger.warning(
                "rate_limit_fallback_to_memory",
                reason=reason,
                msg="Rate limiting degraded to in-process sliding window",
            )
            self._fallback_warned = True

    @staticmethod
    def _api_rpm_key(api_key_id: str) -> str:
        return f"rl:api:{api_key_id}:rpm"

    @staticmethod
    def _api_tpm_key(api_key_id: str) -> str:
        return f"rl:api:{api_key_id}:tpm"

    @staticmethod
    def _ch_rpm_key(channel_key_id: str) -> str:
        return f"rl:ch:{channel_key_id}:rpm"

    @staticmethod
    def _ch_tpm_key(channel_key_id: str) -> str:
        return f"rl:ch:{channel_key_id}:tpm"

    async def _consume_rpm(self, key: str, limit: int) -> _ConsumeResult:
        backend = await self._ensure_backend()
        if backend == "redis" and self._redis is not None:
            try:
                now = time.time()
                member = f"{now}:{uuid.uuid4().hex}"
                result = await self._redis.eval(
                    _LUA_RPM, 1, key, now, _WINDOW_SECONDS, limit, member
                )
                return _ConsumeResult(bool(int(result[0])), int(result[1]) or 60)
            except Exception as exc:  # noqa: BLE001
                self._warn_fallback(f"Redis RPM error: {exc}")
                self._backend = "memory"
        return await self._memory.consume_rpm(key, limit)

    async def _peek_rpm(self, key: str, limit: int) -> bool:
        backend = await self._ensure_backend()
        if backend == "redis" and self._redis is not None:
            try:
                now = time.time()
                result = await self._redis.eval(
                    _LUA_RPM_PEEK, 1, key, now, _WINDOW_SECONDS, limit
                )
                return bool(int(result))
            except Exception as exc:  # noqa: BLE001
                self._warn_fallback(f"Redis RPM peek error: {exc}")
                self._backend = "memory"
        return await self._memory.peek_rpm(key, limit)

    async def _consume_tpm(self, key: str, limit: int, tokens: int) -> _ConsumeResult:
        backend = await self._ensure_backend()
        if backend == "redis" and self._redis is not None:
            try:
                now = time.time()
                member = f"{uuid.uuid4().hex}"
                result = await self._redis.eval(
                    _LUA_TPM,
                    1,
                    key,
                    now,
                    _WINDOW_SECONDS,
                    limit,
                    max(0, tokens),
                    member,
                )
                return _ConsumeResult(bool(int(result[0])), int(result[1]) or 60)
            except Exception as exc:  # noqa: BLE001
                self._warn_fallback(f"Redis TPM error: {exc}")
                self._backend = "memory"
        return await self._memory.consume_tpm(key, limit, tokens)

    async def _peek_tpm(self, key: str, limit: int, tokens: int) -> bool:
        backend = await self._ensure_backend()
        if backend == "redis" and self._redis is not None:
            try:
                now = time.time()
                result = await self._redis.eval(
                    _LUA_TPM_PEEK,
                    1,
                    key,
                    now,
                    _WINDOW_SECONDS,
                    limit,
                    max(0, tokens),
                )
                return bool(int(result))
            except Exception as exc:  # noqa: BLE001
                self._warn_fallback(f"Redis TPM peek error: {exc}")
                self._backend = "memory"
        return await self._memory.peek_tpm(key, limit, tokens)

    async def enforce_api_key_limits(
        self,
        *,
        api_key_id: str,
        rpm_limit: int | None,
        tpm_limit: int | None,
        estimated_tokens: int,
    ) -> None:
        """检查并消费平台 API Key 限额；超限抛 RateLimitExceededError。

        rpm_limit/tpm_limit 为 None 或 <=0 视为不限制。
        RPM 先于 TPM：两个都超时用户看到的是 RPM 错误（先触发的那个）。
        """
        if rpm_limit is not None and rpm_limit > 0:
            result = await self._consume_rpm(self._api_rpm_key(api_key_id), rpm_limit)
            if not result.allowed:
                raise RateLimitExceededError(
                    f"RPM limit exceeded ({rpm_limit}/min)",
                    retry_after=result.retry_after,
                )
        if tpm_limit is not None and tpm_limit > 0:
            result = await self._consume_tpm(
                self._api_tpm_key(api_key_id), tpm_limit, estimated_tokens
            )
            if not result.allowed:
                raise RateLimitExceededError(
                    f"TPM limit exceeded ({tpm_limit}/min)",
                    retry_after=result.retry_after,
                )

    async def peek_channel_key_available(
        self,
        *,
        channel_key_id: str,
        rpm_limit: int | None,
        tpm_limit: int | None,
        estimated_tokens: int = 1,
    ) -> bool:
        """渠道 Key 是否仍有余量（不消费）。无限额视为可用。"""
        if rpm_limit is not None and rpm_limit > 0:
            if not await self._peek_rpm(self._ch_rpm_key(channel_key_id), rpm_limit):
                return False
        if tpm_limit is not None and tpm_limit > 0:
            if not await self._peek_tpm(
                self._ch_tpm_key(channel_key_id), tpm_limit, estimated_tokens
            ):
                return False
        return True

    async def try_consume_channel_key(
        self,
        *,
        channel_key_id: str,
        rpm_limit: int | None,
        tpm_limit: int | None,
        estimated_tokens: int = 1,
    ) -> bool:
        """尝试消费渠道 Key 限额；成功 True，打满 False（本轮跳过该 Key）。

        先 peek 再 consume，降低无谓写入。peek 与 consume 之间有 TOCTOU：
        并发下 TPM 失败时 RPM 可能已计入——可接受的轻微偏差，换 Key 重试即可。
        """
        # 先 peek，再分别消费；若 TPM 失败则 RPM 已计入——可接受的轻微偏差
        if not await self.peek_channel_key_available(
            channel_key_id=channel_key_id,
            rpm_limit=rpm_limit,
            tpm_limit=tpm_limit,
            estimated_tokens=estimated_tokens,
        ):
            return False
        if rpm_limit is not None and rpm_limit > 0:
            result = await self._consume_rpm(self._ch_rpm_key(channel_key_id), rpm_limit)
            if not result.allowed:
                return False
        if tpm_limit is not None and tpm_limit > 0:
            result = await self._consume_tpm(
                self._ch_tpm_key(channel_key_id), tpm_limit, estimated_tokens
            )
            if not result.allowed:
                return False
        return True


rate_limit_service = RateLimitService()
