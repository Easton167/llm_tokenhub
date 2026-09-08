"""request_id 中间件：透传/生成 X-Request-ID，绑定结构化日志与基础 HTTP 指标。

职责：
- 每个请求有一个 request_id：优先用客户端 ``X-Request-ID``（截断 128），否则 UUID。
- 写入 ``request.state.request_id``，网关结算/日志与上游 ``X-Request-ID`` 头复用同一值。
- 把 request_id/method/path bind 进 structlog contextvars。
- 记录 HTTP 计数与延迟；path 经 ``normalize_path`` 折叠 ID，避免高基数。
- 成功时把 request_id 回写到响应头，便于客户端对账。
"""

from __future__ import annotations

import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.core.logging import bind_request_context, clear_request_context, get_logger
from app.core.metrics import HTTP_LATENCY, HTTP_REQUESTS, normalize_path

logger = get_logger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"


def resolve_request_id(request: Request) -> str:
    """优先使用客户端传入的 X-Request-ID，否则生成 UUID。"""
    incoming = request.headers.get(REQUEST_ID_HEADER)
    if incoming and incoming.strip():
        return incoming.strip()[:128]
    return str(uuid.uuid4())


class RequestIdMiddleware(BaseHTTPMiddleware):
    """最外层业务中间件（CORS 之外）。异常路径也要打点并清 context，防止协程串号。"""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        request_id = resolve_request_id(request)
        request.state.request_id = request_id
        bind_request_context(
            request_id=request_id,
            method=request.method,
            path=request.url.path,
        )

        start = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
        except Exception:
            # 未捕获异常：仍记一条 HTTP 指标，再交给 FastAPI 的 Exception handler
            elapsed = time.perf_counter() - start
            path_label = normalize_path(request.url.path)
            HTTP_REQUESTS.labels(
                method=request.method, path=path_label, status=str(status_code)
            ).inc()
            HTTP_LATENCY.labels(method=request.method, path=path_label).observe(elapsed)
            logger.exception(
                "request_failed",
                latency_ms=int(elapsed * 1000),
                status_code=status_code,
            )
            clear_request_context()
            raise
        else:
            elapsed = time.perf_counter() - start
            path_label = normalize_path(request.url.path)
            HTTP_REQUESTS.labels(
                method=request.method, path=path_label, status=str(status_code)
            ).inc()
            HTTP_LATENCY.labels(method=request.method, path=path_label).observe(elapsed)
            response.headers[REQUEST_ID_HEADER] = request_id
            logger.info(
                "request_completed",
                latency_ms=int(elapsed * 1000),
                status_code=status_code,
            )
            clear_request_context()
            return response
