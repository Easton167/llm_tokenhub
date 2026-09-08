"""上游协议适配器：平台 OpenAI Schema ↔ 厂商私有/兼容协议。

网关（``api/v1/chat.py``）只依赖 ``BaseAdapter`` + ``get_adapter(adapter_type)``。
新增厂商：实现子类，在 ``registry`` 里 ``register_adapter`` 或 ``register_adapter_factory``。
流式解析若有跨 chunk 状态（Anthropic usage），必须用工厂，禁止单例。
"""
