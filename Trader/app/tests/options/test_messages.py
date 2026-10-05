"""OPTSIM-T10: every option message (`OptionMessages`): text, kind and dedupe key. Pure, no database."""

from decimal import Decimal
from typing import Any

import pytest

from tests.options.factories import EXPIRY, SESSION, T0, make_contract
from trader.notify.types import Button, Buttons, OutboundMessage
from trader.options.messages import OptionMessages
from trader.options.protocols import OptionRenderer, OptionSummaryView
from trader.options.types import (
    LegFill,
    LifecycleEvent,
    LifecycleKind,
    OptionAccountState,
    OptionContract,
    OptionFillEvent,
    OptPositionView,
    PromptChoice,
    PromptView,
    StructureKind,
    StructureView,
)

BASE = "https://trader.example/"
LINK = '<a href="https://trader.example/options">Options page</a>'
MESSAGES = OptionMessages(BASE)
PUT = make_contract(1, strike="12.00", right="put")
CALL_13 = make_contract(2, strike="13.00", right="call")
CALL_14 = make_contract(3, strike="14.00", right="call")
BUTTONS: Buttons = ((Button("Yes", "o:9:y:n:mac"), Button("No", "o:9:n:n:mac")),)


def structure(kind: StructureKind, source: str, *contracts: OptionContract) -> StructureView:
    positions = tuple(
        OptPositionView(
            id=i,
            structure_id=5,
            instrument="option",
            contract=c,
            underlying="F",
            qty=-1,
            avg_price=Decimal("0.45"),
            realized_pnl=Decimal(0),
        )
        for i, c in enumerate(contracts, start=1)
    )
    return StructureView(
        id=5,
        source=source,
        strategy_config_id=None,
        kind=kind,
        underlying="F",
        state="open",
        close_reason=None,
        frozen=False,
        qty=1,
        entry_net=Decimal("0.45"),
        reserved_cash=Decimal(0),
        take_profit_net=None,
        cover_structure_id=None,
        parent_structure_id=None,
        realized_pnl=Decimal(0),
        fees_total=Decimal(0),
        opened_at=T0,
        closed_at=None,
        positions=positions,
        meta={},
    )


def leg(leg_no: int, contract_id: int, side: Any, price: str, qty: int = 1) -> LegFill:
    return LegFill(leg_no, "option", contract_id, side, "open", qty, Decimal(price), Decimal("0.99"), {})


def fill(order_id: int, source: str, net: str, qty: int, fees: str, *legs: LegFill) -> OptionFillEvent:
    return OptionFillEvent(
        order_id=order_id,
        structure_id=5,
        source=source,
        strategy_config_id=None,
        intent="open",
        ts=T0,
        qty=qty,
        net_price=Decimal(net),
        fees=Decimal(fees),
        legs=legs,
        realized_pnl=None,
    )


def event(
    event_id: int,
    kind: LifecycleKind,
    contract: OptionContract,
    close: str,
    shares: int = 0,
    cash: str = "0",
) -> LifecycleEvent:
    return LifecycleEvent(
        id=event_id,
        structure_id=5,
        source="wheel",
        strategy_config_id=None,
        kind=kind,
        session_date=EXPIRY,
        ts=T0,
        contract=contract,
        qty=1,
        strike=contract.strike,
        underlying_close=Decimal(close),
        shares_delta=shares,
        cash_delta=Decimal(cash),
        new_structure_id=None,
    )


def prompt(**changes: Any) -> PromptView:
    values: dict[str, Any] = {
        "id": 9,
        "source": "wheel",
        "kind": "approve_ticker",
        "scope_key": "F",
        "dedupe_key": "wheel:approve:F",
        "title": "Approve F for the wheel?",
        "body": "F passed the screen.",
        "choices": (PromptChoice("y", "Yes"), PromptChoice("n", "No")),
        "needs_text": False,
        "default_choice": None,
        "data": {},
        "status": "pending",
        "asked_at": T0,
        "last_sent_at": None,
        "send_count": 0,
        "answered_at": None,
        "answer": None,
        "answer_text": None,
        "answered_via": None,
        "delivered_at": None,
    }
    return PromptView(**{**values, **changes})


ASSIGNED = event(3, "assigned", PUT, "11.50", shares=100, cash="-1200")
SUMMARY = OptionSummaryView(
    session_date=SESSION,
    account=OptionAccountState(
        cash=Decimal("4000"),
        reserved=Decimal("1200"),
        free_cash=Decimal("2800"),
        positions_value=Decimal("1123.45"),
        equity=Decimal("5123.45"),
        premium_collected=Decimal("45"),
        as_of=T0,
        marks_complete=True,
    ),
    day_change=Decimal("23.45"),
    fills=2,
    lifecycle=(ASSIGNED,),
    open_structures=3,
    pending_prompts=1,
    by_source={"wheel": Decimal("12"), "manual": Decimal("-3")},
)

