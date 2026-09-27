"""POST /api/credentials/questrade -> TokenOut: paste a new Questrade refresh token (BR-55, R4 mitigation;
SPEC §4.1 initial setup, §11, §14 audit of credential changes).

The token (stripped) reseeds the chain (`services.credentials.seed`) and is exchanged at once
(`access()`), so a bad token is reported straight away: a `QuestradeAuthError` is a 422 with its message
(written to be shown to Stephen). Every attempt writes an `audit_log` row `credentials.questrade.seed`
(after: `{"ok": bool}`) and an `info` or `error` event (source `questrade.token`). The token never appears in
a response, a log line, an event or an audit row: any text shown is masked, and the token itself is cut
out of it should an error ever quote it. A plain `def` route: the seed and the exchange run in the thread
pool, off the event loop.

Registered under `/api` by `trader.api.routers.ROUTERS`.
"""

import structlog
from fastapi import APIRouter

from trader.adapters.questrade.auth import QuestradeAuthError
from trader.api import views
from trader.api.deps import CsrfUser, Services, actor
from trader.api.errors import ApiError
from trader.api.schemas import CredentialIn, TokenOut
from trader.db import models as m
from trader.db.session import session_scope
from trader.events import log_event
from trader.logging_setup import REDACTED, redact_text

log = structlog.get_logger("api.credentials")

router = APIRouter(tags=["credentials"])

AUDIT_ACTION = "credentials.questrade.seed"
EVENT_SOURCE = "questrade.token"


def _safe(message: str, token: str) -> str:
    """`message` masked by pattern, and with the token itself cut out."""
    return redact_text(message.replace(token, REDACTED))


def _record(services: Services, who: str, ok: bool, message: str) -> None:
    core = services.core
    with session_scope(core.factory) as s:
        s.add(m.AuditLog(ts=core.clock.now(), actor=who, action=AUDIT_ACTION, before=None, after={"ok": ok}))
        log_event(s, core.clock, "info" if ok else "error", EVENT_SOURCE, message, {"ok": ok})


@router.post("/credentials/questrade")
def paste_questrade_token(body: CredentialIn, user: CsrfUser, services: Services) -> TokenOut:
    token = body.refresh_token.get_secret_value().strip()
    if not token:
        msg = "The refresh token is empty. Paste a manual token from Questrade."
        raise ApiError(422, "validation", msg, [{"loc": ["body", "refresh_token"], "msg": "empty"}])
    store = services.credentials
    who = actor(user)
    try:
        store.seed(token)
        store.access()
    except QuestradeAuthError as exc:
        shown = _safe(str(exc), token) or "Questrade refused the token"
        log.warning("api.questrade_token_rejected", actor=who)
        _record(services, who, False, f"Questrade token pasted on the web was not accepted: {shown}")
        raise ApiError(422, "token_rejected", shown) from None
    except Exception as exc:
        kind = type(exc).__name__
        log.error("api.questrade_token_failed", actor=who, error_type=kind)
        _record(services, who, False, f"Questrade token pasted on the web could not be checked ({kind})")
        raise ApiError(
            502, "upstream", f"Questrade could not be reached to check the token ({kind})"
        ) from None
    log.info("api.questrade_token_seeded", actor=who)
    _record(services, who, True, "Questrade token pasted on the web and accepted")
    return views.token_out(store.health, services.core.clock.now())
