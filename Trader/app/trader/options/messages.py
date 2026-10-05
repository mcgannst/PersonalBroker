"""Every Telegram message of the options simulation (OPTSIM task plan T10): pure functions of the option
value types, implementing `OptionRenderer`.

Messages are Telegram HTML: every dynamic string is masked and escaped here, so callers pass raw values.
Times are shown in Mountain Time. Only existing message kinds are used (`fill`, `alert`, `daily_summary`),
so the notifier, the `notifications` table and the web's message list need no change. Net prices are per
share, credit positive (task plan §3.1): a fill reads "Net credit 0.45" or "Net debit 1.20".
"""

import html
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from trader.logging_setup import redact_text
from trader.notify.messages import fmt_money, fmt_price, fmt_signed_money, fmt_time, link
from trader.notify.types import Buttons, OutboundMessage
from trader.options.prompts import link_choices, prompt_path
from trader.options.protocols import OptionSummaryView
from trader.options.types import (
    HUNDRED,
    LegFill,
    LifecycleEvent,
    OptionContract,
    OptionFillEvent,
    PromptView,
    StructureView,
    contract_label,
)

MOUNTAIN = ZoneInfo("America/Edmonton")
OPTIONS_PATH = "/options"
STRUCTURE_LABELS: Mapping[str, str] = {
    "csp": "cash-secured put",
    "covered_call": "covered call",
    "long_call": "long call",
    "long_put": "long put",
}
LIFECYCLE_HEADLINES: Mapping[str, str] = {
    "expired": "EXPIRED",
    "exercised": "EXERCISED",
    "assigned": "ASSIGNED",
    "called_away": "CALLED AWAY",
    "early_assignment": "EARLY ASSIGNMENT",
    "frozen": "FROZEN",
}


def _e(value: object) -> str:
    """A dynamic string for the message: secrets masked first, then HTML-escaped."""
    return html.escape(redact_text(str(value)), quote=False)


def _words(value: str) -> str:
    return value.replace("_", " ")


def _signed_int(value: int) -> str:
    return f"{value:+d}"


