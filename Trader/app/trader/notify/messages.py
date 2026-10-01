"""Every Telegram message format (SPEC §4.4): pure functions of the notify views.

Messages are Telegram HTML: every dynamic string is masked (trader.logging_setup.redact_text: an error text
can quote a token URL) and then escaped here, so callers pass raw values. Each message is self-contained
(ticker, quantity, prices, stop, P&L, reason), because the web app is reachable only at home; a link to the
web app is added for use there. Times are shown in Mountain Time, money, prices and other numbers through
Decimal.
"""

import html
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from trader.logging_setup import REDACTED, is_secret_key, redact_text
from trader.market.clock import Clock, RealClock
from trader.notify.types import (
    AlertView,
    Buttons,
    DailySummaryView,
    DecisionsLineView,
    FillView,
    MessageKind,
    OutboundMessage,
    OverlayView,
    PnlView,
    PositionLine,
    PreopenView,
    ProposalView,
    QuoteBarsLineView,
    Renderer,
    RunToDateView,
    SoakLineView,
    StatusView,
    WeeklyReportView,
)

TELEGRAM_LIMIT = 4096
TRUNCATED_LINE = "… (truncated, see the web app)"
WEB_POINTER = "… (full text on the web)"  # where an over-long weekly commentary was cut
# The event KillSwitches.reset writes (`kill switch <switch> reset`): rendered as the reset confirmation.
RESET_MESSAGE = re.compile(r"kill switch (\S+) reset")
EMPTY_TEXT = "(empty)"
ERROR_CHARS = 300  # a job failure shows the first 300 characters of its error
TAIL_ERROR_CHARS = 500  # a closed proposal's or token failure's error text (then "…")
VIA_CHARS = 100
TAIL_LIMIT = 2048  # a final line kept whole by _fit is itself cut (tag-safe) beyond this

_CENT = Decimal("0.01")
_TENTH = Decimal("0.1")
_PRICE_Q = Decimal("0.0001")
_MOUNTAIN_ZONES = frozenset(
    {"America/Edmonton", "America/Denver", "America/Boise", "Canada/Mountain", "US/Mountain", "MST7MDT"}
)

PROPOSAL_HEADLINES: Mapping[str, str] = {
    "entry": "ENTRY",
    "stop": "PROTECTIVE STOP",
    "exit": "EXIT",
    "cancel": "CANCEL",
}
PHASE_TEXT: Mapping[str, str] = {
    "closed_day": "closed (no session today)",
    "pre_market": "pre-market",
    "open": "open",
    "after_close": "after the close",
}
# The SPEC §4.4 command table.
COMMANDS: tuple[tuple[str, str], ...] = (
    ("/status", "Session phase, next event, approval mode, kill switches, token health"),
    ("/positions", "Open positions: qty, entry, last, stop, unrealized P&L, unprotected time"),
    ("/pnl", "Today's realized and unrealized P&L, week to date, equity and drawdown"),
    ("/pending", "Re-send pending proposals with their Approve/Reject buttons"),
    ("/pause", "Block new entry proposals (asks for confirmation); exits and stops keep working"),
    ("/resume", "Lift a /pause (automatic kill switches are reset only in the web app)"),
    ("/help", "This list"),
)
PAUSE_CONFIRM_TEXT = "Pause new entries? Exits and stops keep working."
_APPROVED = frozenset({"approved", "submitted", "auto_approved"})
_PCT_SWITCHES = frozenset({"daily_loss_pct", "max_drawdown_pct"})
_KNOWN_SWITCHES = ("daily_loss_pct", "max_drawdown_pct", "expectancy", "manual_pause")


# --- formatting helpers ------------------------------------------------------------------------------------


def link(base_url: str, path: str) -> str:
    """The web-app URL for `path` under `base_url` (exactly one slash between them)."""
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def fmt_money(value: Decimal) -> str:
    """`$1,234.56`, negative `-$12.30`."""
    q = value.quantize(_CENT, ROUND_HALF_UP)
    sign = "-" if q < 0 else ""
    return f"{sign}${abs(q):,.2f}"


def fmt_price(value: Decimal) -> str:
    """4 decimal places, trimmed to at least 2."""
    s = f"{value.quantize(_PRICE_Q, ROUND_HALF_UP):f}"
    whole, _, frac = s.partition(".")
    frac = frac.rstrip("0")
    return f"{whole}.{frac.ljust(2, '0')}"


def fmt_pct(value: Decimal) -> str:
    """A fraction as a signed percentage: `Decimal("0.0123")` → `+1.23%`."""
    p = (value * 100).quantize(_CENT, ROUND_HALF_UP)
    return f"{'-' if p < 0 else '+'}{abs(p):.2f}%"


