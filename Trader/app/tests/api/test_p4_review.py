"""P4-REVIEW regressions: job and worker `detail` JSON is masked like event data, and a signed-in user's
CSRF token never shows in a repr (a log line or a traceback)."""

from datetime import UTC, date, datetime

from trader.api.deps import AuthUser
from trader.api.routers.jobs import job_run_out
from trader.api.views import redacted_json
from trader.db import models as m
from trader.logging_setup import REDACTED

TOKEN = "csrf-token-value-not-real"


def test_the_auth_user_repr_hides_the_csrf_token() -> None:
    user = AuthUser(id=1, username="stephen", session_id=2, csrf_token=TOKEN)
    assert TOKEN not in repr(user)
    assert user.csrf_token == TOKEN


def test_a_job_run_detail_is_masked() -> None:
    row = m.JobRun(
        id=1,
        job="nightly",
        session_date=date(2026, 10, 6),
        started_at=datetime(2026, 10, 6, 1, 0, tzinfo=UTC),
        finished_at=None,
        status="succeeded",
        error=None,
        detail={"refresh_token": "abc", "nested": {"password": "hunter2"}, "count": 3},
    )
    out = job_run_out(row)
    assert out.detail == {"refresh_token": REDACTED, "nested": {"password": REDACTED}, "count": 3}


def test_redacted_json_keeps_non_secret_values() -> None:
    assert redacted_json({"rate_limit": {"market_remaining": 17}}) == {"rate_limit": {"market_remaining": 17}}
