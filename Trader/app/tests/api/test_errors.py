"""P4-T1 acceptance test 7: `install_error_handlers` renders every error as `ErrorOut` and never echoes input
or a secret (SPEC §14)."""

import logging
from collections.abc import Awaitable, Callable

import pytest
import structlog
from fastapi import FastAPI, Request, Response
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException

from trader.api.errors import ApiError, install_error_handlers


class _Body(BaseModel):
    username: str = Field(min_length=1, max_length=50)
    password: str = Field(min_length=20, max_length=200)


def _app(*, request_id: str | None = None) -> FastAPI:
    app = FastAPI()
    install_error_handlers(app)

    if request_id is not None:

        @app.middleware("http")
        async def _rid(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
            request.state.request_id = request_id
            return await call_next(request)

    @app.get("/conflict")
    def conflict() -> None:
        raise ApiError(409, "conflict", "x")

    @app.get("/locked")
    def locked() -> None:
        raise ApiError(429, "too_many_requests", "Try again later", headers={"Retry-After": "60"})

    @app.get("/default")
    def default() -> None:
        raise ApiError(401)

    @app.get("/http/{status}")
    def http(status: int) -> None:
        raise HTTPException(status, headers={"Retry-After": "7"} if status == 429 else None)

    @app.post("/login")
    def login(body: _Body) -> dict[str, str]:
        return {"ok": body.username}

    @app.get("/boom")
    def boom() -> None:
        raise RuntimeError("token=abc")

    return app


def _client(**kw: str | None) -> TestClient:
    return TestClient(_app(**kw), base_url="https://testserver", raise_server_exceptions=False)


def test_api_error_renders_the_error_shape() -> None:
    resp = _client().get("/conflict")
    assert resp.status_code == 409
    assert resp.json() == {"error": {"code": "conflict", "message": "x", "fields": None, "request_id": None}}


def test_api_error_keeps_its_headers() -> None:
    resp = _client().get("/locked")
    assert resp.status_code == 429 and resp.headers["retry-after"] == "60"
    assert resp.json()["error"]["code"] == "too_many_requests"


def test_api_error_defaults_code_and_message_from_the_status() -> None:
    resp = _client().get("/default")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "unauthorized"
    assert resp.json()["error"]["message"]


def test_request_id_is_in_the_body_when_the_request_has_one() -> None:
    resp = _client(request_id="r-1234").get("/conflict")
    assert resp.json()["error"]["request_id"] == "r-1234"


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, "unauthorized"),
        (403, "forbidden"),
        (404, "not_found"),
        (409, "conflict"),
        (429, "too_many_requests"),
        (418, "http_error"),
    ],
)
def test_http_exceptions_get_a_code_from_their_status(status: int, code: str) -> None:
    resp = _client().get(f"/http/{status}")
    assert resp.status_code == status
    assert resp.json()["error"]["code"] == code
    if status == 429:
        assert resp.headers["retry-after"] == "7"


def test_unknown_route_and_wrong_method() -> None:
    client = _client()
    assert client.get("/nope").json()["error"]["code"] == "not_found"
    resp = client.post("/conflict")
    assert resp.status_code == 405 and resp.json()["error"]["code"] == "method_not_allowed"


def test_validation_error_never_echoes_the_input() -> None:
    resp = _client().post("/login", json={"username": "", "password": "hunter2hunter2"})
    assert resp.status_code == 422
    assert "hunter2" not in resp.text
    body = resp.json()["error"]
    assert body["code"] == "validation"
    assert {tuple(f["loc"]) for f in body["fields"]} == {("body", "username"), ("body", "password")}
    for f in body["fields"]:
        assert set(f) == {"loc", "msg"}  # never `input` or `ctx`


def test_malformed_json_is_a_422_without_the_body() -> None:
    resp = _client().post(
        "/login", content=b'{"password": "hunter2hunter2"', headers={"content-type": "application/json"}
    )
    assert resp.status_code == 422
    assert "hunter2" not in resp.text


def test_unhandled_exception_is_a_500_with_no_secret_in_body_or_log(
    caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    caplog.set_level(logging.DEBUG)
    with structlog.testing.capture_logs() as logs:
        resp = _client(request_id="r-9").get("/boom")
    assert resp.status_code == 500
    assert resp.json() == {
        "error": {"code": "internal", "message": "Internal error", "fields": None, "request_id": "r-9"}
    }
    assert "abc" not in resp.text
    assert any(e.get("error_type") == "RuntimeError" for e in logs)
    captured = capsys.readouterr()
    assert "abc" not in repr(logs) + caplog.text + captured.out + captured.err
