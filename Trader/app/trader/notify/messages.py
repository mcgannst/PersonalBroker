"""Every Telegram message format (SPEC §4.4): pure functions of the notify views.

P3-T1 stub: the contracts are final, P3-T4 implements them.
"""

from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from trader.notify.types import (
    AlertView,
    Buttons,
    DailySummaryView,
    FillView,
    OutboundMessage,
    OverlayView,
    PnlView,
    PositionLine,
    PreopenView,
    ProposalView,
    StatusView,
)

TELEGRAM_LIMIT = 4096


def link(base_url: str, path: str) -> str:
    raise NotImplementedError("P3-T4")


def fmt_money(value: Decimal) -> str:
    """`$1,234.56`, negative `-$12.30`."""
    raise NotImplementedError("P3-T4")


def fmt_price(value: Decimal) -> str:
    """4 decimal places, trimmed to at least 2."""
    raise NotImplementedError("P3-T4")


def fmt_pct(value: Decimal) -> str:
    """`+1.23%`."""
    raise NotImplementedError("P3-T4")


def fmt_time(dt: datetime, tz: ZoneInfo) -> str:
    """`09:35 MT`."""
    raise NotImplementedError("P3-T4")


def fmt_duration(seconds: float) -> str:
    """`3m 20s`."""
    raise NotImplementedError("P3-T4")


class MessageRenderer:
    """The Renderer (trader.notify.types.Renderer): links under `base_url`, times in `tz`."""

    def __init__(self, base_url: str, tz: ZoneInfo) -> None:
        self.base_url = base_url
        self.tz = tz

    def proposal(self, v: ProposalView, buttons: Buttons) -> OutboundMessage:
        raise NotImplementedError("P3-T4")

    def proposal_closed(self, v: ProposalView, final_status: str, via: str | None) -> str:
        raise NotImplementedError("P3-T4")

    def fill(self, v: FillView) -> OutboundMessage:
        raise NotImplementedError("P3-T4")

    def overlay(self, v: OverlayView) -> OutboundMessage:
        raise NotImplementedError("P3-T4")

    def alert(self, v: AlertView) -> OutboundMessage:
        raise NotImplementedError("P3-T4")

    def daily_summary(self, v: DailySummaryView, buttons: Buttons) -> OutboundMessage:
        raise NotImplementedError("P3-T4")

    def journal_answered(self, session_date: date, rules_followed: bool) -> OutboundMessage:
        raise NotImplementedError("P3-T4")

    def premarket_brief(self, session_date: date, brief: str) -> OutboundMessage:
        raise NotImplementedError("P3-T4")

    def preopen(self, v: PreopenView) -> OutboundMessage:
        raise NotImplementedError("P3-T4")

    def checkin(self, v: StatusView, at_label: str) -> OutboundMessage:
        raise NotImplementedError("P3-T4")

    def status(self, v: StatusView) -> OutboundMessage:
        raise NotImplementedError("P3-T4")

    def positions(self, lines: Sequence[PositionLine], now: datetime) -> OutboundMessage:
        raise NotImplementedError("P3-T4")

    def pnl(self, v: PnlView) -> OutboundMessage:
        raise NotImplementedError("P3-T4")

    def pause_confirm(self, buttons: Buttons) -> OutboundMessage:
        raise NotImplementedError("P3-T4")

    def help(self) -> OutboundMessage:
        raise NotImplementedError("P3-T4")

    def reply(self, text: str) -> OutboundMessage:
        raise NotImplementedError("P3-T4")

    def weekly_link(self, week_ending: date) -> OutboundMessage:
        raise NotImplementedError("P3-T4")
