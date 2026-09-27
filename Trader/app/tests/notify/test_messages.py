"""P3-T4 acceptance tests: every Telegram message format (SPEC §4.4) as pure functions of the views."""

import dataclasses
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from trader.market.clock import FixedClock
from trader.notify.messages import (
    TELEGRAM_LIMIT,
    MessageRenderer,
    _fit,
    fmt_duration,
    fmt_money,
    fmt_pct,
    fmt_price,
    fmt_time,
    link,
)
from trader.notify.types import (
    AlertView,
    Button,
    Check,
    DailySummaryView,
    FillView,
    OutboundMessage,
    OverlayView,
    PnlView,
    PositionLine,
    PreopenView,
    ProposalView,
    Renderer,
    StatusView,
    TradeLine,
)

MT = ZoneInfo("America/Edmonton")
BASE = "http://trader.home:8080"
T0 = datetime(2026, 10, 6, 13, 35, 5, tzinfo=UTC)  # 09:35:05 ET, 07:35 MT
APPROVE = Button("✅ Approve", "p:42:a:n1:mac")
REJECT = Button("❌ Reject", "p:42:r:n1:mac")
YES = Button("Yes", "j:20261006:y:n2:mac")
NO = Button("No", "j:20261006:n:n2:mac")


@pytest.fixture
def r() -> MessageRenderer:
    return MessageRenderer(BASE, MT)


def proposal(**kw: Any) -> ProposalView:
    base: dict[str, Any] = dict(
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
    base.update(kw)
    return ProposalView(**base)


def fill(**kw: Any) -> FillView:
    base: dict[str, Any] = dict(
        fill_id=7,
        ticker="AAA",
        side="buy",
        purpose="entry",
        qty=33,
        price=Decimal("21.5608"),
        ts=T0,
        reason="orb_entry",
        position_id=12,
        stop_loss=Decimal("21.41"),
        pnl=None,
        pnl_r=None,
    )
    base.update(kw)
    return FillView(**base)


def alert(**kw: Any) -> AlertView:
    base: dict[str, Any] = dict(
        kind="alert", level="error", source="engine", message="something broke", ts=T0, data={}
    )
    base.update(kw)
    return AlertView(**base)


def position(**kw: Any) -> PositionLine:
    base: dict[str, Any] = dict(
        position_id=12,
        ticker="AAA",
        qty=33,
        entry=Decimal("21.5608"),
        last=Decimal("21.70"),
        stop=Decimal("21.41"),
        unrealized_pnl=Decimal("4.59"),
        unprotected_seconds=0,
        stop_working=True,
    )
    base.update(kw)
    return PositionLine(**base)


def status_view(**kw: Any) -> StatusView:
    base: dict[str, Any] = dict(
        now=T0,
        phase="open",
        session_date=date(2026, 10, 6),
        next_event_key="entry_cancel",
        next_event_at=datetime(2026, 10, 6, 15, 30, tzinfo=UTC),
        approval_mode="manual",
        blocking_switches=(),
        token_ok=True,
        token_age_hours=3.2,
        token_error=None,
        heartbeat_age_seconds=5.0,
        positions=(position(),),
        pending_count=1,
    )
    base.update(kw)
    return StatusView(**base)


def summary(**kw: Any) -> DailySummaryView:
    base: dict[str, Any] = dict(
        session_date=date(2026, 10, 6),
        trades=(
            TradeLine(
                ticker="AAA",
                qty=33,
                entry=Decimal("21.5608"),
                exit=Decimal("21.8887"),
                pnl=Decimal("10.82"),
                pnl_r=Decimal("2.17"),
                exit_reason="flatten_close",
            ),
        ),
        realized_pnl=Decimal("10.82"),
        fees=Decimal("1.00"),
        equity=Decimal("25010.82"),
        drawdown_pct=Decimal("0.0036"),
        open_positions=(),
        decisions=3,
        avg_decision_seconds=65.0,
        unprotected_seconds=45,
        blocking_switches=(),
        archive={"5m": 812, "1m": 8190},
    )
    base.update(kw)
    return DailySummaryView(**base)


# --- 1. entry proposal ------------------------------------------------------------------------------------


def test_entry_proposal_is_self_contained_with_link_and_buttons(r: MessageRenderer) -> None:
    msg = r.proposal(proposal(), ((APPROVE, REJECT),))
    assert msg.kind == "proposal"
    t = msg.text
    assert "ENTRY" in t
    assert "AAA" in t
    assert "33" in t
    assert "buy stop 21.56" in t
    assert "Stop loss: 21.41" in t
    assert "$4.95" in t
    assert "ORB breakout above 21.55" in t
    assert "orb_sip" in t
    assert "07:40 MT" in t  # expiry, 5 minutes after 07:35 MT
    assert "5 min" in t
    assert f'href="{BASE}/dashboard?proposal=42"' in t
    assert msg.buttons == ((APPROVE, REJECT),)
    assert [b.callback_data for b in msg.buttons[0]] == ["p:42:a:n1:mac", "p:42:r:n1:mac"]


def test_proposal_buttons_given_in_two_rows_are_put_in_one_row(r: MessageRenderer) -> None:
    msg = r.proposal(proposal(), ((APPROVE,), (REJECT,)))
    assert msg.buttons == ((APPROVE, REJECT),)


# --- 2. headlines, closed proposals, auto mode -------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "headline"),
    [("entry", "ENTRY"), ("stop", "PROTECTIVE STOP"), ("exit", "EXIT"), ("cancel", "CANCEL")],
)
def test_each_proposal_kind_has_its_headline(r: MessageRenderer, kind: str, headline: str) -> None:
    msg = r.proposal(proposal(kind=kind), ((APPROVE, REJECT),))
    assert msg.text.startswith(f"<b>{headline}")


