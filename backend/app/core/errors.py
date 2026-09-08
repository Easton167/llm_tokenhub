"""统一错误响应格式与业务异常层次，对外 API 始终返回结构化 JSON。

约定：
- 可预期错误全部继承 ``AppError``，带 HTTP 状态码 + 机器可读 ``code`` + OpenAI 风格 ``error_type``。
- 网关/适配器 ``normalize_error`` 只构造异常对象、不 raise，由调用方决定重试还是返回。
- 未捕获异常在 ``register_exception_handlers`` 里吞掉堆栈，对外固定 500 internal_error。
- 响应体形状对齐 OpenAI：``{"error": {"message", "type", "code"}}``。
"""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel


class ErrorDetail(BaseModel):
    """单条错误明细。type 给客户端分类，code 给程序分支。"""

    message: str
    type: str
    code: str


class ErrorResponse(BaseModel):
    """对外唯一错误信封。不要在此之外再包一层 data/success。"""

    error: ErrorDetail


class AppError(Exception):
    """可预期的业务异常基类，携带 HTTP 状态码与机器可读 error code。"""

    def __init__(
        self,
        message: str,
        *,
        code: str = "internal_error",
        error_type: str = "internal_error",
        status_code: int = 500,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.message = message
        self.code = code
        self.error_type = error_type
        self.status_code = status_code
        self.headers = headers or {}
        super().__init__(message)


class InvalidRequestError(AppError):
    """参数不合法、协议无法映射、缺少计费规则等 400。"""

    def __init__(self, message: str = "Invalid request") -> None:
        super().__init__(
            message,
            code="invalid_request",
            error_type="invalid_request_error",
            status_code=400,
        )


class InvalidApiKeyError(AppError):
    """平台 API Key 缺失/格式错/哈希未命中/已禁用/已过期；也用于上游 401 归一化。"""

    def __init__(self, message: str = "Invalid API key") -> None:
        super().__init__(
            message,
            code="invalid_api_key",
            error_type="invalid_request_error",
            status_code=401,
        )


class UnauthorizedError(AppError):
    """JWT 缺失/无效或登录凭证错误。"""

    def __init__(self, message: str = "Unauthorized") -> None:
        super().__init__(
            message,
            code="unauthorized",
            error_type="authentication_error",
            status_code=401,
        )


class ForbiddenError(AppError):
    """已认证但无权：扣别人钱包、非组织成员、管理端越权等。"""

    def __init__(self, message: str = "Forbidden") -> None:
        super().__init__(
            message,
            code="forbidden",
            error_type="permission_error",
            status_code=403,
        )


class InsufficientBalanceError(AppError):
    """402：钱包可用额不足、成员额度耗尽且组织策略为 deny、上游余额错误归一化。"""

    def __init__(self, message: str = "Insufficient balance") -> None:
        super().__init__(
            message,
            code="insufficient_balance",
            error_type="billing_error",
            status_code=402,
        )


class ModelNotAuthorizedError(AppError):
    """403：角色未授权该模型，或 API Key.allowed_models 未包含该 alias。"""

    def __init__(self, message: str = "You do not have access to this model") -> None:
        super().__init__(
            message,
            code="model_not_authorized",
            error_type="permission_error",
            status_code=403,
        )


class ModelNotFoundError(AppError):
    """404：平台 alias 不存在/已禁用，或上游返回模型不存在。"""

    def __init__(self, message: str = "Model not found") -> None:
        super().__init__(
            message,
            code="model_not_found",
            error_type="invalid_request_error",
            status_code=404,
        )


class ResourceNotFoundError(AppError):
    """通用 404：钱包、组织成员、知识库文档等业务资源。"""

    def __init__(self, message: str = "Resource not found") -> None:
        super().__init__(
            message,
            code="not_found",
            error_type="invalid_request_error",
            status_code=404,
        )


class ConflictError(AppError):
    """409：唯一约束冲突、状态机非法跳转（支付单重复入账等）。"""

    def __init__(
        self,
        message: str = "Resource conflict",
        *,
        code: str = "conflict",
    ) -> None:
        super().__init__(
            message,
            code=code,
            error_type="conflict_error",
            status_code=409,
        )


class GiftAmountExceedsLimitError(AppError):
    """赠送金额超过单笔或操作人日累计上限。"""

    def __init__(self, message: str = "Gift amount exceeds limit") -> None:
        super().__init__(
            message,
            code="gift_amount_exceeds_limit",
            error_type="invalid_request_error",
            status_code=422,
        )


class RateLimitExceededError(AppError):
    """Key 级 RPM/TPM 超限；带 Retry-After，不预扣、不调上游。"""

    def __init__(
        self,
        message: str = "Rate limit exceeded",
        *,
        retry_after: int = 60,
    ) -> None:
        super().__init__(
            message,
            code="rate_limit_exceeded",
            error_type="rate_limit_error",
            status_code=429,
            headers={"Retry-After": str(max(1, retry_after))},
        )
        self.retry_after = retry_after


class UnsupportedFileTypeError(AppError):
    """知识库上传：扩展名与真实类型不匹配，或不在支持列表。"""

    def __init__(self, message: str = "Unsupported file type") -> None:
        super().__init__(
            message,
            code="unsupported_file_type",
            error_type="invalid_request_error",
            status_code=400,
        )


class QuotaExceededError(AppError):
    """知识库配额超限（库数 / 文档数 / 存储量 / 单文件 / 批量）。"""

    def __init__(
        self,
        message: str,
        *,
        code: str = "quota_exceeded",
    ) -> None:
        super().__init__(
            message,
            code=code,
            error_type="quota_error",
            status_code=400,
        )


class KbDisabledError(AppError):
    """知识库已被管理员禁用：不可上传、不可检索。"""

    def __init__(
        self,
        message: str = "Knowledge base is disabled",
    ) -> None:
        super().__init__(
            message,
            code="kb_disabled",
            error_type="invalid_request_error",
            status_code=400,
        )


def error_response(exc: AppError) -> JSONResponse:
    """把 AppError 转成 JSONResponse；RateLimitExceededError 会带 Retry-After。"""
    body = ErrorResponse(
        error=ErrorDetail(
            message=exc.message,
            type=exc.error_type,
            code=exc.code,
        )
    )
    return JSONResponse(
        status_code=exc.status_code,
        content=body.model_dump(),
        headers=exc.headers or None,
    )


def register_exception_handlers(app: FastAPI) -> None:
    """注册全局异常处理。必须在路由之前调用，否则部分路径可能漏掉。"""

    @app.exception_handler(AppError)
    async def handle_app_error(_request: Request, exc: AppError) -> JSONResponse:
        return error_response(exc)

    @app.exception_handler(Exception)
    async def handle_unexpected_error(_request: Request, _exc: Exception) -> JSONResponse:
        # 未捕获异常统一映射为 500，避免向客户端泄露内部堆栈
        return error_response(
            AppError(
                "An unexpected error occurred",
                code="internal_error",
                error_type="internal_error",
                status_code=500,
            )
        )
