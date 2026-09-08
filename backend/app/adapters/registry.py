"""适配器注册表：按渠道的协议类型（adapter_type）查找对应的 Adapter 实例。

网关不再硬绑定某个具体适配器：路由选中渠道后，用渠道携带的
``adapter_type`` 到本注册表查找 Adapter。新增协议时只需实现
``BaseAdapter`` 子类并调用 ``register_adapter`` 注册，无需修改网关代码。
"""

from __future__ import annotations

from typing import Callable

from app.adapters.anthropic import AnthropicAdapter
from app.adapters.base import BaseAdapter
from app.adapters.openai_compatible import openai_compatible_adapter
from app.core.errors import AppError

# 内置协议类型常量
ADAPTER_TYPE_OPENAI_COMPATIBLE = "openai_compatible"
ADAPTER_TYPE_ANTHROPIC = "anthropic"


class AdapterNotConfiguredError(AppError):
    """渠道配置了未注册的协议类型时抛出。

    属于平台侧配置错误（渠道/Provider 的 adapter_type 填错或适配器未注册），
    返回 503 + 明确错误码，而非笼统的 500。
    """

    def __init__(self, adapter_type: str) -> None:
        super().__init__(
            f"Adapter type '{adapter_type}' is not registered. "
            "Check the provider/channel adapter_type configuration.",
            code="adapter_not_configured",
            error_type="configuration_error",
            status_code=503,
        )


_REGISTRY: dict[str, BaseAdapter] = {}
_FACTORY_REGISTRY: dict[str, Callable[[], BaseAdapter]] = {}


def register_adapter(adapter_type: str, adapter: BaseAdapter) -> None:
    """注册（或覆盖）某协议类型对应的 Adapter 单例。

    适用于完全无状态的适配器（如 openai_compatible），所有请求共享同一实例。
    """
    _REGISTRY[adapter_type] = adapter


def register_adapter_factory(
    adapter_type: str, factory: Callable[[], BaseAdapter]
) -> None:
    """注册（或覆盖）某协议类型的 Adapter 工厂，``get_adapter`` 每次调用返回新实例。

    适用于流式解析有状态的适配器（如 anthropic：message_start 的输入侧 usage
    要暂存到最终 message_delta 才能合成完整 Usage），避免并发请求共享状态。
    """
    _FACTORY_REGISTRY[adapter_type] = factory


def unregister_adapter(adapter_type: str) -> None:
    """移除某协议类型的注册（主要供测试清理使用）。"""
    _REGISTRY.pop(adapter_type, None)
    _FACTORY_REGISTRY.pop(adapter_type, None)


def get_adapter(adapter_type: str) -> BaseAdapter:
    """按协议类型查找 Adapter（单例注册返回共享实例，工厂注册返回新实例）。

    查找顺序：先单例表，再工厂表。同一 adapter_type 不要同时注册两种。

    Raises:
        AdapterNotConfiguredError: 协议类型未注册时抛出（配置错误，503）。
    """
    adapter = _REGISTRY.get(adapter_type)
    if adapter is not None:
        return adapter
    factory = _FACTORY_REGISTRY.get(adapter_type)
    if factory is not None:
        return factory()
    raise AdapterNotConfiguredError(adapter_type)


# 内置适配器注册
register_adapter(ADAPTER_TYPE_OPENAI_COMPATIBLE, openai_compatible_adapter)
register_adapter_factory(ADAPTER_TYPE_ANTHROPIC, AnthropicAdapter)
