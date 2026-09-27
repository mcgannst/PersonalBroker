"""POST /api/auth/login (public), POST /api/auth/logout, GET /api/auth/me, PUT /api/auth/password,
POST /api/auth/totp/setup, POST /api/auth/totp/confirm, POST /api/auth/totp/disable.

The router is registered under `/api` by `trader.api.routers.ROUTERS`. Every route is a plain `def` (database
work runs in FastAPI's thread pool). Login is the only public route: it checks the per-IP rate limit before it
touches the database, and any `Origin` header. Every other change needs the session and the CSRF header.
"""

import math

from fastapi import APIRouter, Request, Response

from trader.api import auth
from trader.api.deps import CsrfUser, CurrentUser, Services
from trader.api.errors import ApiError
from trader.api.schemas import (
    LoginIn,
    OkOut,
    PasswordChangeIn,
    SessionOut,
    TotpConfirmIn,
    TotpDisableIn,
    TotpSetupIn,
    TotpSetupOut,
)

router = APIRouter(tags=["auth"])


@router.post("/auth/login")
def post_login(body: LoginIn, request: Request, response: Response, services: Services) -> SessionOut:
    auth.check_origin(request)
    ip = auth.client_ip(request)
    wait = auth.login_limiter(request, services).allow(ip or "unknown")
    if wait is not None:
        raise ApiError(
            429,
            "too_many_requests",
            "Too many login attempts. Try again in a minute.",
            headers={"Retry-After": str(max(math.ceil(wait), 1))},
        )
    session, cookie = auth.login(
        services, body.username, body.password, body.totp, ip, request.headers.get("user-agent")
    )
    response.set_cookie(
        auth.COOKIE_NAME,
        cookie,
        max_age=auth.cookie_max_age(services),
        path="/",
        secure=True,
        httponly=True,
        samesite="strict",
    )
    return session


@router.post("/auth/logout")
def post_logout(user: CsrfUser, response: Response, services: Services) -> OkOut:
    auth.logout(services, user)
    response.headers["Set-Cookie"] = auth.CLEAR_COOKIE
    return OkOut()


@router.get("/auth/me")
def get_me(user: CurrentUser, services: Services) -> SessionOut:
    return auth.session_info(services, user)


@router.put("/auth/password")
def put_password(body: PasswordChangeIn, user: CsrfUser, services: Services) -> OkOut:
    auth.change_password(services, user, body.current_password, body.new_password, body.totp)
    return OkOut(message="Password changed. Other sessions were signed out.")


@router.post("/auth/totp/setup")
def post_totp_setup(body: TotpSetupIn, user: CsrfUser, services: Services) -> TotpSetupOut:
    return auth.totp_setup(services, user, body.password)


@router.post("/auth/totp/confirm")
def post_totp_confirm(body: TotpConfirmIn, user: CsrfUser, services: Services) -> OkOut:
    auth.totp_confirm(services, user, body.code)
    return OkOut(message="Two-step codes are on")


@router.post("/auth/totp/disable")
def post_totp_disable(body: TotpDisableIn, user: CsrfUser, services: Services) -> OkOut:
    auth.totp_disable(services, user, body.password, body.code)
    return OkOut(message="Two-step codes are off")
