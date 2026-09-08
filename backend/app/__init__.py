"""LLM TokenHub 后端应用包。

这是一个 OpenAI 兼容的 LLM 网关：对外暴露 ``/v1/chat/completions``、
``/v1/embeddings``、``/v1/models``，对内完成鉴权、限流、计费主体决策、
余额预扣、渠道路由、上游协议适配、实扣结算与用量落库。

目录职责（按请求主链路从外到内）：

- ``main.py``：FastAPI 装配（生命周期、中间件、路由、健康检查）。
- ``core/``：配置、错误码、安全原语、时间约定、指标、渠道 Key 加解密。
- ``api/``：HTTP 入口。``api/v1`` 是对外兼容接口；其余为用户端 / 管理端 REST。
- ``api/deps.py``：依赖注入（API Key / JWT → 上下文）。
- ``services/``：无 HTTP 的业务编排。网关核心是
  auth / permission / rate_limit / billing_subject / billing / wallet /
  routing / quota / usage。
- ``adapters/``：平台标准 Schema ↔ 上游厂商协议（OpenAI-compatible / Anthropic）。
- ``db/``：异步引擎、会话工厂、ORM 模型。
- ``middleware/``：request_id 透传与 HTTP 指标。
- ``kb/`` ``storage/`` ``payments/`` ``tasks/``：知识库、对象存储、支付渠道、后台任务。

金额约定：全程最小货币单位整数（见 ``settings.money_scale``），禁止 float。
时间约定：库内 UTC aware，对外 ISO8601 后缀 ``Z``。
事务约定：Service 不自行 commit；FastAPI ``get_db`` 在请求成功结束时提交。
"""
