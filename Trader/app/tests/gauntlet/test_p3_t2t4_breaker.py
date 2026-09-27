"""P3-B5 gauntlet: breaker tests for P3-T2 (logging unification) and P3-T4 (MessageRenderer).

Fakes only: no network, no database. The token strings below are made-up, token-shaped values.
"""

import importlib.resources
import json
import logging
import re
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import structlog

from trader.adapters.telegram.callbacks import MAX_DATA_BYTES, CallbackSigner
from trader.logging_setup import configure_logging
from trader.notify.messages import (
    TELEGRAM_LIMIT,
    TRUNCATED_LINE,
    MessageRenderer,
    fmt_duration,
    fmt_money,
    fmt_pct,
    fmt_price,
    fmt_time,
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
    StatusView,
    TradeLine,
)

MT = ZoneInfo("America/Edmonton")
BASE = "http://trader.home:8080"
T0 = datetime(2026, 10, 6, 13, 35, 5, tzinfo=UTC)
FAKE_BOT_TOKEN = "7123456789:AAH4sEcReTvAlUe-abcdefghijklmnopqrstu"
FAKE_BOT_SECRET = "AAH4sEcReTvAlUe"
FAKE_REFRESH = "QtR3fr35hT0k3nValue123"
FAKE_ACCESS = "QtAcC3ssT0k3nValue456"

EVIL = '<b>X</b></a><a href="http://evil.example/">tap</a> & AT&T <i>x</i> &lt; &#60;'
APPROVE = Button("Approve", "p:42:a:n1:mac")
REJECT = Button("Reject", "p:42:r:n1:mac")


# --- Telegram HTML checker ---------------------------------------------------------------------------------

_TAG = re.compile(r"<(/?)([A-Za-z][A-Za-z0-9]*)([^<>]*)>")
_ENTITY = re.compile(r"&(?:amp|lt|gt|quot|#[0-9]+|#x[0-9A-Fa-f]+);")
_HREF = re.compile(r'^\s+href="([^"<>]*)"$')
ALLOWED_TAGS = {"b", "a"}


def html_problems(text: str) -> list[str]:
    """What Telegram's HTML parse mode would reject, or what would be an injection: unknown tags, a tags
    with anything but one quoted href, unbalanced tags, a bare `<`/`>` in text, a bare `&`."""
    problems: list[str] = []
    stack: list[str] = []
    pos = 0
    text_parts: list[str] = []
    for m in _TAG.finditer(text):
        text_parts.append(text[pos : m.start()])
        pos = m.end()
        closing, name, attrs = m.group(1), m.group(2).lower(), m.group(3)
        if name not in ALLOWED_TAGS:
            problems.append(f"tag <{closing}{name}> is not produced by the renderer: {m.group(0)!r}")
            continue
        if closing:
            if attrs.strip():
                problems.append(f"closing tag with attributes: {m.group(0)!r}")
            if not stack or stack[-1] != name:
                problems.append(f"unbalanced </{name}> (open: {stack})")
            else:
                stack.pop()
        else:
            if name == "a":
                href = _HREF.match(attrs)
                if href is None:
                    problems.append(f"bad <a> attributes: {m.group(0)!r}")
                elif "evil.example" in href.group(1):
                    problems.append(f"injected link: {m.group(0)!r}")
                if "a" in stack:
                    problems.append("nested <a>")
            elif attrs.strip():
                problems.append(f"attributes on <{name}>: {m.group(0)!r}")
            stack.append(name)
    text_parts.append(text[pos:])
    if stack:
        problems.append(f"unclosed tags at the end: {stack}")
    for part in text_parts:
        if "<" in part or ">" in part:
            problems.append(f"bare < or > in text: {part[:80]!r}")
        for amp in re.finditer("&", part):
            if not _ENTITY.match(part, amp.start()):
                problems.append(f"bare & in text: {part[max(0, amp.start() - 20) : amp.start() + 20]!r}")
    return problems