def test_order_types_render_their_prices(r: MessageRenderer) -> None:
    assert "sell market" in r.proposal(proposal(side="sell", order_type="market", stop=None), ()).text
    limit = r.proposal(proposal(order_type="limit", stop=None, limit=Decimal("21.6")), ()).text
    assert "buy limit 21.60" in limit
    sl = r.proposal(proposal(order_type="stop_limit", limit=Decimal("21.7")), ()).text
    assert "buy stop 21.56 limit 21.70" in sl


@pytest.mark.parametrize(
    ("final", "via", "error", "line"),
    [
        ("approved", "telegram", None, "Approved via telegram"),
        ("submitted", "web", None, "Approved via web"),
        ("rejected", "telegram", None, "Rejected"),
        ("expired", None, None, "Expired"),
        ("failed", "telegram", "insufficient buying power", "Failed: insufficient buying power"),
        ("pending", None, None, "Already decided (pending)"),
    ],
)
def test_proposal_closed_renders_each_final_status(
    r: MessageRenderer, final: str, via: str | None, error: str | None, line: str
) -> None:
    v = proposal(status=final, error=error)
    text = r.proposal_closed(v, final, via)
    assert text.splitlines()[-1] == f"<b>{line}</b>"
    assert "AAA" in text and "Stop loss: 21.41" in text  # the same body
    assert "Expires" not in text  # nothing left to decide


def test_approved_entry_blocked_by_a_kill_switch_shows_why(r: MessageRenderer) -> None:
    """Stephen tapped Approve, but the kill switches (or a pause) blocked the entry: the service records it
    as rejected with the reason in `error`, and the closed message must say why (P3-T6 review)."""
    error = "entry blocked: kill switch manual_pause is tripped"
    v = proposal(status="rejected", decided_via="telegram", error=error)
    text = r.proposal_closed(v, "rejected", "telegram")
    assert text.splitlines()[-1] == f"<b>Rejected: {error}</b>"
    escaped = r.proposal_closed(dataclasses.replace(v, error="blocked <b>&"), "rejected", "telegram")
    assert escaped.splitlines()[-1] == "<b>Rejected: blocked &lt;b&gt;&amp;</b>"


def test_auto_approved_entry_has_auto_headline_and_no_buttons(r: MessageRenderer) -> None:
    for v in (
        proposal(status="auto_approved"),
        proposal(status="submitted", decided_via="auto"),
    ):
        msg = r.proposal(v, ((APPROVE, REJECT),))
        assert msg.text.startswith("<b>AUTO ENTRY")
        assert msg.buttons == ()
        assert "Expires" not in msg.text