CASES: dict[str, tuple[OutboundMessage, str, str, list[str]]] = {
    "fill single leg": (
        MESSAGES.fill(
            fill(7, "wheel", "0.45", 1, "0.99", leg(1, 1, "sell", "0.45")), structure("csp", "wheel", PUT)
        ),
        "fill",
        "opt:fill:7",
        [
            "<b>OPTION FILL</b> F cash-secured put (wheel)",
            "Sell to open 1 F 2026-11-20 P 12.00 @ 0.45",
            "Net credit 0.45 x 1 = $45.00, fees $0.99",
            "08:00 MT · order 7 · open",
            LINK,
        ],
    ),
    "fill spread": (
        MESSAGES.fill(
            fill(8, "manual", "-0.50", 2, "3.96", leg(1, 2, "buy", "0.80", 2), leg(2, 3, "sell", "0.30", 2)),
            structure("debit_spread", "manual", CALL_13, CALL_14),
        ),
        "fill",
        "opt:fill:8",
        [
            "<b>OPTION FILL</b> F debit spread (manual)",
            "Buy to open 2 F 2026-11-20 C 13.00 @ 0.80",
            "Sell to open 2 F 2026-11-20 C 14.00 @ 0.30",
            "Net debit 0.50 x 2 = $100.00, fees $3.96",
            "08:00 MT · order 8 · open",
            LINK,
        ],
    ),
    "assigned": (
        MESSAGES.lifecycle(ASSIGNED),
        "alert",
        "opt:life:3",
        [
            "<b>ASSIGNED</b> F 2026-11-20 P 12.00 x 1 (wheel)",
            "Close 11.50, strike 12.00",
            "Shares +100, cash -$1,200.00",
            "Session 2026-11-20 · 08:00 MT · structure 5",
            LINK,
        ],
    ),
    "called away": (
        MESSAGES.lifecycle(event(4, "called_away", CALL_14, "14.60", shares=-100, cash="1400")),
        "alert",
        "opt:life:4",
        [
            "<b>CALLED AWAY</b> F 2026-11-20 C 14.00 x 1 (wheel)",
            "Close 14.60, strike 14.00",
            "Shares -100, cash +$1,400.00",
            "Session 2026-11-20 · 08:00 MT · structure 5",
            LINK,
        ],
    ),
    "expired": (
        MESSAGES.lifecycle(event(5, "expired", PUT, "12.50")),
        "alert",
        "opt:life:5",
        [
            "<b>EXPIRED</b> F 2026-11-20 P 12.00 x 1 (wheel)",
            "Close 12.50, strike 12.00",
            "Session 2026-11-20 · 08:00 MT · structure 5",
            LINK,
        ],
    ),
    "alert": (
        MESSAGES.alert("wheel", "STRIKE_TOUCHED", "F touched 12.00", "wheel:F:touch"),
        "alert",
        "opt:alert:wheel:F:touch",
        ["<b>OPTIONS ALERT</b> STRIKE TOUCHED (wheel)", "F touched 12.00"],
    ),
    "prompt with buttons": (
        MESSAGES.prompt(prompt(), BUTTONS),
        "alert",
        "opt:prompt:9:1",
        ["<b>QUESTION</b> Approve F for the wheel? (wheel)", "F passed the screen."],
    ),
    "summary": (
        MESSAGES.summary(SUMMARY),
        "daily_summary",
        "opt:summary:2026-10-06",
        [
            "<b>Options account value $5,123.45</b>",
            "Tue 2026-10-06, day change +$23.45",
            "Cash $4,000.00, reserved $1,200.00, free $2,800.00",
            "Positions $1,123.45, premium collected $45.00",
            "Fills 2, open structures 3",
            "Realized today: manual -$3.00, wheel +$12.00",
            "ASSIGNED F 2026-11-20 P 12.00 x 1 (wheel)",
            "Questions waiting for you: 1",
            LINK,
        ],
    ),
}


@pytest.mark.parametrize("name", list(CASES))
def test_messages(name: str) -> None:
    msg, kind, dedupe_key, lines = CASES[name]
    assert msg.text.split("\n") == lines
    assert (msg.kind, msg.dedupe_key) == (kind, dedupe_key)
    assert msg.buttons == (BUTTONS if name == "prompt with buttons" else ())


def test_dynamic_text_is_escaped() -> None:
    renderer: OptionRenderer = MESSAGES  # the class satisfies the protocol
    alert = renderer.alert("<src>", "K&R", "<b>bold</b> & more", "k")
    assert alert.text == "<b>OPTIONS ALERT</b> K&amp;R (&lt;src&gt;)\n&lt;b&gt;bold&lt;/b&gt; &amp; more"
    asked = renderer.prompt(
        prompt(
            title="Own <F>?",
            body="Why & how",
            needs_text=True,
            send_count=1,
            choices=(PromptChoice("a", "Approve <now>"), PromptChoice("r", "Reject")),
        ),
        (),
    )
    assert asked.text.split("\n") == [
        "<b>QUESTION (reminder 1)</b> Own &lt;F&gt;? (wheel)",
        "Why &amp; how",
        '<a href="https://trader.example/options?prompt=9">'
        "Approve &lt;now&gt;: answer on the Options page</a>",
    ]
    assert asked.dedupe_key == "opt:prompt:9:2"
