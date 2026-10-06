"""The wheel's panel for the generic Strategies tab (task plan §3.5): a summary, three tables (positions,
the owner's tickers, candidates) and the owner's actions. Pure: it draws what the store and the context
hold. Money cells are text (a Decimal's digits), dates are ISO text.

The web offers an action on the rows that list it, and panel-wide the actions no row lists (`add_ticker`,
`clear_pause`). A toggle starts from the row's cell of the same key (`thesis_broken`)."""

from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

from trader.option_strategies.base import (
    KeyValue,
    PanelAction,
    PanelColumn,
    PanelRow,
    PanelTable,
    StrategyPanel,
    Tone,
)
from trader.option_strategies.wheel.store import APPROVED, CANDIDATE, EventRow, PositionRow, TickerRow
from trader.options.types import OptionAccountState, StructureView

AUTO = "AUTO"  # no override: the security type the facts give
SECURITY_TYPES = (AUTO, "STOCK", "BROAD_INDEX_ETF", "SECTOR_ETF", "LEVERAGED_OR_INVERSE_ETF")

ACTIONS = (
    PanelAction("add_ticker", "Add a ticker", "text"),
    PanelAction("approve", "Approve: why would you own it?", "text"),
    PanelAction("reject", "Reject", "button", confirm=True),
    PanelAction("set_reason", "Ownership reason", "text"),
    PanelAction("thesis_broken", "Thesis broken", "toggle"),
    PanelAction("security_type", "Security type", "choice", choices=SECURITY_TYPES),
    PanelAction("ack_caution", "Acknowledge cautions", "button"),
    PanelAction("rescreen", "Screen again", "button"),
    PanelAction("clear_pause", "Allow new positions again", "button", confirm=True),
)

_TEST_TONE: Mapping[str, Tone] = {"PASS": "ok", "CAUTION": "warn", "FAIL": "bad"}


def _text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _screen_detail(shot: dict[str, Any] | None) -> tuple[KeyValue, ...]:
    """The ten tests of the last screen, then its flags and what the owner still has to acknowledge."""
    if not shot:
        return (KeyValue("Screen", "not screened yet"),)
    rows = [
        KeyValue(f"{t['number']}. {t['name']}", f"{t['status']}: {t['reason']}", _TEST_TONE.get(t["status"]))
        for t in shot.get("tests", [])
    ]
    if shot.get("stale"):
        rows.append(KeyValue("Quotes", "market closed: verify live", "warn"))
    if shot.get("flags"):
        rows.append(KeyValue("Flags", ", ".join(shot["flags"])))
    if shot.get("disqualifiers"):
        rows.append(KeyValue("Disqualified", ", ".join(shot["disqualifiers"]), "bad"))
    if shot.get("unacknowledged"):
        rows.append(KeyValue("To acknowledge", ", ".join(shot["unacknowledged"]), "warn"))
    return tuple(rows)


def _option_cells(structures: Sequence[StructureView], structure_id: int | None) -> dict[str, Any]:
    for s in structures:
        if s.id != structure_id:
            continue
        for p in s.positions:
            if p.qty != 0 and p.contract is not None:
                return {"strike": str(p.contract.strike), "expiry": p.contract.expiry.isoformat()}
    return {"strike": None, "expiry": None}


def _position_row(pos: PositionRow, structures: Sequence[StructureView], last: EventRow | None) -> PanelRow:
    option_id = pos.call_structure_id if pos.state == "CALL_OPEN" else pos.put_structure_id
    option = _option_cells(structures, option_id) if pos.state != "SHARES_HELD" else {}
    fresh = {True: "yes", False: "no", None: "not answered"}[pos.fresh_cash_answer]
    return PanelRow(
        id=f"pos:{pos.id}",
        cells={
            "ticker": pos.ticker,
            "state": pos.state,
            "strike": option.get("strike"),
            "expiry": option.get("expiry"),
            "contracts": pos.contracts,
            "net_cost": _text(pos.net_cost),
            "roll_count": pos.roll_count,
            "next_action": last.action if last is not None else None,
            "why": last.reason if last is not None else "not evaluated yet",
        },
        detail=(
            KeyValue("Put premium per share", str(pos.total_put_premium)),
            KeyValue("Call premium per share", str(pos.total_call_premium)),
            KeyValue("Assignment strike", _text(pos.assignment_strike) or "none"),
            KeyValue("Fresh-cash answer", fresh),
            KeyValue("Drawdown review", pos.drawdown_review_text or "none"),
            KeyValue("Fees", str(pos.fees)),
            KeyValue("Opened", pos.opened_at.date().isoformat()),
        ),
    )


