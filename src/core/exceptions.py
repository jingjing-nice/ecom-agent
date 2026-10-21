"""统一业务异常与全局异常处理器。

响应约定（方案 §7）：普通响应包含 request_id、data、error_code、retryable。
"""
from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.encoders import jsonable_encoder
from loguru import logger
from starlette.exceptions import HTTPException as StarletteHTTPException


class AppError(Exception):
    """业务异常基类。

    error_code 为稳定枚举值，前端按码处理；
    retryable 表示调用方是否可以安全重试。
    """

    status_code: int = 400
    error_code: str = "bad_request"
    retryable: bool = False

    def __init__(self, message: str, *, details: Any = None):
        super().__init__(message)
        self.message = message
        self.details = details


class AuthError(AppError):
    status_code = 401
    error_code = "unauthenticated"


class PermissionDenied(AppError):
    status_code = 403
    error_code = "permission_denied"


class NotFound(AppError):
    status_code = 404
    error_code = "not_found"


class Conflict(AppError):
    """并发冲突 / 幂等键冲突 / 状态机非法迁移。"""

    status_code = 409
    error_code = "conflict"


class InvalidState(AppError):
    """状态机非法迁移。"""

    status_code = 409
    error_code = "invalid_state_transition"


class RateLimited(AppError):
    status_code = 429
    error_code = "rate_limited"
    retryable = True


class WorkflowError(AppError):
    """Agent 工作流失败（超预算、超时、模型失败等）。"""

    status_code = 502
    error_code = "workflow_failed"
    retryable = True


class UpstreamError(AppError):
    """外部系统调用失败（不确定状态时 retryable=False，转人工核对）。"""

    status_code = 502
    error_code = "upstream_error"
    retryable = True


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "")


def register_exception_handlers(app: FastAPI) -> None:
    """注册全局异常处理器，保持统一响应结构。"""

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError):
        logger.warning(
            f"[{_request_id(request)}] 业务异常 {exc.error_code}: {exc.message}"
        )
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "request_id": _request_id(request),
                "data": None,
                "error_code": exc.error_code,
                "message": exc.message,
                "details": exc.details,
                "retryable": exc.retryable,
            },
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(request: Request, exc: StarletteHTTPException):
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "request_id": _request_id(request),
                "data": None,
                "error_code": f"http_{exc.status_code}",
                "message": str(exc.detail),
                "details": None,
                "retryable": False,
            },
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content={
                "request_id": _request_id(request),
                "data": None,
                "error_code": "validation_error",
                "message": "请求参数校验失败",
                "details": jsonable_encoder(exc.errors()),
                "retryable": False,
            },
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception):
        logger.exception(f"[{_request_id(request)}] 未处理异常: {exc}")
        return JSONResponse(
            status_code=500,
            content={
                "request_id": _request_id(request),
                "data": None,
                "error_code": "internal_error",
                "message": "服务器内部错误",
                "details": None,
                "retryable": True,
            },
        )
