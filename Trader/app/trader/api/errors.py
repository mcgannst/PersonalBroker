"""The API's one error shape (`ErrorOut`) and the handlers that render every failure with it (SPEC §11, §14).

- `ApiError`: raised by routes and dependencies for an expected failure (status, code, message).
- Starlette `HTTPException` (unknown route, wrong method, ...): the code is derived from the status.
- `RequestValidationError`: 422 `validation` with `fields = [{loc, msg}]` only. Never `input` or `ctx`, so a
  password or token in a rejected body is never echoed.
- Anything else: 500 `internal`, "Internal error". The log line has the exception type, a redacted message
  and where it was raised; never the traceback's locals or the request body.

Every error body carries `request_id` when the request has one (`request.state.request_id`, set by the
request-log middleware of T3).
"""

import traceback
from collections.abc import Mapping
from http import HTTPStatus
from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from trader.api.schemas import ErrorBody, ErrorOut, FieldError
from trader.logging_setup import redact_text

log = structlog.get_logger("api.errors")

# HTTP status -> error code. Any other status is "http_error".
ERROR_CODES: Mapping[int, str] = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "too_large",
    422: "validation",
    429: "too_many_requests",
    500: "internal",
    503: "unavailable",
}
HTTP_EXCEPTION_CODES: Mapping[int, str] = {
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    429: "too_many_requests",
}


def _phrase(status: int) -> str:
    try:
        return HTTPStatus(status).phrase
    except ValueError:
        return "Error"


class ApiError(Exception):
    """An expected API failure, rendered as `ErrorOut`. `code` and `message` default from the status;
    `headers` are sent with the response (e.g. `Retry-After`, or a `Set-Cookie` that clears the session)."""

    def __init__(
        self,
        status: int,
        code: str | None = None,
        message: str | None = None,
        fields: list[dict[str, Any]] | None = None,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.status = status
        self.code = code or ERROR_CODES.get(status, "http_error")
        self.message = message or _phrase(status)
        self.fields = fields
        self.headers = dict(headers) if headers else None
        super().__init__(f"{self.status} {self.code}: {self.message}")


def _request_id(request: Request) -> str | None:
    rid = getattr(request.state, "request_id", None)
    return rid if isinstance(rid, str) else None


def error_response(
    request: Request,
    status: int,
    code: str,
    message: str,
    fields: list[dict[str, Any]] | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    body = ErrorOut(
        error=ErrorBody(
            code=code,
            message=message,
            fields=[FieldError.model_validate(f) for f in fields] if fields is not None else None,
            request_id=_request_id(request),
        )
    )
    return JSONResponse(body.model_dump(mode="json"), status_code=status, headers=dict(headers or {}))


async def _api_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ApiError)
    return error_response(request, exc.status, exc.code, exc.message, exc.fields, exc.headers)


async def _http_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    code = HTTP_EXCEPTION_CODES.get(exc.status_code, "http_error")
    detail = exc.detail if isinstance(exc.detail, str) and exc.detail else _phrase(exc.status_code)
    return error_response(request, exc.status_code, code, detail, headers=exc.headers)


async def _validation_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    fields = [
        {
            "loc": [p if isinstance(p, int) else str(p) for p in err.get("loc", ())],
            "msg": str(err.get("msg", "")),
        }
        for err in exc.errors()
    ]
    return error_response(request, 422, "validation", "Invalid request", fields)


async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
    frames = traceback.extract_tb(exc.__traceback__)
    where = f"{frames[-1].filename}:{frames[-1].lineno} in {frames[-1].name}" if frames else None
    log.error(
        "api.unhandled",
        error_type=type(exc).__name__,
        error=redact_text(str(exc))[:500],
        where=where,
        method=request.method,
        path=request.url.path,
        request_id=_request_id(request),
    )
    return error_response(request, 500, "internal", "Internal error")


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ApiError, _api_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(Exception, _unhandled)