def _ticker_row(t: TickerRow) -> PanelRow:
    shot = t.last_screen or {}
    waiting = set(shot.get("unacknowledged", [])) - t.acknowledged
    if t.status == APPROVED:
        actions: tuple[str, ...] = ("set_reason", "thesis_broken", "security_type", "rescreen", "reject")
        if waiting:
            actions = ("ack_caution", *actions)
    else:
        actions = ("approve", "rescreen")
    return PanelRow(
        id=t.ticker,
        cells={
            "ticker": t.ticker,
            "status": t.status,
            "would_own": t.would_own,
            "ownership_reason": t.ownership_reason,
            "thesis_broken": t.thesis_broken,
            "security_type": t.security_type_override or AUTO,
            "verdict": t.last_verdict,
            "screened": None if t.last_screened_at is None else t.last_screened_at.date().isoformat(),
        },
        actions=actions,
        detail=(*_screen_detail(t.last_screen), KeyValue("Changed by", t.updated_by)),
    )


def _candidate_row(t: TickerRow) -> PanelRow:
    shot = t.last_screen or {}
    chosen = shot.get("chosen") or {}
    return PanelRow(
        id=t.ticker,
        cells={
            "ticker": t.ticker,
            "origin": t.origin,
            "verdict": t.last_verdict,
            "passes": shot.get("passes"),
            "expiry": shot.get("expiry"),
            "strike": chosen.get("strike"),
            "breakeven": shot.get("breakeven"),
        },
        actions=("approve", "reject", "rescreen", "security_type"),
        detail=_screen_detail(t.last_screen),
    )


def build(
    *,
    positions: Sequence[PositionRow],
    tickers: Sequence[TickerRow],
    last_actions: Mapping[int, EventRow],
    structures: Sequence[StructureView],
    account: OptionAccountState,
    paused: bool,
    benchmark: str,
    pending_prompts: int,
) -> StrategyPanel:
    by_state = {
        state: sum(1 for p in positions if p.state == state)
        for state in ("PUT_OPEN", "SHARES_HELD", "CALL_OPEN")
    }
    summary = (
        KeyValue("Puts open", str(by_state["PUT_OPEN"])),
        KeyValue("Shares held", str(by_state["SHARES_HELD"])),
        KeyValue("Calls open", str(by_state["CALL_OPEN"])),
        KeyValue("New positions", "paused" if paused else "allowed", "warn" if paused else "ok"),
        KeyValue("Cash", str(account.cash)),
        KeyValue("Reserved", str(account.reserved)),
        KeyValue("Benchmark", benchmark),
        KeyValue("Questions waiting", str(pending_prompts), "warn" if pending_prompts else None),
    )
    return StrategyPanel(
        summary=summary,
        tables=(
            PanelTable(
                key="positions",
                title="Positions",
                columns=(
                    PanelColumn("ticker", "Ticker"),
                    PanelColumn("state", "State", "badge"),
                    PanelColumn("strike", "Strike", "money"),
                    PanelColumn("expiry", "Expiry", "date"),
                    PanelColumn("contracts", "Contracts", "number"),
                    PanelColumn("net_cost", "Net cost", "money"),
                    PanelColumn("roll_count", "Rolls", "number"),
                    PanelColumn("next_action", "Next action", "badge"),
                    PanelColumn("why", "Why"),
                ),
                rows=tuple(_position_row(p, structures, last_actions.get(p.id)) for p in positions),
                empty_text="No wheel position is open",
            ),
            PanelTable(
                key="tickers",
                title="Tickers",
                columns=(
                    PanelColumn("ticker", "Ticker"),
                    PanelColumn("status", "Status", "badge"),
                    PanelColumn("would_own", "Would own", "bool"),
                    PanelColumn("ownership_reason", "Why own it"),
                    PanelColumn("thesis_broken", "Thesis broken", "bool"),
                    PanelColumn("security_type", "Security type"),
                    PanelColumn("verdict", "Last verdict", "badge"),
                    PanelColumn("screened", "Screened", "date"),
                ),
                rows=tuple(_ticker_row(t) for t in tickers if t.status != CANDIDATE),
                empty_text="No ticker is approved yet",
            ),
            PanelTable(
                key="candidates",
                title="Candidates",
                columns=(
                    PanelColumn("ticker", "Ticker"),
                    PanelColumn("origin", "From"),
                    PanelColumn("verdict", "Verdict", "badge"),
                    PanelColumn("passes", "Tests passed", "number"),
                    PanelColumn("expiry", "Expiry", "date"),
                    PanelColumn("strike", "Strike", "money"),
                    PanelColumn("breakeven", "Breakeven", "money"),
                ),
                rows=tuple(_candidate_row(t) for t in tickers if t.status == CANDIDATE),
                empty_text="No candidate is waiting",
            ),
        ),
        actions=ACTIONS,
    )