# --- 3. fills ---------------------------------------------------------------------------------------------


def test_entry_fill(r: MessageRenderer) -> None:
    msg = r.fill(fill())
    assert msg.kind == "fill"
    assert "BOUGHT 33 AAA @ 21.5608, stop 21.41" in msg.text
    assert "07:35 MT" in msg.text
    assert f'href="{BASE}/trades?position=12"' in msg.text
    assert "P&amp;L" not in msg.text


def test_stop_fill_is_stop_hit_with_pnl(r: MessageRenderer) -> None:
    msg = r.fill(
        fill(side="sell", purpose="stop", price=Decimal("21.41"), pnl=Decimal("-4.95"), pnl_r=Decimal("-1"))
    )
    assert msg.kind == "stop_hit"
    assert "STOPPED OUT" in msg.text
    assert "SOLD 33 AAA @ 21.41" in msg.text
    assert "-$4.95 (-1.00R)" in msg.text


def test_flatten_fill(r: MessageRenderer) -> None:
    msg = r.fill(
        fill(
            side="sell",
            purpose="exit",
            reason="flatten_close",
            price=Decimal("21.8887"),
            pnl=Decimal("10.82"),
            pnl_r=Decimal("2.1717"),
        )
    )
    assert msg.kind == "flatten"
    assert "FLATTENED" in msg.text
    assert "+$10.82 (+2.17R)" in msg.text


def test_overlay_exit_fill_is_a_fill_with_its_reason(r: MessageRenderer) -> None:
    msg = r.fill(fill(side="sell", purpose="exit", reason="overlay_negative", pnl=Decimal("3"), pnl_r=None))
    assert msg.kind == "fill"
    assert "SOLD 33 AAA" in msg.text
    assert "overlay_negative" in msg.text
    assert "+$3.00" in msg.text


def test_overlay_message(r: MessageRenderer) -> None:
    v = OverlayView(
        decision="exit",
        spy_return=Decimal("-0.004512"),
        prior_close=Decimal("571.20"),
        price=Decimal("568.62"),
        position_ids=(12, 13),
        ts=datetime(2026, 10, 6, 19, 30, tzinfo=UTC),
        note="SPY below prior close: exit",
    )
    msg = r.overlay(v)
    assert msg.kind == "overlay"
    assert "EXIT" in msg.text
    assert "-0.45%" in msg.text
    assert "571.20" in msg.text and "568.62" in msg.text
    assert "#12" in msg.text and "#13" in msg.text
    assert "13:30 MT" in msg.text
    held = r.overlay(
        dataclasses.replace(
            v, decision="hold", spy_return=None, prior_close=None, price=None, position_ids=()
        )
    )
    assert "HOLD" in held.text and "SPY data unavailable" in held.text


# --- 4. alerts --------------------------------------------------------------------------------------------


def test_kill_switch_alert_for_drawdown_needs_web_reset(r: MessageRenderer) -> None:
    msg = r.alert(
        alert(
            kind="kill_switch",
            source="killswitch",
            message="kill switch max_drawdown_pct tripped: entries blocked",
            data={"switch": "max_drawdown_pct", "value": "0.1612", "threshold": "0.15"},
        )
    )
    assert msg.kind == "kill_switch"
    assert "max_drawdown_pct" in msg.text
    assert "16.12%" in msg.text and "15.00%" in msg.text
    assert "reset in the web app" in msg.text
    assert f'href="{BASE}/system"' in msg.text


def test_kill_switch_alert_for_manual_pause_does_not_need_web_reset(r: MessageRenderer) -> None:
    msg = r.alert(
        alert(
            kind="kill_switch",
            source="killswitch",
            level="warning",
            message="manual pause: entries blocked",
            data={"actor": "telegram"},
        )
    )
    assert msg.kind == "kill_switch"
    assert "web app" not in msg.text
    assert "/resume" in msg.text


def test_daily_loss_switch_resets_by_itself(r: MessageRenderer) -> None:
    msg = r.alert(
        alert(
            kind="kill_switch",
            source="killswitch",
            message="kill switch daily_loss_pct tripped: entries blocked",
            data={"switch": "daily_loss_pct", "value": "0.051", "threshold": "0.05"},
        )
    )
    assert "next session" in msg.text
    assert "web app" not in msg.text


