"""API test helpers (P4-T1). `make_client(services, *routers, user=...)` builds a bare FastAPI app with the
error handlers, the given routers under `/api` and `app.state.services`, with the auth dependencies
overridden: `current_user` returns `user` (401 when it is None) and `require_csrf` returns the same user
(the real CSRF check is T4's and is swept by T18). Import with `from tests.api.conftest import make_client`.
"""

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from trader.api.deps import ApiServices, AuthUser, current_user, require_csrf
from trader.api.errors import ApiError, install_error_handlers

BASE_URL = "https://testserver"  # the session cookie is Secure
DEFAULT_USER = AuthUser(id=1, username="stephen", session_id=1, csrf_token="test-csrf-token")


def make_client(
    services: ApiServices,
    *routers: APIRouter,
    user: AuthUser | None = DEFAULT_USER,
    raise_server_exceptions: bool = True,
) -> TestClient:
    """A TestClient on `https://testserver`. Pass `raise_server_exceptions=False` to see a 500 response
    instead of the exception."""
    app = FastAPI()
    install_error_handlers(app)
    for router in routers:
        app.include_router(router, prefix="/api")
    app.state.services = services

    def signed_in() -> AuthUser:
        if user is None:
            raise ApiError(401, "unauthorized", "Please log in")
        return user

    app.dependency_overrides[current_user] = signed_in
    app.dependency_overrides[require_csrf] = signed_in
    return TestClient(app, base_url=BASE_URL, raise_server_exceptions=raise_server_exceptions)
