"""P4-T6 acceptance test 10: a closed proposal's Telegram message shows the decision time (MT) when the view
carries `decided_at`; without it the P3 wording is unchanged."""

import dataclasses
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from trader.notify.messages import TELEGRAM_LIMIT, MessageRenderer
from trader.notify.types import ProposalView

MT = ZoneInfo("America/Edmonton")
T0 = datetime(2026, 10, 6, 13, 35, 5, tzinfo=UTC)  # 07:35 MT
DECIDED = datetime(2026, 10, 6, 13, 36, 10, tzinfo=UTC)  # 07:36 MT


@pytest.fixture
def r() -> MessageRenderer:
    return MessageRenderer("http://trader.home:8080", MT)


def view(**kw: object) -> ProposalView:
    base = ProposalView(
        proposal_id=42,
        kind="entry",
        status="pending",
        ticker="AAA",
        side="buy",
        order_type="stop",
        qty=33,
        stop=Decimal("21.56"),
        limit=None,
        stop_loss=Decimal("21.41"),
        risk_usd=Decimal("4.95"),
        reason="ORB breakout above 21.55",
        strategy_key="orb_sip",
        created_at=T0,
        expires_at=T0 + timedelta(minutes=5),
        decided_via=None,
        error=None,
    )
    return dataclasses.replace(base, **kw)  # type: ignore[arg-type]


def last(text: str) -> str:
    return text.splitlines()[-1]


def test_approved_via_web_shows_the_decision_time(r: MessageRenderer) -> None:
    v = view(status="submitted", decided_via="web", decided_at=DECIDED)
    assert last(r.proposal_closed(v, "submitted", "web")) == "<b>Approved via web at 07:36 MT</b>"


def test_rejected_shows_the_decision_time(r: MessageRenderer) -> None:
    v = view(status="rejected", decided_via="telegram", decided_at=DECIDED)
    assert last(r.proposal_closed(v, "rejected", "telegram")) == "<b>Rejected at 07:36 MT</b>"


def test_blocked_entry_keeps_its_reason_then_the_time(r: MessageRenderer) -> None:
    error = "entry blocked: kill switch manual_pause is tripped"
    v = view(status="rejected", decided_via="web", decided_at=DECIDED, error=error)
    assert last(r.proposal_closed(v, "rejected", "web")) == f"<b>Rejected: {error} at 07:36 MT</b>"


def test_automatic_approval_is_nobodys_decision_and_keeps_its_wording(r: MessageRenderer) -> None:
    """Auto mode, or an exit auto-submitted on expiry: no human decided, so no decision time (the P3
    integration test pins `Approved (auto)` as the last line)."""
    v = view(status="auto_approved", decided_via="auto", decided_at=DECIDED)
    assert last(r.proposal_closed(v, "auto_approved", None)) == "<b>Approved (auto)</b>"
    flattened = view(kind="exit", status="submitted", decided_via="auto", decided_at=DECIDED)
    assert last(r.proposal_closed(flattened, "submitted", "auto")) == "<b>Approved (auto)</b>"


def test_the_time_follows_the_mountain_offset_in_winter(r: MessageRenderer) -> None:
    decided = datetime(2026, 12, 1, 15, 0, tzinfo=UTC)  # UTC-6 all year from 2026c
    v = view(status="submitted", decided_via="telegram", decided_at=decided)
    assert last(r.proposal_closed(v, "submitted", "telegram")) == "<b>Approved via telegram at 09:00 MT</b>"


@pytest.mark.parametrize(
    ("final", "via", "error", "line"),
    [
        ("approved", "telegram", None, "Approved via telegram"),
        ("submitted", "web", None, "Approved via web"),
        ("rejected", "telegram", None, "Rejected"),
        ("rejected", "web", "entry blocked: x", "Rejected: entry blocked: x"),
        ("expired", None, None, "Expired"),
        ("failed", "telegram", "insufficient buying power", "Failed: insufficient buying power"),
    ],
)
def test_without_decided_at_the_p3_wording_is_unchanged(
    r: MessageRenderer, final: str, via: str | None, error: str | None, line: str
) -> None:
    v = view(status=final, error=error)
    assert v.decided_at is None
    assert last(r.proposal_closed(v, final, via)) == f"<b>{line}</b>"


@pytest.mark.parametrize(("final", "line"), [("expired", "Expired"), ("failed", "Failed: refused")])
def test_expired_and_failed_never_show_a_decision_time(r: MessageRenderer, final: str, line: str) -> None:
    v = view(status=final, decided_at=DECIDED, error="refused" if final == "failed" else None)
    assert last(r.proposal_closed(v, final, "web")) == f"<b>{line}</b>"


def test_a_huge_error_with_a_time_still_fits(r: MessageRenderer) -> None:
    v = view(status="rejected", decided_at=DECIDED, error="refused & <why> " * 2000)
    text = r.proposal_closed(v, "rejected", "web")
    assert len(text) <= TELEGRAM_LIMIT
    assert last(text).endswith(" at 07:36 MT</b>")