def test_job_failure_alert_cuts_the_error(r: MessageRenderer) -> None:
    msg = r.alert(
        alert(
            kind="job_failure",
            source="job.premarket",
            message="premarket failed for 2026-10-06",
            data={"error": "X" * 1000},
        )
    )
    assert msg.kind == "job_failure"
    assert "premarket" in msg.text and "2026-10-06" in msg.text
    assert "X" * 300 in msg.text
    assert "X" * 301 not in msg.text


def test_token_failure_alert(r: MessageRenderer) -> None:
    msg = r.alert(
        alert(
            kind="token_failure",
            source="questrade.token",
            message="token refresh failed",
            data={"error": "HTTP 400"},
        )
    )
    assert msg.kind == "token_failure"
    assert "Questrade token refresh failed: HTTP 400. Paste a new token in Settings." in msg.text


def test_escalation_alert_includes_message_and_data(r: MessageRenderer) -> None:
    msg = r.alert(
        alert(
            kind="escalation",
            source="proposals",
            message="position 12 is still unprotected (alert 3)",
            data={"position_id": 12, "proposal_id": 42},
        )
    )
    assert msg.kind == "escalation"
    assert "position 12 is still unprotected (alert 3)" in msg.text
    assert "position_id: 12" in msg.text and "proposal_id: 42" in msg.text


def test_generic_alert(r: MessageRenderer) -> None:
    msg = r.alert(alert())
    assert msg.kind == "alert"
    assert "something broke" in msg.text and "engine" in msg.text
    assert f'href="{BASE}/system"' in msg.text


# --- 5. daily summary -------------------------------------------------------------------------------------


def test_daily_summary(r: MessageRenderer) -> None:
    msg = r.daily_summary(summary(), ((YES, NO),))
    assert msg.kind == "daily_summary"
    t = msg.text
    assert "2026-10-06" in t
    assert "AAA" in t and "+$10.82 (+2.17R)" in t and "flatten_close" in t
    assert "Realized P&amp;L: +$10.82" in t
    assert "Fees: $1.00" in t
    assert "Equity: $25,010.82" in t
    assert "Drawdown: 0.36%" in t
    assert "1m 5s" in t  # average decision time
    assert "Unprotected time: 45s" in t
    assert "1m: 8190" in t and "5m: 812" in t
    assert "Rules followed?" in t
    assert f'href="{BASE}/journal?date=2026-10-06"' in t
    assert "STILL OPEN" not in t
    assert msg.buttons == ((YES, NO),)


def test_daily_summary_with_an_open_position_is_loud(r: MessageRenderer) -> None:
    t = r.daily_summary(summary(open_positions=(position(),), trades=()), ((YES, NO),)).text
    assert "STILL OPEN" in t
    assert "No trades" in t


def test_journal_answered(r: MessageRenderer) -> None:
    assert r.journal_answered(date(2026, 10, 6), True).text == "Journal 2026-10-06: rules followed = Yes"
    assert "= No" in r.journal_answered(date(2026, 10, 6), False).text


# --- 6. escaping and truncation ---------------------------------------------------------------------------


def test_dynamic_strings_are_escaped(r: MessageRenderer) -> None:
    msg = r.proposal(proposal(reason="spike <b>& run", ticker="A<B"), ((APPROVE, REJECT),))
    assert "spike &lt;b&gt;&amp; run" in msg.text
    assert "A&lt;B" in msg.text
    assert "<b>&" not in msg.text
    assert "&lt;script&gt;" in r.reply("<script>").text


def test_long_brief_is_cut_on_a_line_break(r: MessageRenderer) -> None:
    brief = "\n".join(f"line {i:04d} " + "x" * 90 for i in range(100))  # ~10,000 characters
    assert len(brief) > 10_000 - 200
    msg = r.premarket_brief(date(2026, 10, 6), brief)
    assert msg.kind == "premarket_brief"
    assert len(msg.text) < TELEGRAM_LIMIT
    lines = msg.text.splitlines()
    assert lines[-1] == "… (truncated, see the web app)"
    assert lines[-2].startswith("line ") and lines[-2].endswith("x" * 90)  # a whole line, not cut