class OptionMessages:
    """`OptionRenderer`. `public_base_url` is the web app's address (PUBLIC_BASE_URL)."""

    def __init__(self, public_base_url: str) -> None:
        self.base_url = public_base_url

    def _link(self, path: str, text: str) -> str:
        return f'<a href="{html.escape(link(self.base_url, path), quote=True)}">{_e(text)}</a>'

    def _time(self, ts: datetime) -> str:
        return fmt_time(ts, MOUNTAIN)

    # --- fills ----------
    def fill(self, fill: OptionFillEvent, structure: StructureView) -> OutboundMessage:
        contracts = {p.contract.id: p.contract for p in structure.positions if p.contract is not None}
        kind = STRUCTURE_LABELS.get(structure.kind, _words(structure.kind))
        lines = [f"<b>OPTION FILL</b> {_e(structure.underlying)} {_e(kind)} ({_e(fill.source)})"]
        lines += [self._leg_line(leg, structure.underlying, contracts) for leg in fill.legs]
        cash = fill.net_price * HUNDRED * fill.qty
        word = "credit" if fill.net_price >= 0 else "debit"
        lines.append(
            f"Net {word} {fmt_price(abs(fill.net_price))} x {fill.qty} = {fmt_money(abs(cash))}, "
            f"fees {fmt_money(fill.fees)}"
        )
        if fill.realized_pnl is not None:
            lines.append(f"Realized P&amp;L {fmt_signed_money(fill.realized_pnl)}")
        lines.append(f"{self._time(fill.ts)} · order {fill.order_id} · {_e(fill.intent)}")
        lines.append(self._link(OPTIONS_PATH, "Options page"))
        return OutboundMessage("fill", "\n".join(lines), dedupe_key=f"opt:fill:{fill.order_id}")

    @staticmethod
    def _leg_line(leg: LegFill, underlying: str, contracts: Mapping[int, OptionContract]) -> str:
        if leg.instrument == "shares":
            what = f"{underlying} shares"
        elif leg.contract_id is not None and leg.contract_id in contracts:
            what = contract_label(contracts[leg.contract_id])
        else:
            what = f"{underlying} contract {leg.contract_id}"
        verb = f"{leg.side.capitalize()} to {leg.effect}"
        return f"{_e(verb)} {leg.qty} {_e(what)} @ {fmt_price(leg.price)}"

    # --- lifecycle ----------
    def lifecycle(self, event: LifecycleEvent) -> OutboundMessage:
        headline = LIFECYCLE_HEADLINES.get(event.kind, _words(event.kind).upper())
        what = contract_label(event.contract) if event.contract is not None else "shares"
        lines = [f"<b>{_e(headline)}</b> {_e(what)} x {event.qty} ({_e(event.source)})"]
        facts: list[str] = []
        if event.underlying_close is not None:
            facts.append(f"close {fmt_price(event.underlying_close)}")
        if event.strike is not None:
            facts.append(f"strike {fmt_price(event.strike)}")
        if facts:
            lines.append(", ".join(facts).capitalize())
        moved: list[str] = []
        if event.shares_delta:
            moved.append(f"shares {_signed_int(event.shares_delta)}")
        if event.cash_delta:
            moved.append(f"cash {fmt_signed_money(event.cash_delta)}")
        if moved:
            lines.append(", ".join(moved).capitalize())
        lines.append(
            f"Session {event.session_date.isoformat()} · {self._time(event.ts)} · "
            f"structure {event.structure_id}"
        )
        lines.append(self._link(OPTIONS_PATH, "Options page"))
        return OutboundMessage("alert", "\n".join(lines), dedupe_key=f"opt:life:{event.id}")

    # --- alerts ----------
    def alert(self, source: str, kind: str, message: str, dedupe_key: str) -> OutboundMessage:
        text = f"<b>OPTIONS ALERT</b> {_e(_words(kind))} ({_e(source)})\n{_e(message)}"
        return OutboundMessage("alert", text, dedupe_key=f"opt:alert:{dedupe_key}")

    # --- prompts ----------
    def prompt(self, prompt: PromptView, buttons: Buttons) -> OutboundMessage:
        """The question with its buttons. A choice that needs free text is a link to the Options page.
        The dedupe key counts the sends, so each daily reminder is its own message."""
        number = prompt.send_count + 1
        head = "QUESTION" if number == 1 else f"QUESTION (reminder {number - 1})"
        lines = [f"<b>{head}</b> {_e(prompt.title)} ({_e(prompt.source)})", _e(prompt.body)]
        path = prompt_path(prompt.id)
        lines += [self._link(path, f"{c.label}: answer on the Options page") for c in link_choices(prompt)]
        return OutboundMessage(
            "alert", "\n".join(lines), buttons=buttons, dedupe_key=f"opt:prompt:{prompt.id}:{number}"
        )

    # --- the daily summary ----------
    def summary(self, view: OptionSummaryView) -> OutboundMessage:
        """The first line is the account value."""
        acct = view.account
        lines = [f"<b>Options account value {fmt_money(acct.equity)}</b>"]
        day = f"{view.session_date:%a} {view.session_date.isoformat()}"
        change = "n/a" if view.day_change is None else fmt_signed_money(view.day_change)
        lines.append(f"{day}, day change {change}")
        lines.append(
            f"Cash {fmt_money(acct.cash)}, reserved {fmt_money(acct.reserved)}, "
            f"free {fmt_money(acct.free_cash)}"
        )
        lines.append(
            f"Positions {fmt_money(acct.positions_value)}, "
            f"premium collected {fmt_money(acct.premium_collected)}"
        )
        if not acct.marks_complete:
            lines.append("Some positions have no current quote: the account value is incomplete.")
        lines.append(f"Fills {view.fills}, open structures {view.open_structures}")
        if view.by_source:
            realized = ", ".join(
                f"{_e(source)} {fmt_signed_money(Decimal(amount))}"
                for source, amount in sorted(view.by_source.items())
            )
            lines.append(f"Realized today: {realized}")
        for event in view.lifecycle:
            what = contract_label(event.contract) if event.contract is not None else "shares"
            headline = LIFECYCLE_HEADLINES.get(event.kind, _words(event.kind).upper())
            lines.append(f"{_e(headline)} {_e(what)} x {event.qty} ({_e(event.source)})")
        if view.pending_prompts:
            lines.append(f"Questions waiting for you: {view.pending_prompts}")
        lines.append(self._link(OPTIONS_PATH, "Options page"))
        return OutboundMessage(
            "daily_summary", "\n".join(lines), dedupe_key=f"opt:summary:{view.session_date.isoformat()}"
        )
