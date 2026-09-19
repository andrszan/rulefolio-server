import logging
import re
import time
from collections.abc import Awaitable, Callable
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response

from app.core.database import SessionLocal
from app.core.errors import error_response, unhandled_exception_handler
from app.recovery.gate import api_requests_allowed

logger = logging.getLogger(__name__)
REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,128}\Z")


async def unexpected_exception_boundary(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    try:
        return await call_next(request)
    except Exception as exc:
        return await unhandled_exception_handler(request, exc)


async def recovery_gate(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    if request.url.path in {"/health", "/ready"}:
        return await call_next(request)

    with SessionLocal() as session:
        if not api_requests_allowed(session):
            return error_response(
                503,
                "服务正在恢复，请稍后重试",
                {"reason": "recovery_in_progress"},
            )
    return await call_next(request)


async def request_id_and_access_log(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    request_id = request.headers.get("X-Request-ID", "")
    if not REQUEST_ID_PATTERN.fullmatch(request_id):
        request_id = uuid4().hex
    request.state.request_id = request_id

    started_at = time.perf_counter()
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    logger.info(
        "请求完成 request_id=%s method=%s path=%s status=%s duration_ms=%.2f",
        request_id,
        request.method,
        request.url.path,
        response.status_code,
        (time.perf_counter() - started_at) * 1000,
    )
    return response


def register_middlewares(app: FastAPI, cors_origins: list[str]) -> None:
    app.middleware("http")(recovery_gate)
    app.middleware("http")(unexpected_exception_boundary)
    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_credentials=False,
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=["X-Request-ID"],
        )
    # Starlette 后注册的 middleware 位于外层。
    app.middleware("http")(request_id_and_access_log)