def test_one_giant_line_is_cut_without_breaking_an_entity(r: MessageRenderer) -> None:
    msg = r.reply("&" * 10_000)
    assert len(msg.text) <= TELEGRAM_LIMIT
    body = msg.text.rsplit("\n", 1)[0]
    assert body.endswith("&amp;")


def test_truncation_keeps_buttons(r: MessageRenderer) -> None:
    long_reason = "\n".join(["reason"] * 2000)
    msg = r.proposal(proposal(reason=long_reason), ((APPROVE, REJECT),))
    assert len(msg.text) <= TELEGRAM_LIMIT
    assert msg.buttons == ((APPROVE, REJECT),)


# --- 7. times in MT ---------------------------------------------------------------------------------------


def test_times_render_in_mountain_time_across_dst() -> None:
    oct6, nov2 = datetime(2026, 10, 6, 13, 35, 5, tzinfo=UTC), datetime(2026, 11, 2, 14, 35, 5, tzinfo=UTC)
    assert fmt_time(oct6, MT) == "07:35 MT"  # MDT, UTC-6
    # The plan's MST case (07:35 MT on Mon 2026-11-02, the first session after the clocks change on Sun
    # 2026-11-01) is checked on a Mountain zone that still changes its clocks. tzdata 2026c puts
    # America/Edmonton on permanent UTC-6 from 2026 (Alberta), so there the rendering must follow the tz
    # database, never a fixed offset.
    denver = ZoneInfo("America/Denver")
    assert fmt_time(oct6, denver) == "07:35 MT"
    assert fmt_time(nov2, denver) == "07:35 MT"
    assert fmt_time(nov2, MT) == f"{nov2.astimezone(MT):%H:%M} MT"


def test_non_mountain_zone_uses_its_own_abbreviation() -> None:
    assert fmt_time(T0, ZoneInfo("America/New_York")) == "09:35 EDT"


# --- formatting helpers -----------------------------------------------------------------------------------


def test_formatters() -> None:
    assert fmt_money(Decimal("1234.56")) == "$1,234.56"
    assert fmt_money(Decimal("-12.3")) == "-$12.30"
    assert fmt_money(Decimal("0.005")) == "$0.01"
    assert fmt_price(Decimal("21.5608")) == "21.5608"
    assert fmt_price(Decimal("21.41")) == "21.41"
    assert fmt_price(Decimal("21.4")) == "21.40"
    assert fmt_price(Decimal("21.56081")) == "21.5608"
    assert fmt_price(Decimal("21.412")) == "21.412"
    assert fmt_pct(Decimal("0.0123")) == "+1.23%"
    assert fmt_pct(Decimal("-0.004512")) == "-0.45%"
    assert fmt_pct(Decimal("0")) == "+0.00%"
    assert fmt_duration(200) == "3m 20s"
    assert fmt_duration(45) == "45s"
    assert fmt_duration(0) == "0s"
    assert fmt_duration(3725) == "1h 2m 5s"
    assert link(BASE + "/", "/system") == f"{BASE}/system"
    assert link(BASE, "journal?date=2026-10-06") == f"{BASE}/journal?date=2026-10-06"


# --- status, positions, pnl, preopen, commands ------------------------------------------------------------


def test_status_and_checkin(r: MessageRenderer) -> None:
    msg = r.status(status_view())
    t = msg.text
    assert "open" in t
    assert "entry_cancel" in t and "09:30 MT" in t
    assert "manual" in t
    assert "Kill switches: none" in t
    assert "OK" in t
    assert "5s" in t
    assert "AAA" in t
    assert "Pending proposals: 1" in t
    checkin = r.checkin(status_view(), "11:30")
    assert checkin.kind == "checkin"
    assert "Check-in 11:30" in checkin.text


def test_status_when_blocked_and_token_bad(r: MessageRenderer) -> None:
    t = r.status(
        status_view(
            phase="closed_day",
            next_event_key=None,
            next_event_at=None,
            blocking_switches=("max_drawdown_pct",),
            token_ok=False,
            token_error="refresh token expired",
            heartbeat_age_seconds=None,
            positions=(),
            pending_count=0,
        )
    ).text
    assert "none today" in t
    assert "max_drawdown_pct" in t
    assert "refresh token expired" in t
    assert "no heartbeat" in t


