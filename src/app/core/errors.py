import logging
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.responses import ApiResponse

logger = logging.getLogger(__name__)


class PublicApiException(HTTPException):
    def __init__(
        self,
        status_code: int,
        message: str,
        reason: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(status_code=status_code, detail=message, headers=headers)
        self.reason = reason


def api_error(
    status_code: int, message: str, reason: str, headers: dict[str, str] | None = None
) -> PublicApiException:
    return PublicApiException(status_code, message, reason, headers)


def error_response(
    status_code: int,
    message: str,
    data: Any = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=ApiResponse(code=status_code, message=message, data=data).model_dump(),
        headers=headers,
    )


async def http_exception_handler(
    _: Request, exc: StarletteHTTPException
) -> JSONResponse:
    if isinstance(exc, PublicApiException):
        return error_response(
            exc.status_code,
            str(exc.detail),
            {"reason": exc.reason},
            exc.headers,
        )
    if isinstance(exc.detail, str):
        return error_response(exc.status_code, exc.detail, headers=exc.headers)
    return error_response(
        exc.status_code, HTTPStatus(exc.status_code).phrase, headers=exc.headers
    )


async def validation_exception_handler(
    _: Request, exc: RequestValidationError
) -> JSONResponse:
    errors = [
        {"location": error["loc"], "message": error["msg"], "type": error["type"]}
        for error in exc.errors()
    ]
    return error_response(
        422, "请求参数有误", {"reason": "validation_failed", "errors": errors}
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.error(
        "未处理异常 request_id=%s method=%s path=%s",
        getattr(request.state, "request_id", "-"),
        request.method,
        request.url.path,
    )
    return error_response(500, "Internal Server Error")


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
