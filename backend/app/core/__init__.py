"""跨模块基础设施：配置、错误、安全、时间、日志、指标、渠道 Key 密码学。

本包不含业务编排。业务异常类型集中在 ``errors.py``，由 ``register_exception_handlers``
挂到 FastAPI，保证对外始终是 ``{"error": {message, type, code}}``。
"""