def test_positions(r: MessageRenderer) -> None:
    lines = (
        position(),
        position(
            position_id=13,
            ticker="BBB",
            last=None,
            unrealized_pnl=None,
            stop_working=False,
            unprotected_seconds=200,
        ),
    )
    t = r.positions(lines, T0).text
    assert "AAA" in t and "21.5608" in t and "21.70" in t and "+$4.59" in t
    assert "BBB" in t and "n/a" in t and "(no stop order)" in t and "3m 20s" in t
    assert "No open positions" in r.positions((), T0).text


def test_pnl(r: MessageRenderer) -> None:
    t = r.pnl(
        PnlView(
            session_date=date(2026, 10, 6),
            realized_today=Decimal("10.82"),
            unrealized=Decimal("-2"),
            week_to_date=Decimal("30"),
            equity=Decimal("25010.82"),
            peak_equity=Decimal("25100"),
            drawdown_pct=Decimal("0.0036"),
        )
    ).text
    assert "+$10.82" in t and "-$2.00" in t and "+$30.00" in t
    assert "$25,010.82" in t and "$25,100.00" in t and "0.36%" in t


def test_preopen(r: MessageRenderer) -> None:
    msg = r.preopen(
        PreopenView(
            session_date=date(2026, 10, 6),
            approval_mode="manual",
            checks=(
                Check("token", True, "info", "refreshed 2 h ago"),
                Check("universe", False, "warning", "only 3 symbols"),
                Check("worker", False, "error", "no heartbeat"),
            ),
        )
    )
    assert msg.kind == "preopen"
    t = msg.text
    assert "manual" in t
    assert "OK token" in t and "WARNING universe" in t and "ERROR worker" in t


def test_help_lists_the_seven_commands(r: MessageRenderer) -> None:
    t = r.help().text
    for cmd in ("/status", "/positions", "/pnl", "/pending", "/pause", "/resume", "/help"):
        assert cmd in t


def test_pause_confirm_weekly_and_reply(r: MessageRenderer) -> None:
    buttons = ((Button("Yes, pause", "s:1:y:n3:mac"), Button("No", "s:1:n:n3:mac")),)
    pc = r.pause_confirm(buttons)
    assert pc.text == "Pause new entries? Exits and stops keep working."
    assert pc.buttons == buttons
    wl = r.weekly_link(date(2026, 10, 9))
    assert wl.kind == "weekly_report"
    assert "2026-10-09" in wl.text and BASE in wl.text
    assert r.reply("Paused: new entries are blocked.").text == "Paused: new entries are blocked."
    assert r.reply("").text  # never empty


# --- 8. protocol and non-empty ----------------------------------------------------------------------------


def test_message_renderer_satisfies_renderer_and_never_returns_empty(r: MessageRenderer) -> None:
    rr: Renderer = r
    pv = proposal()
    messages: list[OutboundMessage] = [
        rr.proposal(pv, ((APPROVE, REJECT),)),
        rr.fill(fill()),
        rr.overlay(
            OverlayView("hold", None, None, None, (), T0, ""),
        ),
        rr.alert(alert(message="", data={})),
        rr.daily_summary(summary(trades=(), archive={}, avg_decision_seconds=None), ()),
        rr.journal_answered(date(2026, 10, 6), True),
        rr.premarket_brief(date(2026, 10, 6), ""),
        rr.preopen(PreopenView(date(2026, 10, 6), "auto", ())),
        rr.checkin(status_view(), "13:30"),
        rr.status(status_view()),
        rr.positions((), T0),
        rr.pnl(PnlView(date(2026, 10, 6), *(Decimal(0),) * 6)),
        rr.pause_confirm(()),
        rr.help(),
        rr.reply(""),
        rr.weekly_link(date(2026, 10, 9)),
    ]
    for m in messages:
        assert isinstance(m, OutboundMessage)
        assert m.text.strip()
        assert len(m.text) <= TELEGRAM_LIMIT
    assert rr.proposal_closed(pv, "expired", None).strip()


# --- P3-T4 fix round 1 ------------------------------------------------------------------------------------

