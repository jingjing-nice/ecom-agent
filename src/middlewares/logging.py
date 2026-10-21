"""请求日志中间件：生成 request_id（trace_id）、记录耗时与状态码。"""
from __future__ import annotations

import time
import uuid
import re

from loguru import logger
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response


class LoggingMiddleware(BaseHTTPMiddleware):
    """为每个请求生成 request_id，并记录访问日志。"""

    async def dispatch(self, request: Request, call_next) -> Response:
        supplied = request.headers.get("X-Request-Id", "")
        request_id = supplied if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", supplied) else uuid.uuid4().hex[:16]
        request.state.request_id = request_id

        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            cost_ms = (time.perf_counter() - start) * 1000
            logger.exception(
                f"[{request_id}] {request.method} {request.url.path} 异常 ({cost_ms:.0f}ms)"
            )
            raise

        cost_ms = (time.perf_counter() - start) * 1000
        response.headers["X-Request-Id"] = request_id
        # 健康检查与静态资源不打印，避免噪音
        if not request.url.path.startswith(("/health", "/favicon")):
            logger.info(
                f"[{request_id}] {request.method} {request.url.path} "
                f"-> {response.status_code} ({cost_ms:.0f}ms)"
            )
        return response