def assert_telegram_html(text: str, where: str) -> None:
    problems = html_problems(text)
    assert not problems, f"{where}: {problems}"


# --- view builders -----------------------------------------------------------------------------------------


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


def trade(**kw: Any) -> TradeLine:
    base: dict[str, Any] = dict(
        ticker="AAA",
        qty=33,
        entry=Decimal("21.5608"),
        exit=Decimal("21.8887"),
        pnl=Decimal("10.82"),
        pnl_r=Decimal("2.17"),
        exit_reason="flatten_close",
    )
    base.update(kw)
    return TradeLine(**base)


def summary(**kw: Any) -> DailySummaryView:
    base: dict[str, Any] = dict(
        session_date=date(2026, 10, 6),
        trades=(trade(),),
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


# ===========================================================================================================
# P3-T4: MessageRenderer
# ===========================================================================================================


def _evil_messages(r: MessageRenderer) -> dict[str, str]:
    """Every renderer method, with EVIL in every dynamic string it shows."""
    evil_pos = position(ticker=EVIL)
    evil_status = status_view(
        next_event_key=EVIL,
        approval_mode=EVIL,
        blocking_switches=(EVIL, "manual_pause"),
        token_ok=False,
        token_error=EVIL,
        positions=(evil_pos,),
    )
    evil_proposal = proposal(ticker=EVIL, side=EVIL, reason=EVIL, strategy_key=EVIL, kind=EVIL)
    evil_data = {EVIL: EVIL, "switch": EVIL, "value": EVIL, "threshold": EVIL, "error": EVIL}
    out: dict[str, OutboundMessage | str] = {
        "proposal": r.proposal(evil_proposal, ((APPROVE, REJECT),)),
        "proposal_order_type": r.proposal(proposal(order_type=EVIL), ((APPROVE, REJECT),)),
        "proposal_auto": r.proposal(proposal(ticker=EVIL, status="auto_approved"), ()),
        "proposal_closed_failed": r.proposal_closed(proposal(ticker=EVIL, error=EVIL), "failed", EVIL),
        "proposal_closed_rejected": r.proposal_closed(proposal(error=EVIL), "rejected", None),
        "proposal_closed_approved": r.proposal_closed(proposal(), "approved", EVIL),
        "proposal_closed_other": r.proposal_closed(proposal(), EVIL, None),
        "fill_entry": r.fill(fill(ticker=EVIL, side=EVIL)),
        "fill_exit": r.fill(fill(ticker=EVIL, purpose="exit", side="sell", reason=EVIL, pnl=Decimal("-1"))),
        "fill_other_purpose": r.fill(fill(purpose=EVIL)),
        "overlay": r.overlay(
            OverlayView(
                decision=EVIL,
                spy_return=Decimal("-0.0071"),
                prior_close=Decimal("571.2"),
                price=Decimal("567.1"),
                position_ids=(1, 2),
                ts=T0,
                note=EVIL,
            )
        ),
        "daily_summary": r.daily_summary(
            summary(
                trades=(trade(ticker=EVIL, exit_reason=EVIL),),
                open_positions=(evil_pos,),
                blocking_switches=(EVIL,),
                archive={EVIL: 3},
            ),
            ((Button("Yes", "j:20261006:y:n2:mac"), Button("No", "j:20261006:n:n2:mac")),),
        ),
        "premarket_brief": r.premarket_brief(date(2026, 10, 6), EVIL + "\n" + EVIL),
        "preopen": r.preopen(
            PreopenView(
                session_date=date(2026, 10, 6),
                approval_mode=EVIL,
                checks=(
                    Check(name=EVIL, ok=False, level="error", detail=EVIL),
                    Check(name="token", ok=True, level="info", detail=EVIL),
                ),
            )
        ),
        "checkin": r.checkin(evil_status, EVIL),
        "status": r.status(evil_status),
        "status_phase": r.status(status_view(phase=EVIL)),
        "positions": r.positions([evil_pos], T0),
        "reply": r.reply(EVIL),
        "pause_confirm": r.pause_confirm(((APPROVE,),)),
        "help": r.help(),
        "pnl": r.pnl(
            PnlView(
                session_date=date(2026, 10, 6),
                realized_today=Decimal("-5"),
                unrealized=Decimal("0"),
                week_to_date=Decimal("12.5"),
                equity=Decimal("25000"),
                peak_equity=Decimal("25100"),
                drawdown_pct=Decimal("0.004"),
            )
        ),
        "weekly_link": r.weekly_link(date(2026, 10, 9)),
    }
    for kind in ("kill_switch", "job_failure", "token_failure", "escalation", "alert"):
        out[f"alert_{kind}"] = r.alert(
            alert(kind=kind, level=EVIL, source=EVIL, message=EVIL, data=evil_data)
        )
    return {k: (v if isinstance(v, str) else v.text) for k, v in out.items()}


def test_every_dynamic_string_is_escaped_everywhere_including_links() -> None:
    """HTML injection: a ticker, reason, strategy, headline, error, check or note containing tags, a closing
    </a>, a new <a href> or a bare & must come out as text. Otherwise Telegram rejects the message with
    "can't parse entities" (the alert or proposal is lost) or shows a link Stephen did not ask for.
    The base URL itself (from the env) is also escaped inside href."""
    for base in (BASE, 'http://h/x?a=1&b=2"><b>x', "http://trader.home:8080/"):
        r = MessageRenderer(base, MT)
        for name, text in _evil_messages(r).items():
            assert text.strip(), name
            assert_telegram_html(text, f"{name} (base {base!r})")
            # (the escaped text may still contain the words href="..."; html_problems checks real tags)
            assert '<a href="http://evil.example/">' not in text, name
            assert "<i>" not in text, name


def test_long_messages_are_cut_on_safe_boundaries() -> None:
    """Message length: a 50-name pre-market brief with & and < in every line, a 300-trade summary, 200
    open positions and a single 12,000-character reply line must all be at most TELEGRAM_LIMIT, end with
    the truncation line, keep their buttons, and never leave half an entity (`&am`) or tag behind."""
    r = MessageRenderer(BASE, MT)
    names = [
        f"{i:02d}. T{i}&Co <NYSE:T{i}> gap +{i}.1% on 'Q3 beat & raise' <b>catalyst</b> "
        + "news & more <" * 12
        for i in range(50)
    ]
    brief = "\n".join(names)
    buttons = ((Button("Yes", "j:20261006:y:n2:mac"), Button("No", "j:20261006:n:n2:mac")),)
    msgs = {
        "premarket_brief": r.premarket_brief(date(2026, 10, 6), brief),
        "daily_summary": r.daily_summary(
            summary(
                trades=tuple(trade(ticker=f"T{i}&<b>", exit_reason="stop & <loss>") for i in range(300)),
                open_positions=(position(ticker="A&B"),),
            ),
            buttons,
        ),
        "checkin": r.checkin(
            status_view(positions=tuple(position(ticker="A&<") for _ in range(200))), "10:00"
        ),
        "reply_one_line": r.reply("&<>" * 4000),
        "brief_one_line": r.premarket_brief(date(2026, 10, 6), "&" * 12000),
    }
    for name, msg in msgs.items():
        assert len(msg.text) <= TELEGRAM_LIMIT, (name, len(msg.text))
        assert msg.text.rstrip().endswith(TRUNCATED_LINE), name
        assert_telegram_html(msg.text, name)
    assert msgs["daily_summary"].buttons == buttons  # the Yes/No journal buttons are kept


def test_proposal_closed_with_a_huge_error_stays_within_the_limit() -> None:
    """proposal_closed keeps its final status line (the `tail`) whole. A broker refusal or a blocked-entry
    reason can be long (an exception text, a Questrade error body). If the tail alone is longer than the
    limit, the edited message exceeds 4096 characters, Telegram rejects the edit, and the tapped proposal
    keeps its buttons and never shows that it failed."""
    r = MessageRenderer(BASE, MT)
    long_error = "Questrade 400: " + "order rejected & <details> " * 300
    for status in ("failed", "rejected"):
        text = r.proposal_closed(proposal(error=long_error), status, "telegram")
        assert len(text) <= TELEGRAM_LIMIT, (status, len(text))
        assert_telegram_html(text, status)
    long_via = "x" * 5000
    text = r.proposal_closed(proposal(), "approved", long_via)
    assert len(text) <= TELEGRAM_LIMIT, ("approved via", len(text))


def test_decimal_values_render_without_float_or_exponent_leakage() -> None:
    """Values: negatives, zero, the numeric(14,4) extremes, tiny values and exponent-form Decimals format as
    money/prices, never as `1E+3`, `-0.00` or a float tail. JSONB data decoded from event_log arrives as
    float, and a kill-switch value must not render as 0.30000000000000004."""
    assert fmt_money(Decimal("-12.3")) == "-$12.30"
    assert fmt_money(Decimal("0")) == "$0.00"
    assert fmt_money(Decimal("-0")) == "$0.00"
    assert fmt_money(Decimal("-0.004")) == "$0.00"  # no "-$0.00"
    assert fmt_money(Decimal("0.005")) == "$0.01"
    assert fmt_money(Decimal("9999999999.9999")) == "$10,000,000,000.00"
    assert fmt_money(Decimal("-1234567.891")) == "-$1,234,567.89"
    assert fmt_money(Decimal("1E+3")) == "$1,000.00"
    assert fmt_price(Decimal("0.0001")) == "0.0001"
    assert fmt_price(Decimal("0E-10")) == "0.00"
    assert fmt_price(Decimal("1E+2")) == "100.00"
    assert fmt_price(Decimal("1E-12")) == "0.00"
    assert fmt_price(Decimal("21.56080")) == "21.5608"
    assert fmt_price(Decimal("9999999999.9999")) == "9999999999.9999"
    assert fmt_pct(Decimal("-0.0123")) == "-1.23%"
    assert fmt_pct(Decimal("0")) == "+0.00%"
    assert "-0.00" not in fmt_pct(Decimal("-0.00001"))
    assert fmt_duration(-5) == "0s"
    assert fmt_duration(3599.6) == "1h 0m 0s"

    r = MessageRenderer(BASE, MT)
    closing = r.fill(
        fill(
            purpose="exit",
            side="sell",
            reason="target",
            price=Decimal("2.15608E+1"),
            pnl=Decimal("-1.082E+1"),
            pnl_r=Decimal("-2.17E0"),
        )
    ).text
    assert "@ 21.5608" in closing
    assert "-$10.82 (-2.17R)" in closing
    tiny_loss = r.fill(fill(purpose="exit", side="sell", pnl=Decimal("-0.001"), pnl_r=Decimal("-0.001"))).text
    assert "-$0.00" not in tiny_loss and "-0.00R" not in tiny_loss
    texts = [
        closing,
        tiny_loss,
        r.pnl(PnlView(date(2026, 10, 6), *(Decimal("1E+4"),) * 5, Decimal("5E-3"))).text,
    ]
    for t in texts:
        assert not re.search(r"\d[eE][+-]?\d", t), t
        assert "Decimal(" not in t

    # jsonb decoded as float, a switch that is not a percentage
    ks = r.alert(
        alert(
            kind="kill_switch",
            message="expectancy below threshold",
            data={"switch": "expectancy", "value": 0.1 + 0.2, "threshold": -0.05},
        )
    ).text
    assert "0.30000000000000004" not in ks, ks


def test_missing_and_none_fields_render_cleanly() -> None:
    """None and missing fields everywhere: no crash, no empty message, no literal `None`, valid HTML."""
    r = MessageRenderer(BASE, MT)
    bare_pos = position(last=None, stop=None, unrealized_pnl=None, stop_working=False, unprotected_seconds=75)
    msgs: dict[str, str] = {
        "proposal_market": r.proposal(
            proposal(order_type="market", stop=None, limit=None, stop_loss=None, risk_usd=None, reason=""), ()
        ).text,
        "proposal_stop_no_price": r.proposal(
            proposal(order_type="stop_limit", stop=None, limit=None), ()
        ).text,
        "closed_failed_no_error": r.proposal_closed(proposal(error=None), "failed", None),
        "closed_rejected_no_error": r.proposal_closed(proposal(error=None), "rejected", None),
        "fill_no_position": r.fill(fill(position_id=None, stop_loss=None, reason="")).text,
        "fill_pnl_no_r": r.fill(
            fill(purpose="exit", side="sell", reason="", pnl=Decimal("3"), pnl_r=None)
        ).text,
        "overlay_no_data": r.overlay(
            OverlayView(
                decision="hold",
                spy_return=None,
                prior_close=None,
                price=None,
                position_ids=(),
                ts=T0,
                note="",
            )
        ).text,
        "status_bare": r.status(
            status_view(
                next_event_key=None,
                next_event_at=None,
                token_ok=False,
                token_age_hours=None,
                token_error=None,
                heartbeat_age_seconds=None,
                positions=(bare_pos,),
                pending_count=0,
            )
        ).text,
        "summary_bare": r.daily_summary(
            summary(
                trades=(trade(pnl_r=None, exit_reason=""),),
                avg_decision_seconds=None,
                archive={},
                decisions=0,
            ),
            (),
        ).text,
        "preopen_no_checks": r.preopen(PreopenView(date(2026, 10, 6), "manual", ())).text,
        "brief_empty": r.premarket_brief(date(2026, 10, 6), "").text,
        "reply_empty": r.reply("").text,
        "positions_empty": r.positions([], T0).text,
    }
    for kind in ("kill_switch", "job_failure", "token_failure", "escalation", "alert"):
        msgs[f"alert_{kind}_bare"] = r.alert(alert(kind=kind, message="", data={})).text
        msgs[f"alert_{kind}_nones"] = r.alert(
            alert(
                kind=kind, message="", data={"switch": None, "value": None, "threshold": None, "error": None}
            )
        ).text
    for name, text in msgs.items():
        assert text.strip(), name
        assert "None" not in text, (name, text)
        assert_telegram_html(text, name)


def test_buttons_pass_through_and_real_callback_data_fits_64_bytes() -> None:
    """Callback data is at most 64 bytes (Telegram's limit): the renderer passes the given buttons through
    unchanged (proposal buttons in one row) and the real signer's data at maximal refs fits the limit."""
    signer = CallbackSigner.derive("session-secret-for-tests")
    longest = [
        signer.data("p", "9" * 19, "a", "Zz_-Zz_-"),
        signer.data("s", "9" * 19, "y", "Zz_-Zz_-"),
        signer.data("j", "20261006", "n", "Zz_-Zz_-"),
    ]
    for data in longest:
        assert len(data.encode()) <= MAX_DATA_BYTES <= 64
    r = MessageRenderer(BASE, MT)
    a, rj = Button("✅ Approve", longest[0]), Button("❌ Reject", longest[0].replace(":a:", ":r:"))
    msg = r.proposal(proposal(), ((a,), (rj,)))
    assert msg.buttons == ((a, rj),)
    for row in msg.buttons:
        for b in row:
            assert len(b.callback_data.encode()) <= 64
    yes_no = ((Button("Yes", longest[2]), Button("No", longest[2])),)
    assert r.daily_summary(summary(), yes_no).buttons == yes_no
    assert r.pause_confirm(yes_no).buttons == yes_no


def _tzdata_package_zone(key: str) -> ZoneInfo:
    """The same zone from the pip `tzdata` package (what a slim Docker image without system zoneinfo uses)."""
    area, city = key.split("/")
    res = importlib.resources.files("tzdata.zoneinfo").joinpath(area).joinpath(city)
    with res.open("rb") as f:
        return ZoneInfo.from_file(f, key=key)


def test_times_follow_the_tz_database_across_dst_dates() -> None:
    """Times in MT across the DST dates come from the tz database, not a fixed offset: 09:35 ET renders as
    07:35 MT both in January (MST, UTC-7) and October (UTC-6). Around the 2026/2027 change dates the
    rendering matches the installed tz database, and the system zoneinfo and the pip tzdata package agree
    (so the Mac and the Docker image show the same times)."""
    pkg = _tzdata_package_zone("America/Edmonton")
    instants = [
        datetime(2026, 1, 15, 14, 35, 5, tzinfo=UTC),  # 09:35 ET in winter
        datetime(2026, 3, 8, 8, 59, tzinfo=UTC),  # just before the March 2026 change in Alberta
        datetime(2026, 3, 8, 9, 0, tzinfo=UTC),
        datetime(2026, 3, 9, 13, 35, 5, tzinfo=UTC),
        T0,
        datetime(2026, 11, 1, 7, 59, tzinfo=UTC),
        datetime(2026, 11, 1, 8, 0, tzinfo=UTC),
        datetime(2026, 11, 2, 14, 35, 5, tzinfo=UTC),
        datetime(2027, 3, 14, 9, 0, tzinfo=UTC),
        datetime(2027, 11, 7, 8, 0, tzinfo=UTC),
    ]
    for dt in instants:
        expected = f"{dt.astimezone(MT):%H:%M} MT"
        assert fmt_time(dt, MT) == expected, dt
        assert dt.astimezone(MT).utcoffset() == dt.astimezone(pkg).utcoffset(), dt
        # an aware non-UTC input gives the same wall time
        assert fmt_time(dt.astimezone(ZoneInfo("America/New_York")), MT) == expected
    assert fmt_time(datetime(2026, 1, 15, 14, 35, 5, tzinfo=UTC), MT) == "07:35 MT"  # UTC-7
    assert fmt_time(T0, MT) == "07:35 MT"  # UTC-6: not a fixed offset
    r = MessageRenderer(BASE, MT)
    jan = datetime(2026, 1, 15, 14, 35, 5, tzinfo=UTC)
    text = r.proposal(proposal(created_at=jan, expires_at=jan + timedelta(minutes=5)), ()).text
    assert "Expires 07:40 MT (5 min to decide)" in text


def test_alert_text_does_not_leak_a_bot_token_or_refresh_token() -> None:
    """AlertView data comes from event_log rows, and an exception string there can quote the Telegram URL
    (bot token in the path) or the Questrade token URL (refresh token in the query). The alert goes to a
    chat on Stephen's phone and to Telegram's servers: the full token must not be in the text. The
    logging redactor (trader.logging_setup.redact_text) already knows these shapes."""
    r = MessageRenderer(BASE, MT)
    tg_url = f"https://api.telegram.org/bot{FAKE_BOT_TOKEN}/sendMessage"
    qt_url = f"https://login.questrade.com/oauth2/token?grant_type=refresh_token&refresh_token={FAKE_REFRESH}"
    views = {
        "job_failure": alert(
            kind="job_failure", source="job.nightly", data={"error": f"NetworkError: POST {tg_url} timed out"}
        ),
        "alert_message": alert(message=f"telegram send failed for token {FAKE_BOT_TOKEN}"),
        "escalation_data": alert(kind="escalation", data={"url": tg_url, "auth": f"Bearer {FAKE_ACCESS}"}),
        "token_failure": alert(kind="token_failure", data={"error": f"HTTP 400 from {qt_url}"}),
        "kill_switch": alert(
            kind="kill_switch", message=f"pause via {tg_url}", data={"switch": "manual_pause"}
        ),
    }
    leaks = []
    for name, v in views.items():
        text = r.alert(v).text
        for secret in (FAKE_BOT_SECRET, FAKE_REFRESH, FAKE_ACCESS):
            if secret in text:
                leaks.append((name, secret))
    assert not leaks, f"token leaked into Telegram alert text: {leaks}"


# ===========================================================================================================
# P3-T2: logging
# ===========================================================================================================

TOUCHED_LOGGERS = ("", "httpx", "httpcore", "telegram", "sqlalchemy")


@pytest.fixture
def clean_logging() -> Iterator[None]:
    root = logging.getLogger()
    handlers = list(root.handlers)
    levels = {name: logging.getLogger(name).level for name in TOUCHED_LOGGERS}
    structlog.reset_defaults()
    yield
    for handler in list(root.handlers):
        if handler not in handlers:
            root.removeHandler(handler)
    for name, level in levels.items():
        logging.getLogger(name).setLevel(level)
    structlog.reset_defaults()


def json_lines(out: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in out.splitlines() if line.strip()]


@pytest.mark.usefixtures("clean_logging")
def test_questrade_token_json_and_token_kwargs_are_redacted(capsys: pytest.CaptureFixture[str]) -> None:
    """Questrade's token endpoint answers with JSON ({"access_token": ..., "refresh_token": ...}). An error
    that quotes the body, or a caller that logs the token as a field, must not write the token to stdout
    (Docker logs). key=value and URL query forms are covered by T2 already; the JSON form and a field named
    after the secret are not."""
    configure_logging("cron")
    body = json.dumps(
        {
            "access_token": FAKE_ACCESS,
            "refresh_token": FAKE_REFRESH,
            "api_server": "https://api01.iq.questrade.com/",
        }
    )
    log = structlog.get_logger("trader.adapters.questrade.auth")
    log.error("token exchange returned an unexpected body", body=body)
    logging.getLogger("trader.adapters.questrade.auth").error("refresh failed: %s", body)
    log.info("token seeded", refresh_token=FAKE_REFRESH, access_token=FAKE_ACCESS)
    out = capsys.readouterr().out
    lines = json_lines(out)
    assert len(lines) == 3
    leaks = [
        (i, s) for i, line in enumerate(out.splitlines()) for s in (FAKE_REFRESH, FAKE_ACCESS) if s in line
    ]
    assert not leaks, f"Questrade token written to the log (line index, secret): {leaks}"
    assert "api01.iq.questrade.com" in out  # non-secret fields survive


@pytest.mark.usefixtures("clean_logging")
def test_secrets_in_tracebacks_and_args_are_redacted_for_stdlib_and_structlog(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Secrets inside exception tracebacks (a chained cause too), in %-args of stdlib records and in
    structlog positional args, from loggers that are NOT quieted (the trader's own), are all masked."""
    configure_logging("worker")
    tg_url = f"https://api.telegram.org/bot{FAKE_BOT_TOKEN}/getUpdates"
    qt_url = f"https://login.questrade.com/oauth2/token?grant_type=refresh_token&refresh_token={FAKE_REFRESH}"
    std = logging.getLogger("trader.adapters.telegram.api")
    slog = structlog.get_logger("trader.worker")
    try:
        try:
            raise ConnectionError(f"Authorization: Bearer {FAKE_ACCESS} rejected")
        except ConnectionError as cause:
            raise RuntimeError(f"HTTP 409 for {tg_url}") from cause
    except RuntimeError:
        std.exception("poll failed")
        slog.exception("poll failed")
    try:
        raise ValueError(f"400 Bad Request for url '{qt_url}'")
    except ValueError:
        std.error("refresh failed", exc_info=True)
        slog.error("refresh failed", exc_info=True)
    std.warning("retrying %s with token %s", tg_url, FAKE_BOT_TOKEN)
    std.warning("headers %r", {"Authorization": f"Bearer {FAKE_ACCESS}"})
    slog.warning("retrying %s", tg_url)
    out = capsys.readouterr().out
    lines = json_lines(out)
    assert len(lines) == 7
    for secret in (FAKE_BOT_SECRET, FAKE_REFRESH, FAKE_ACCESS):
        assert secret not in out, secret
    assert all("Traceback" in line["exception"] for line in lines[:4])
    assert "ConnectionError" in lines[0]["exception"]  # the chained cause is still shown, masked


@pytest.mark.usefixtures("clean_logging")
def test_redaction_keeps_ordinary_numbers_times_and_ids(capsys: pytest.CaptureFixture[str]) -> None:
    """Not over-eager: prices, quantities, times, dates, ids, ratios and fields that merely contain the
    word token (token_age_hours, token_ok) are logged unchanged."""
    configure_logging("worker")
    keep = [
        "fill AAA 33 @ 21.5608 at 2026-10-06T13:35:05Z",
        "orb_open at 09:35:05 ET, 07:35 MT",
        "order 1234567890 account 12345678 symbol_id=8049",
        "ratio 12345:67890 and 20261006:y",
        "pnl -$10.82 (+2.17R) drawdown 0.36%",
        "token_age_hours=3.2 token_ok=True tokens=5",
        "callback j:20261006:y:AbCdEfGh:0123456789abcdef",
        "event:orb_open session 2026-10-06",
    ]
    log = structlog.get_logger("trader.test")
    for i, text in enumerate(keep):
        log.info(text, idx=i, detail=text, price=Decimal("21.5608").__str__(), qty=33)
        logging.getLogger("trader.stdlib").info("%s", text)
    lines = json_lines(capsys.readouterr().out)
    assert len(lines) == 2 * len(keep)
    for i, text in enumerate(keep):
        s, std = lines[2 * i], lines[2 * i + 1]
        assert s["event"] == text and s["detail"] == text, s
        assert s["price"] == "21.5608" and s["qty"] == 33
        assert std["event"] == text, std
        assert "[REDACTED]" not in json.dumps(s) + json.dumps(std)


@pytest.mark.usefixtures("clean_logging")
def test_repeated_configuration_is_idempotent_and_library_loggers_stay_quiet(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """configure_logging called many times (every CLI command and the worker call it; a command may call
    another) keeps exactly one handler and one line per message; sqlalchemy, httpx, httpcore and telegram
    children stay quiet below WARNING even with level=DEBUG; their warnings still come through, masked."""
    root = logging.getLogger()
    before = len(root.handlers)
    for proc, lvl in (("worker", "DEBUG"), ("worker", "DEBUG"), ("cron", "INFO"), ("api", "WARNING")):
        configure_logging(proc, level=lvl)
    assert len(root.handlers) == before + 1
    quiet = (
        "sqlalchemy.engine.Engine",
        "sqlalchemy.pool",
        "httpx",
        "httpcore.http11",
        "httpcore.connection",
        "telegram",
        "telegram.ext.Updater",
        "telegram.Bot",
    )
    for name in quiet:
        lg = logging.getLogger(name)
        assert not lg.isEnabledFor(logging.INFO), name
        lg.info("GET https://api.telegram.org/bot%s/getUpdates", FAKE_BOT_TOKEN)
        lg.debug("SELECT secret FROM api_credentials")
    assert capsys.readouterr().out == ""
    logging.getLogger("telegram.ext.Updater").warning(
        "Conflict at https://api.telegram.org/bot%s/x", FAKE_BOT_TOKEN
    )
    structlog.get_logger("trader.debug").debug("debug is on for the worker")
    lines = json_lines(capsys.readouterr().out)
    assert [line["event"].split(" at ")[0] for line in lines] == ["Conflict", "debug is on for the worker"]
    assert FAKE_BOT_SECRET not in json.dumps(lines)
    assert {line["process"] for line in lines} == {"worker"}
