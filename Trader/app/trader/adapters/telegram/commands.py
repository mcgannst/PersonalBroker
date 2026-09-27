"""Remote Telegram commands (SPEC §4.4): /status, /positions, /pnl, /pending, /pause (confirmed), /resume,
/help. BR-34, BR-30, BR-33.

The view builders are async (a P3-T1 refinement of the plan's signatures) because the last prices come from
the async `quotes` callable. Read-only commands only read; only the /pause confirmation and /resume change
state, through KillSwitches (which writes the audit rows). /resume never resets an automatic switch.
"""

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from trader.adapters.questrade.auth import TokenHealth
from trader.adapters.questrade.models import QtQuote
from trader.adapters.telegram.types import CallbackIssuer, ProposalMessenger
from trader.db import models as m
from trader.engine.killswitch import KillSwitches
from trader.engine.scheduler import DayPlan
from trader.market.calendar import SessionCalendar
from trader.market.clock import Clock
from trader.market.sessions import current_session
from trader.notify import views
from trader.notify.types import Button, OutboundMessage, PnlView, PositionLine, Renderer, StatusView
from trader.settings_store import RuntimeSettings

log = structlog.get_logger("telegram.commands")

TOKEN_MAX_AGE_HOURS = views.TOKEN_MAX_AGE_HOURS
ACTOR = "telegram"
UNKNOWN = "Unknown command. Try /help."
MANUAL_PAUSE = "manual_pause"


@dataclass(frozen=True)
class CommandDeps:
    factory: sessionmaker[Session]
    clock: Clock
    calendar: SessionCalendar
    settings: Callable[[], RuntimeSettings]
    killswitches: KillSwitches
    run_id: int
    chat_id: int
    plan: Callable[[date], DayPlan]
    fired: Callable[[date], set[str]]
    token_health: Callable[[], TokenHealth]
    quotes: Callable[[Sequence[int]], Awaitable[Mapping[int, QtQuote]]] | None
    messenger: ProposalMessenger
    issuer: CallbackIssuer
    render: Renderer


# --- view builders ----------------------------------------------------------------------------------------


def _pending_ids(deps: CommandDeps) -> list[int]:
    with deps.factory() as s:
        return list(
            s.execute(
                select(m.Proposal.id)
                .where(m.Proposal.run_id == deps.run_id, m.Proposal.status == "pending")
                .order_by(m.Proposal.created_at, m.Proposal.id)
            ).scalars()
        )


async def position_lines(deps: CommandDeps) -> tuple[PositionLine, ...]:
    """Open positions of the live run, oldest first, with the last price, stop and unprotected time
    (the shared builder in trader.notify.views, also used by the check-ins)."""
    return await views.position_lines(deps.factory, deps.clock, deps.run_id, deps.quotes)


async def status_view(deps: CommandDeps) -> StatusView:
    """The shared status view (trader.notify.views), so /status and the check-ins can't disagree."""
    return await views.status_view(
        factory=deps.factory,
        clock=deps.clock,
        calendar=deps.calendar,
        settings=deps.settings(),
        killswitches=deps.killswitches,
        run_id=deps.run_id,
        plan=deps.plan,
        fired=deps.fired,
        token_health=deps.token_health,
        quotes=deps.quotes,
    )


async def pnl_view(deps: CommandDeps, lines: Sequence[PositionLine] | None = None) -> PnlView:
    """Today's realized P&L, the unrealized P&L of open positions, the week to date (from the Monday of the
    current ET week) and equity/drawdown from the latest snapshot (or the starting cash when none): the
    shared `trader.notify.views.pnl_view` (the web dashboard uses it too). Positions without a quote are
    left out of `unrealized` (the /pnl handler then says it is partial). `lines` reuses already-built
    position lines (one quote fetch per command)."""
    now = deps.clock.now()
    if lines is None:
        lines = await position_lines(deps)
    return views.pnl_view(deps.factory, deps.calendar, now, deps.run_id, lines)


# --- command handler -------------------------------------------------------------------------------------


def _reset_hint(switch: str) -> str:
    """How an automatic switch clears (killswitch.py): daily_loss_pct by itself at the next session; the
    others only by a reset with a reason in the web app."""
    if switch == "daily_loss_pct":
        return "clears by itself at the next session"
    return "reset it in the web app (with a reason)"


def parse_command(text: str) -> str | None:
    """The command word, lower-cased and without an `@BotName` suffix; None for plain text."""
    words = text.strip().split()
    if not words or not words[0].startswith("/"):
        return None
    return words[0].split("@", 1)[0].lower()


class Commands:
    """Implements trader.adapters.telegram.types.CommandHandler."""

    def __init__(self, deps: CommandDeps) -> None:
        self.deps = deps

    async def handle(self, text: str) -> list[OutboundMessage]:
        d = self.deps
        command = parse_command(text)
        if command == "/status":
            return [d.render.status(await status_view(d))]
        if command == "/positions":
            return [d.render.positions(await position_lines(d), d.clock.now())]
        if command == "/pnl":
            lines = await position_lines(d)
            out = [d.render.pnl(await pnl_view(d, lines))]
            unpriced = [ln.ticker for ln in lines if ln.unrealized_pnl is None]
            if unpriced:  # the unrealized figure leaves these out: say so rather than show a false total
                out.append(d.render.reply(f"Unrealized P&L is partial: no quote for {', '.join(unpriced)}."))
            return out
        if command == "/pending":
            return await self._pending()
        if command == "/pause":
            return [self._pause()]
        if command == "/resume":
            return [d.render.reply(self._resume())]
        if command == "/help":
            return [d.render.help()]
        return [d.render.reply(UNKNOWN)]

    async def _pending(self) -> list[OutboundMessage]:
        ids = _pending_ids(self.deps)
        if not ids:
            return [self.deps.render.reply("No pending proposals.")]
        for proposal_id in ids:
            await self.deps.messenger.send_proposal(proposal_id, resend=True)
        return []

    def _session(self) -> date:
        return current_session(self.deps.calendar, self.deps.clock.now())

    def _is_paused(self) -> bool:
        active = self.deps.killswitches.active(self.deps.run_id, self._session())
        return any(a.switch == MANUAL_PAUSE for a in active)

    def _pause(self) -> OutboundMessage:
        d = self.deps
        if self._is_paused():
            return d.render.reply("Already paused.")
        _, data = d.issuer.issue(
            "pause",
            str(d.run_id),
            ("y", "n"),
            d.chat_id,
            d.settings().telegram_confirm_ttl_seconds,
        )
        buttons = ((Button("Yes, pause", data["y"]), Button("No", data["n"])),)
        return d.render.pause_confirm(buttons)

    def _resume(self) -> str:
        d = self.deps
        if not d.killswitches.resume(d.run_id, actor=ACTOR):
            return "Not paused."
        still = [a.switch for a in d.killswitches.active(d.run_id, self._session())]
        if still:
            notes = "\n".join(f"- {switch}: {_reset_hint(switch)}" for switch in still)
            return f"Manual pause lifted.\nStill blocked by:\n{notes}"
        return "Manual pause lifted."

    async def confirm_pause(self, action: str) -> str:
        """The answer to a pause confirmation tap: `y` pauses new entries, anything else cancels."""
        if action != "y":
            return "Cancelled."
        d = self.deps
        if not d.killswitches.pause(d.run_id, self._session(), actor=ACTOR):
            return "Already paused."
        return "Paused: new entries are blocked."