def fmt_time(dt: datetime, tz: ZoneInfo) -> str:
    """`09:35 MT`: the wall-clock time in `tz` (labelled MT for Mountain Time zones). `dt` must be aware: a
    naive datetime would be read as the host's local time."""
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(f"fmt_time needs a timezone-aware datetime, got {dt!r}")
    local = dt.astimezone(tz)
    label = "MT" if tz.key in _MOUNTAIN_ZONES else (local.tzname() or tz.key)
    return f"{local:%H:%M} {label}"


def fmt_duration(seconds: float) -> str:
    """`3m 20s`, `45s`, `1h 2m 5s` (negative counts as zero)."""
    total = max(0, round(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _e(value: object) -> str:
    """A dynamic string for the message: secrets masked first, then HTML-escaped."""
    return html.escape(redact_text(str(value)), quote=False)


def _clip(value: object, limit: int) -> str:
    """The masked text cut to `limit` characters plus `…` (masked before cutting, so a cut can never leave
    a partial token that the patterns no longer recognise)."""
    text = redact_text(str(value))
    return text if len(text) <= limit else text[:limit] + "…"


def _fmt_number(value: Decimal) -> str:
    """A number that is not money or a percentage (a kill-switch expectancy, a float from jsonb): 4 decimal
    places trimmed to at least 2, never a float tail, an exponent or `-0.00`."""
    if not value.is_finite():
        return str(value)
    try:
        q = value.quantize(_PRICE_Q, ROUND_HALF_UP)
    except InvalidOperation:  # beyond the context precision
        return f"{value:f}"
    return fmt_price(abs(q) if q == 0 else q)


def _value_text(value: object) -> str:
    """An alert data value: floats and Decimals as numbers, anything else masked and escaped."""
    if isinstance(value, float | Decimal):
        d = _as_decimal(value)
        if d is not None:
            return _fmt_number(d)
    return _e(value)


def _pct_plain(value: Decimal) -> str:
    """A fraction as an unsigned percentage: `0.0036` → `0.36%`."""
    return f"{(value * 100).quantize(_CENT, ROUND_HALF_UP):.2f}%"


def _signed_money(value: Decimal) -> str:
    text = fmt_money(value)
    return text if text.startswith("-") else f"+{text}"


def _fmt_r(value: Decimal) -> str:
    q = value.quantize(_CENT, ROUND_HALF_UP)
    return f"{'-' if q < 0 else '+'}{abs(q):.2f}R"


# The metrics formatters (P5-T10): the daily run-to-date line and the weekly report use these, so the same
# trader.reports.metrics value always reads the same on the phone.
fmt_signed_money = _signed_money  # `+$23.40`, `-$2.50`
fmt_signed_r = _fmt_r  # `+0.18R`


def fmt_rate(value: Decimal) -> str:
    """A ratio (win rate, adherence) as a percentage with one decimal: `0.4167` → `41.7%`."""
    return f"{(value * 100).quantize(_TENTH, ROUND_HALF_UP):.1f}%"


def _whole_pct(part: int, whole: int) -> str:
    return f"{(Decimal(100 * part) / whole).quantize(Decimal(1), ROUND_HALF_UP)}%"


def quote_bars_line(v: QuoteBarsLineView) -> str:
    """QUOTEBAR: `Opening bars from quotes: 100 compared, prices exact 97%, volume within ±10% 81%` (plus
    `, 2 decisions would differ` when any would), or `... 540 built, none compared (shadow check missing)`."""
    head = "Opening bars from quotes: "
    if v.compared <= 0:
        return f"{head}{v.quote_bars} built, none compared (shadow check missing)"
    text = (
        f"{head}{v.compared} compared, prices exact {_whole_pct(v.prices_exact, v.compared)}, "
        f"volume within ±10% {_whole_pct(v.volume_within, v.compared)}"
    )
    if v.decision_differs:
        text += f", {v.decision_differs} decision{'' if v.decision_differs == 1 else 's'} would differ"
    return text


def run_to_date_lines(v: RunToDateView) -> list[str]:
    """The daily summary's run-to-date line(s) (BR-60); None values are left out."""
    parts = [f"{v.trades} trade" if v.trades == 1 else f"{v.trades} trades"]
    if v.win_rate is not None:
        parts.append(f"win rate {fmt_rate(v.win_rate)}")
    if v.expectancy_r is not None:
        parts.append(f"expectancy {fmt_signed_r(v.expectancy_r)}")
    parts.append(f"P&amp;L {fmt_signed_money(v.total_pnl)}")
    lines = ["Run to date: " + ", ".join(parts)]
    if v.expectancy_trades < v.expectancy_min_trades:
        lines.append(f"Expectancy switch: {v.expectancy_trades} of {v.expectancy_min_trades} trades")
    return lines


def _pnl(pnl: Decimal, pnl_r: Decimal | None) -> str:
    return _signed_money(pnl) + (f" ({_fmt_r(pnl_r)})" if pnl_r is not None else "")


def _one_row(buttons: Buttons) -> Buttons:
    flat = tuple(b for row in buttons for b in row)
    return (flat,) if flat else ()


def _safe_cut(text: str) -> str:
    """Drop a trailing partial HTML entity or tag left by a hard cut."""
    amp = text.rfind("&")
    if amp != -1 and ";" not in text[amp:]:
        text = text[:amp]
    lt = text.rfind("<")
    if lt != -1 and ">" not in text[lt:]:
        text = text[:lt]
    return text


_TAG = re.compile(r"<(/?)([A-Za-z]+)[^<>]*>")


def _close_tags(text: str) -> str:
    """`text` with every tag it leaves open (<b>, <a>) closed, innermost first."""
    stack: list[str] = []
    for m in _TAG.finditer(text):
        name = m.group(2).lower()
        if not m.group(1):
            stack.append(name)
        elif stack and stack[-1] == name:
            stack.pop()
    return text + "".join(f"</{name}>" for name in reversed(stack))


def _hard_cut(text: str, room: int, mark: str = "") -> str:
    """At most `room` characters of the HTML `text`: no partial entity or tag, `mark` added where it was
    cut, and every tag left open closed again."""
    if len(text) <= room:
        return text
    cut = room
    while True:
        head = _close_tags(_safe_cut(text[: max(0, cut)]) + mark)
        if len(head) <= room or cut <= 0:
            return head
        cut -= len(head) - room


def _fit(text: str, tail: str = "") -> str:
    """Never empty, at most TELEGRAM_LIMIT characters: an over-long text is cut at the last line break that
    fits and ends with the truncation line. `tail` (a final line) is kept, itself cut at TAIL_LIMIT."""
    if not text.strip():
        text = EMPTY_TEXT
    tail = _hard_cut(tail, TAIL_LIMIT, "…")
    suffix = f"\n{tail}" if tail else ""
    if len(text) + len(suffix) <= TELEGRAM_LIMIT:
        return text + suffix
    room = TELEGRAM_LIMIT - len(suffix) - len(TRUNCATED_LINE) - 1
    cut = text.rfind("\n", 0, room + 1)
    head = _hard_cut(text[:cut] if cut > 0 else text, room)
    return f"{head.rstrip()}\n{TRUNCATED_LINE}{suffix}"


def _cut_words(text: str, room: int) -> str:
    """At most `room` characters of the escaped plain `text` (entities, no tags): cut at the last word
    boundary that fits, then WEB_POINTER. An entity has no whitespace, so a word cut never splits one; a
    single word longer than the room is cut hard, entity-safe."""
    if len(text) <= room:
        return text
    keep = max(0, room - len(WEB_POINTER) - 1)  # one space before the pointer
    head = _safe_cut(text[:keep])
    if len(head) < len(text) and not text[len(head)].isspace():
        space = max(head.rfind(" "), head.rfind("\n"))
        if space > 0:
            head = head[:space]
    head = head.rstrip()
    return f"{head} {WEB_POINTER}" if head else WEB_POINTER


def _position_line(p: PositionLine) -> str:
    last = fmt_price(p.last) if p.last is not None else "n/a"
    stop = fmt_price(p.stop) if p.stop is not None else "none"
    if not p.stop_working:
        stop += " (no stop order)"
    pnl = _signed_money(p.unrealized_pnl) if p.unrealized_pnl is not None else "n/a"
    line = f"{_e(p.ticker)} {p.qty} @ {fmt_price(p.entry)}, last {last}, stop {stop}, P&amp;L {pnl}"
    if p.unprotected_seconds > 0:
        line += f", unprotected {fmt_duration(p.unprotected_seconds)}"
    return line


def _as_decimal(value: object) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


class MessageRenderer:
    """The Renderer (trader.notify.types.Renderer): links under `base_url`, times in `tz`. `clock` (default:
    the real clock) only decides whether an alert's time also needs its date."""

    def __init__(self, base_url: str, tz: ZoneInfo, *, clock: Clock | None = None) -> None:
        self.base_url = base_url
        self.tz = tz
        self.clock: Clock = clock or RealClock()

    # --- building blocks ---------------------------------------------------------------------------------

    def _time(self, dt: datetime) -> str:
        return fmt_time(dt, self.tz)

    def _alert_time(self, dt: datetime) -> str:
        """`07:35 MT` today; `2026-10-05 07:35 MT` for an alert from another day (a relay catching up)."""
        text = fmt_time(dt, self.tz)
        day = dt.astimezone(self.tz).date()
        return text if day == self.clock.now().astimezone(self.tz).date() else f"{day.isoformat()} {text}"

    def _link(self, path: str, label: str) -> str:
        url = html.escape(link(self.base_url, path), quote=True)
        return f'<a href="{url}">{_e(label)}</a>'

    @staticmethod
    def _msg(
        kind: MessageKind, lines: Iterable[str], buttons: Buttons = (), *, silent: bool = False
    ) -> OutboundMessage:
        return OutboundMessage(kind=kind, text=_fit("\n".join(lines)), buttons=buttons, silent=silent)

    # --- proposals ---------------------------------------------------------------------------------------

    @staticmethod
    def _is_auto(v: ProposalView) -> bool:
        return v.status == "auto_approved" or v.decided_via == "auto"

    def _proposal_lines(self, v: ProposalView, *, closed: bool) -> list[str]:
        auto = self._is_auto(v)
        headline = PROPOSAL_HEADLINES.get(v.kind, v.kind.upper())
        if auto:
            headline = f"AUTO {headline}"
        lines = [f"<b>{_e(headline)}: {_e(v.side.upper())} {v.qty} {_e(v.ticker)}</b>"]
        order = v.order_type.replace("_", " ")
        if v.order_type in ("stop", "stop_limit") and v.stop is not None:
            order = f"stop {fmt_price(v.stop)}"
            if v.order_type == "stop_limit" and v.limit is not None:
                order += f" limit {fmt_price(v.limit)}"
        elif v.order_type == "limit" and v.limit is not None:
            order = f"limit {fmt_price(v.limit)}"
        lines.append(f"Order: {_e(v.side)} {_e(order)}")
        if v.stop_loss is not None:
            lines.append(f"Stop loss: {fmt_price(v.stop_loss)}")
        if v.risk_usd is not None:
            lines.append(f"Risk: {fmt_money(v.risk_usd)}")
        lines.append(f"Strategy: {_e(v.strategy_key)}")
        if v.reason.strip():
            lines.append(f"Reason: {_e(v.reason)}")
        if not auto and not closed:
            minutes = max(0, round((v.expires_at - v.created_at).total_seconds() / 60))
            lines.append(f"Expires {self._time(v.expires_at)} ({minutes} min to decide)")
        lines.append(self._link(f"/dashboard?proposal={v.proposal_id}", f"Proposal #{v.proposal_id}"))
        return lines

    def proposal(self, v: ProposalView, buttons: Buttons) -> OutboundMessage:
        auto = self._is_auto(v)
        return self._msg(
            "proposal",
            self._proposal_lines(v, closed=False),
            () if auto else _one_row(buttons),
            silent=auto,
        )

    def proposal_closed(self, v: ProposalView, final_status: str, via: str | None) -> str:
        # Every part of the final line is masked and capped, so the edited message always fits TELEGRAM_LIMIT.
        error = _clip(v.error, TAIL_ERROR_CHARS) if v.error else ""
        if final_status in _APPROVED:
            if final_status == "auto_approved" or via == "auto" or self._is_auto(v):
                final = "Approved (auto)"
            else:
                final = f"Approved via {_clip(via, VIA_CHARS)}" if via else "Approved"
        elif final_status == "rejected":
            # An approved entry that the kill switches or a pause blocked is recorded as rejected with the
            # reason in `error`, so the tapper sees why instead of a plain "Rejected".
            final = f"Rejected: {error}" if error else "Rejected"
        elif final_status == "expired":
            final = "Expired"
        elif final_status == "failed":
            final = f"Failed: {error or 'the order was refused'}"
        else:
            final = f"Already decided ({_clip(final_status, VIA_CHARS)})"
        # When Stephen decided (P4-T6), on Telegram or the web. An automatic approval or an auto-submit on
        # expiry is nobody's decision, so "Approved (auto)" keeps its P3 wording.
        if v.decided_at is not None and not final.startswith("Approved (auto)"):
            if final_status in _APPROVED or final_status == "rejected":
                final += f" at {self._time(v.decided_at)}"
        return _fit("\n".join(self._proposal_lines(v, closed=True)), f"<b>{_e(final)}</b>")

    # --- fills and the overlay ---------------------------------------------------------------------------

    def fill(self, v: FillView) -> OutboundMessage:
        verb = "BOUGHT" if v.side == "buy" else "SOLD"
        main = f"{verb} {v.qty} {_e(v.ticker)} @ {fmt_price(v.price)}"
        kind: MessageKind
        if v.purpose == "entry":
            kind, title = "fill", "ENTRY FILLED"
            if v.stop_loss is not None:
                main += f", stop {fmt_price(v.stop_loss)}"
        elif v.purpose == "stop":
            kind, title = "stop_hit", "STOPPED OUT"
        elif v.reason == "flatten_close":
            kind, title = "flatten", "FLATTENED (end of day)"
        else:
            kind, title = "fill", "EXIT FILLED"
            if v.reason:
                main += f" ({_e(v.reason)})"
        lines = [f"<b>{title}</b>", main]
        if v.pnl is not None:
            lines.append(f"P&amp;L {_pnl(v.pnl, v.pnl_r)}")
        lines.append(f"Filled at {self._time(v.ts)}")
        if v.position_id is not None:
            lines.append(self._link(f"/trades?position={v.position_id}", f"Position #{v.position_id}"))
        else:
            lines.append(self._link("/trades", "Trades"))
        return self._msg(kind, lines)

    def overlay(self, v: OverlayView) -> OutboundMessage:
        lines = [f"<b>SPY OVERLAY: {_e(v.decision.upper())}</b> at {self._time(v.ts)}"]
        if v.spy_return is None and v.prior_close is None and v.price is None:
            lines.append("SPY data unavailable")
        else:
            parts = []
            if v.spy_return is not None:
                parts.append(f"SPY {fmt_pct(v.spy_return)} from the prior close")
            if v.prior_close is not None:
                parts.append(f"prior close {fmt_price(v.prior_close)}")
            if v.price is not None:
                parts.append(f"now {fmt_price(v.price)}")
            lines.append(", ".join(parts))
        ids = ", ".join(f"#{i}" for i in v.position_ids) or "none"
        lines.append(f"Positions affected: {ids}")
        if v.note.strip():
            lines.append(_e(v.note))
        lines.append(self._link("/dashboard", "Dashboard"))
        return self._msg("overlay", lines)

    # --- alerts ------------------------------------------------------------------------------------------

    def alert(self, v: AlertView) -> OutboundMessage:
        # Every dynamic string goes through _e / _clip (masked, then escaped): the message and data come from
        # event_log rows, where an exception text can quote a token URL.
        at = self._alert_time(v.ts)
        message = [_e(v.message)] if v.message.strip() else []
        if v.kind == "kill_switch":
            lines = self._kill_switch_lines(v, at)
        elif v.kind == "job_failure":
            job = v.source.removeprefix("job.")
            lines = [f"<b>JOB FAILED: {_e(job)}</b> at {at}", *message]
            error = v.data.get("error")
            if error:
                lines.append(f"Error: {_e(_clip(error, ERROR_CHARS))}")
        elif v.kind == "token_failure":
            error = _clip(v.data.get("error") or v.message or "unknown error", TAIL_ERROR_CHARS).rstrip(". ")
            lines = [
                f"<b>TOKEN FAILURE</b> at {at}",
                f"Questrade token refresh failed: {_e(error)}. Paste a new token in Settings.",
            ]
        elif v.kind == "escalation":
            lines = [f"<b>ESCALATION</b> at {at}", *message, *self._data_lines(v.data)]
        else:
            lines = [
                f"<b>ALERT ({_e(v.level.upper())})</b> {_e(v.source)} at {at}",
                *message,
                *self._data_lines(v.data),
            ]
        lines.append(self._link("/system", "System page"))
        return self._msg(v.kind, lines)

    @staticmethod
    def _data_lines(data: Mapping[str, Any]) -> list[str]:
        """`key: value` per data item; None values are left out, secret-named keys masked entirely."""
        return [
            f"{_e(k)}: {REDACTED if is_secret_key(k) else _value_text(val)}"
            for k, val in data.items()
            if val is not None
        ]

    @staticmethod
    def _switch_name(v: AlertView) -> str:
        switch = v.data.get("switch")
        if switch:
            return str(switch)
        if v.message.startswith("manual pause"):
            return "manual_pause"
        return next((s for s in _KNOWN_SWITCHES if s in v.message), "unknown")

    def _kill_switch_lines(self, v: AlertView, at: str) -> list[str]:
        reset = RESET_MESSAGE.fullmatch(v.message.strip())
        if reset is not None:  # an automatic switch reset in the web app (P5-T10)
            switch = str(v.data.get("switch") or reset.group(1))
            lines = [f"<b>KILL SWITCH RESET: {_e(switch)}</b> at {at}"]
            reason = v.data.get("reason")
            if reason:
                lines.append(f"Reason: {_e(_clip(reason, TAIL_ERROR_CHARS))}")
            lines.append("Entries allowed again unless another switch is tripped.")
            return lines
        switch = self._switch_name(v)
        lines = [f"<b>KILL SWITCH: {_e(switch)}</b> at {at}"]
        if v.message.strip():
            lines.append(_e(v.message))
        value, threshold = v.data.get("value"), v.data.get("threshold")
        if value is not None and threshold is not None:
            dv, dt = _as_decimal(value), _as_decimal(threshold)
            if switch in _PCT_SWITCHES and dv is not None and dt is not None:
                lines.append(f"Value {_pct_plain(dv)} vs threshold {_pct_plain(dt)}")
            else:
                # Not a percentage (expectancy), or a float decoded from jsonb: through Decimal(str(v)).
                shown_value = _fmt_number(dv) if dv is not None else _e(value)
                shown_threshold = _fmt_number(dt) if dt is not None else _e(threshold)
                lines.append(f"Value {shown_value} vs threshold {shown_threshold}")
        if switch == "manual_pause":
            lines.append("New entries blocked until /resume. Exits and stops keep working.")
        elif switch == "daily_loss_pct":
            lines.append("Entries blocked until the next session (resets automatically).")
        else:
            lines.append("Entries blocked; reset in the web app.")
        return lines

    # --- end of day --------------------------------------------------------------------------------------

    def daily_summary(self, v: DailySummaryView, buttons: Buttons) -> OutboundMessage:
        d = v.session_date.isoformat()
        lines = [f"<b>Daily summary {d}</b>"]
        if v.trades:
            lines.append(f"Trades ({len(v.trades)}):")
            lines += [
                f"{_e(t.ticker)} {t.qty} @ {fmt_price(t.entry)} → {fmt_price(t.exit)}: "
                f"{_pnl(t.pnl, t.pnl_r)} {_e(t.exit_reason)}"
                for t in v.trades
            ]
        else:
            lines.append("No trades.")
        lines += [
            f"Realized P&amp;L: {_signed_money(v.realized_pnl)}",
            f"Fees: {fmt_money(v.fees)}",
            f"Equity: {fmt_money(v.equity)}",
            f"Drawdown: {_pct_plain(v.drawdown_pct)}",
        ]
        if v.run_to_date is not None:
            lines += run_to_date_lines(v.run_to_date)
        if v.open_positions:
            lines.append(f"<b>⚠️ STILL OPEN: {len(v.open_positions)} position(s) after the close</b>")
            lines += [_position_line(p) for p in v.open_positions]
        decisions = f"Decisions: {v.decisions}"
        if v.avg_decision_seconds is not None:
            decisions += f", average {fmt_duration(v.avg_decision_seconds)}"
        lines.append(decisions)
        if v.decision_log is not None:
            lines.append(self._decision_log_line(v.decision_log))
        lines.append(f"Unprotected time: {fmt_duration(v.unprotected_seconds)}")
        lines.append(f"Kill switches: {_e(', '.join(v.blocking_switches)) or 'none'}")
        if v.quote_bars is not None:
            lines.append(quote_bars_line(v.quote_bars))
        archive = ", ".join(f"{_e(k)}: {n}" for k, n in sorted(v.archive.items()))
        lines.append(f"Candles archived: {archive or 'none'}")
        if v.journal_answer is not None:  # AUTOJOURNAL: already recorded, nothing to ask
            followed, via = v.journal_answer
            how = "auto mode" if via == "auto" else (via or "recorded")
            lines.append(f"Rules followed: {'Yes' if followed else 'No'} ({_e(how)})")
        else:
            lines.append("<b>Rules followed?</b>")
        lines.append(self._link(f"/journal?date={d}", f"Journal {d}"))
        return self._msg("daily_summary", lines, buttons)

    def _decision_log_line(self, v: DecisionsLineView) -> str:
        """P6-T11: `Decisions: 812 scanned · 14 ranked · 1 passed · 1 proposal (1 manual) · top rejects
        rvol_below_min 790, catalyst_low_quality 6 · details` (the link to the day's Reports view)."""
        parts = [f"{v.scanned} scanned", f"{v.ranked} ranked", f"{v.passed} passed"]
        proposals = f"{v.proposals} proposal{'' if v.proposals == 1 else 's'}"
        how = [f"{n} {k}" for k, n in (("manual", v.manual), ("auto", v.auto)) if n]
        parts.append(proposals + (f" ({', '.join(how)})" if how else ""))
        if v.fills:
            parts.append(f"{v.fills} fill{'' if v.fills == 1 else 's'}")
        if v.trades:
            parts.append(f"{v.trades} trade{'' if v.trades == 1 else 's'}")
        if v.top_rejects:
            parts.append("top rejects " + ", ".join(f"{_e(rule)} {n}" for rule, n in v.top_rejects[:3]))
        parts.append(self._link(v.link, "details"))
        return "Decisions: " + " · ".join(parts)

    def journal_answered(self, session_date: date, rules_followed: bool) -> OutboundMessage:
        answer = "Yes" if rules_followed else "No"
        return self._msg("reply", [f"Journal {session_date.isoformat()}: rules followed = {answer}"])

    # --- morning -----------------------------------------------------------------------------------------

    def premarket_brief(self, session_date: date, brief: str) -> OutboundMessage:
        body = _e(brief) if brief.strip() else "(no brief)"
        return self._msg("premarket_brief", [f"<b>Pre-market brief {session_date.isoformat()}</b>", body])

    def preopen(self, v: PreopenView) -> OutboundMessage:
        lines = [
            f"<b>Pre-open {v.session_date.isoformat()}</b>",
            f"Approval mode: {_e(v.approval_mode)}",
        ]
        for c in v.checks:
            label = "OK" if c.ok else ("ERROR" if c.level == "error" else "WARNING")
            lines.append(f"{label} {_e(c.name)}: {_e(c.detail)}")
        if not v.checks:
            lines.append("No checks ran.")
        lines.append(self._link("/system", "System page"))
        return self._msg("preopen", lines)

    # --- status and commands -----------------------------------------------------------------------------

    def _status_lines(self, v: StatusView) -> list[str]:
        lines = [
            f"Now {self._time(v.now)}, session {v.session_date.isoformat()}: "
            f"{_e(PHASE_TEXT.get(v.phase, v.phase))}"
        ]
        if v.next_event_key is not None and v.next_event_at is not None:
            lines.append(f"Next event: {_e(v.next_event_key)} at {self._time(v.next_event_at)}")
        else:
            lines.append("Next event: none today")
        lines.append(f"Approval mode: {_e(v.approval_mode)}")
        if v.blocking_switches:
            lines.append(f"Kill switches: BLOCKED by {_e(', '.join(v.blocking_switches))}")
        else:
            lines.append("Kill switches: none")
        age = f", refreshed {v.token_age_hours:.1f} h ago" if v.token_age_hours is not None else ""
        if v.token_ok:
            lines.append(f"Questrade token: OK{age}")
        else:
            lines.append(f"Questrade token: NOT OK ({_e(v.token_error or 'unknown error')}{age})")
        if v.heartbeat_age_seconds is not None:
            lines.append(f"Worker heartbeat: {fmt_duration(v.heartbeat_age_seconds)} ago")
        else:
            lines.append("Worker heartbeat: no heartbeat")
        lines.append(f"Open positions: {len(v.positions)}")
        lines += [_position_line(p) for p in v.positions]
        lines.append(f"Pending proposals: {v.pending_count}")
        lines.append(self._link("/dashboard", "Dashboard"))
        return lines

    def checkin(self, v: StatusView, at_label: str) -> OutboundMessage:
        return self._msg("checkin", [f"<b>Check-in {_e(at_label)}</b>", *self._status_lines(v)])

    def status(self, v: StatusView) -> OutboundMessage:
        return self._msg("reply", ["<b>Status</b>", *self._status_lines(v)])

    def positions(self, lines: Sequence[PositionLine], now: datetime) -> OutboundMessage:
        out = [f"<b>Positions</b> at {self._time(now)}"]
        out += [_position_line(p) for p in lines] or ["No open positions."]
        out.append(self._link("/trades", "Trades"))
        return self._msg("reply", out)

    def pnl(self, v: PnlView) -> OutboundMessage:
        return self._msg(
            "reply",
            [
                f"<b>P&amp;L {v.session_date.isoformat()}</b>",
                f"Realized today: {_signed_money(v.realized_today)}",
                f"Unrealized: {_signed_money(v.unrealized)}",
                f"Week to date: {_signed_money(v.week_to_date)}",
                f"Equity: {fmt_money(v.equity)} (peak {fmt_money(v.peak_equity)})",
                f"Drawdown: {_pct_plain(v.drawdown_pct)}",
                self._link("/trades", "Trades"),
            ],
        )

    def pause_confirm(self, buttons: Buttons) -> OutboundMessage:
        return self._msg("reply", [PAUSE_CONFIRM_TEXT], buttons)

    def help(self) -> OutboundMessage:
        return self._msg("reply", ["<b>Commands</b>", *(f"{c} - {_e(d)}" for c, d in COMMANDS)])

    def reply(self, text: str) -> OutboundMessage:
        return self._msg("reply", [_e(text)])

    def weekly_link(self, week_ending: date) -> OutboundMessage:
        d = week_ending.isoformat()
        return self._msg(
            "weekly_report",
            [f"<b>Weekly report</b> for the week ending {d}", self._link(f"/reports?week={d}", "Report")],
        )

    def weekly_report(self, v: WeeklyReportView) -> OutboundMessage:
        """The self-contained Saturday weekly report (BR-61): headline numbers, the commentary (untrusted
        Claude text, masked and escaped) or the note saying why there is none, and the link. An over-long
        commentary is cut at a word boundary with WEB_POINTER, so the headline and the link always fit."""
        d = v.week_ending.isoformat()
        trades = f"Trades: {v.trades}"
        if v.win_rate is not None:
            trades += f" ({v.wins} wins, win rate {fmt_rate(v.win_rate)})"
        elif v.trades:
            trades += f" ({v.wins} wins)"
        head = [f"<b>Weekly report</b> {v.week_start.isoformat()} to {d}", trades]
        if v.expectancy_r is not None:
            head.append(f"Expectancy: {fmt_signed_r(v.expectancy_r)}")
        head.append(f"P&amp;L: {fmt_signed_money(v.total_pnl)}")
        if v.max_drawdown_pct is not None:
            head.append(f"Max drawdown: {_pct_plain(v.max_drawdown_pct)}")
        if v.adherence_pct is not None:
            head.append(f"Rules followed: {fmt_rate(v.adherence_pct)} of answered days")
        tail = self._link(f"/reports?week={d}", f"Weekly report {d}")
        top = "\n".join(head) + "\n"  # the blank line before the commentary
        if v.commentary is not None and v.commentary.strip():
            room = TELEGRAM_LIMIT - len(top) - len(tail) - 2  # two line breaks around the body
            body = _cut_words(_e(v.commentary.strip()), room)
        elif v.commentary_note:
            body = f"<i>{_e(v.commentary_note)}</i>"
        else:
            body = ""
        lines = [top, body, tail] if body else [top, tail]
        return self._msg("weekly_report", lines)

    # --- the soak / ops line (P6-T2) ---------------------------------------------------------------------

    def soak_line(self, view: SoakLineView) -> OutboundMessage:
        """One line per session: the verdict, the 9:35 scan, the run of clean days and the earliest finish
        (dev "Soak"; prod "Ops" without target and finish), the time in MT, then one line per earlier day
        whose verdict changed. Silent unless the day is not clean. Dedupe `soak:<date>[:final]`."""
        prefix = "Ops" if view.env == "prod" else "Soak"
        head = f"<b>{prefix} {soak_day_label(view.session_date)}</b>"
        if view.final:
            head += " (final)"
        if view.verdict == "clean":
            verdict = "clean ✅"
        elif view.verdict == "pending":
            open_ = [
                "weekly report due Sat" if name == "weekly" else f"{_e(name)} pending" for name in view.failed
            ]
            verdict = "pending ⏳" + (f" ({', '.join(open_)})" if open_ else "")
        else:
            verdict = "NOT clean ❌" + (f" {_e('; '.join(view.failed))}" if view.failed else "")
            if view.catch_up_until is not None:  # not final: retries exhausted before the deadline
                verdict += f" (catch-up possible until {self._time(view.catch_up_until)})"
        parts = [f"{head}: {verdict}"]
        if view.orb_open_seconds is not None:
            parts.append(f"9:35 scan {round(view.orb_open_seconds)} s")
        elif view.orb_open == "off":  # orb_sip disabled: the scan is not expected
            parts.append("9:35 scan off")
        elif not any(f.startswith("event:orb_open") for f in view.failed):
            parts.append(f"9:35 scan {_e(view.orb_open)}")  # e.g. "pending" before 09:37:05 ET
        if view.env != "prod":
            n = view.consecutive_clean
            if n:
                parts.append(f"{n} clean day{'' if n == 1 else 's'} in a row (target {view.target})")
            else:
                parts.append(f"count 0 (target {view.target})")
            if view.earliest_finish is not None:
                parts.append(f"earliest finish {soak_day_label(view.earliest_finish)}")
            else:
                parts.append("target reached")
        parts.append(self._time(self.clock.now()))
        lines = [" · ".join(parts)]
        lines += [f"{soak_day_label(d)} is now {_e(text)}" for d, text in view.changed]
        key = f"soak:{view.session_date.isoformat()}" + (":final" if view.final else "")
        msg = self._msg("soak", lines, silent=view.verdict != "not_clean")
        return OutboundMessage(kind=msg.kind, text=msg.text, dedupe_key=key, silent=msg.silent)


def soak_day_label(d: date) -> str:
    """`Mon 28 Sep`: weekday, day and month (the soak line's dates)."""
    return f"{d:%a} {d.day} {d:%b}"


if TYPE_CHECKING:  # mypy verifies that MessageRenderer satisfies the Renderer protocol

    def _is_renderer(r: MessageRenderer) -> Renderer:
        return r