BOT_TOKEN = "7123456789:AAH4sEcReTvAlUe-abcdefghijklmnopqrstu"
REFRESH = "QtR3fr35hT0k3nValue123"


def test_alert_and_closed_proposal_text_is_masked(r: MessageRenderer) -> None:
    tg_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    views = [
        alert(kind="job_failure", source="job.nightly", data={"error": f"POST {tg_url} timed out"}),
        alert(kind="token_failure", data={"error": f"HTTP 400 for ...?refresh_token={REFRESH}"}),
        alert(kind="escalation", message=f"token {BOT_TOKEN}", data={"refresh_token": REFRESH, "n": 1}),
        alert(kind="alert", data={"body": f'{{"refresh_token": "{REFRESH}"}}'}),
        alert(kind="kill_switch", message=f"via {tg_url}", data={"switch": "manual_pause"}),
    ]
    for v in views:
        text = r.alert(v).text
        assert "AAH4sEcReTvAlUe" not in text and REFRESH not in text, text
    assert "refresh_token: [REDACTED]" in r.alert(views[2]).text
    closed = r.proposal_closed(proposal(error=f"send failed: {tg_url}"), "failed", None)
    assert "AAH4sEcReTvAlUe" not in closed
    # a token cut in half by the 300-character error limit is masked before the cut
    job = r.alert(alert(kind="job_failure", data={"error": "x" * 280 + f" {tg_url}"})).text
    assert "AAH4sEcReTv" not in job


def test_closed_proposal_tail_is_capped_and_every_cut_closes_its_tags(r: MessageRenderer) -> None:
    text = r.proposal_closed(proposal(error="refused & <why> " * 2000), "failed", None)
    assert len(text) <= TELEGRAM_LIMIT
    final = text.splitlines()[-1]
    assert final.startswith("<b>Failed: ") and final.endswith("…</b>")
    assert len(r.proposal_closed(proposal(), "approved", "v" * 9000)) <= TELEGRAM_LIMIT
    assert len(r.proposal_closed(proposal(), "x" * 9000, None)) <= TELEGRAM_LIMIT
    # a raw over-long tail and a one-line bold body are both cut without leaving a tag open
    huge = _fit("<b>" + "&amp;" * 3000 + "</b>", "<b>" + "y" * 9000 + "</b>")
    assert len(huge) <= TELEGRAM_LIMIT
    assert huge.count("<b>") == huge.count("</b>") == 2
    assert "&am\n" not in huge and not huge.split("\n")[0].endswith("&")


def test_non_percentage_and_float_values_render_as_decimals(r: MessageRenderer) -> None:
    ks = r.alert(
        alert(
            kind="kill_switch",
            message="expectancy below threshold",
            data={"switch": "expectancy", "value": 0.1 + 0.2, "threshold": -0.05},
        )
    ).text
    assert "Value 0.30 vs threshold -0.05" in ks
    data = {"spy_return": -0.0000001, "big": Decimal("1E+3"), "id": 12, "gone": None}
    esc = r.alert(alert(kind="escalation", data=data)).text
    assert "spy_return: 0.00" in esc and "big: 1000.00" in esc and "id: 12" in esc
    assert "gone" not in esc and "None" not in esc and "E+" not in esc


def test_auto_approval_reads_approved_auto(r: MessageRenderer) -> None:
    assert r.proposal_closed(proposal(status="auto_approved"), "auto_approved", None).endswith(
        "<b>Approved (auto)</b>"
    )
    assert r.proposal_closed(proposal(), "submitted", "auto").endswith("<b>Approved (auto)</b>")


def test_alert_from_another_day_shows_its_date() -> None:
    same_day = MessageRenderer(BASE, MT, clock=FixedClock(T0 + timedelta(hours=2)))
    next_day = MessageRenderer(BASE, MT, clock=FixedClock(T0 + timedelta(days=1)))
    assert "at 07:35 MT" in same_day.alert(alert()).text
    assert "at 2026-10-06 07:35 MT" in next_day.alert(alert()).text
    assert "2026-10-06" not in next_day.fill(fill()).text  # other messages show the time only


def test_fmt_time_rejects_a_naive_datetime() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        fmt_time(datetime(2026, 10, 6, 13, 35), MT)
