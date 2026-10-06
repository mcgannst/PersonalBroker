"""OPTSIM T11: the wheel's panel and its owner actions, through the real host (which is how the API reaches
them). The environment is the one of `test_strategy`."""

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from tests.option_strategies.wheel.test_strategy import Env, make_env, put_open, state
from trader.option_strategies.base import PanelActionRequest, PanelRow, StrategyPanel
from trader.option_strategies.wheel.strategy import ACCOUNT_SCOPE

pytestmark = pytest.mark.db
D = Decimal


@pytest.fixture
def env(db_factory: sessionmaker[Session]) -> Env:
    return make_env(db_factory)


def rows(panel: StrategyPanel, table: str) -> dict[str, PanelRow]:
    return {row.id: row for row in next(t for t in panel.tables if t.key == table).rows}


async def act(env: Env, action: str, row_id: str | None = None, value: Any = None) -> tuple[bool, str]:
    result = await env.host.action("wheel", PanelActionRequest(action, row_id, value), "web:stephen")
    return result.ok, result.message


async def test_panel_tables(env: Env) -> None:
    pos = await put_open(env)
    env.add_stock("G")
    env.store.add_ticker(env.symbols["G"], "G", origin="screen", actor="strategy:wheel")
    env.go(date(2026, 10, 7))
    await env.daily()
    await env.host.sync_prompts()

    panel = await env.host.panel("wheel")
    assert {kv.label: kv.value for kv in panel.summary} == {
        "Puts open": "1",
        "Shares held": "0",
        "Calls open": "0",
        "New positions": "allowed",
        "Cash": "20149.01",  # 20,000 + 150 premium - 0.99 fee
        "Reserved": "0",
        "Benchmark": "SOFI",
        "Questions waiting": "1",
    }
    assert [t.key for t in panel.tables] == ["positions", "tickers", "candidates"]

    (position,) = rows(panel, "positions").values()
    assert (position.id, position.actions) == (f"pos:{pos.id}", ())
    assert position.cells == {
        "ticker": "F",
        "state": "PUT_OPEN",
        "strike": "50",
        "expiry": "2026-11-20",
        "contracts": 1,
        "net_cost": None,
        "roll_count": 0,
        "next_action": "HOLD",
        "why": "no rule applies",
    }

    ticker = rows(panel, "tickers")["F"]
    assert ticker.cells == {
        "ticker": "F",
        "status": "approved",
        "would_own": True,
        "ownership_reason": "a business I want to hold",
        "thesis_broken": False,
        "security_type": "AUTO",
        "verdict": "QUALIFIED",
        "screened": "2026-10-06",
    }
    assert ticker.actions == ("set_reason", "thesis_broken", "security_type", "rescreen", "reject")
    assert [kv.label for kv in ticker.detail[:10]] == [
        "1. ownership",
        "2. profitability",
        "3. balance_sheet",
        "4. liquidity",
        "5. volatility",
        "6. earnings",
        "7. cash",
        "8. size",
        "9. trend",
        "10. premium",
    ]
    assert {kv.tone for kv in ticker.detail[:10]} == {"ok"}

    candidate = rows(panel, "candidates")["G"]
    assert candidate.cells == {
        "ticker": "G",
        "origin": "screen",
        "verdict": "NEEDS_REVIEW",  # ownership is not answered yet
        "passes": 9,
        "expiry": "2026-11-20",
        "strike": "50",
        "breakeven": "48.50",
    }
    assert candidate.actions == ("approve", "reject", "rescreen", "security_type")
    assert candidate.detail[0].tone == "warn" and candidate.detail[-1].label == "To acknowledge"

    assert [(a.key, a.kind, a.confirm) for a in panel.actions] == [
        ("add_ticker", "text", False),
        ("approve", "text", False),
        ("reject", "button", True),
        ("set_reason", "text", False),
        ("thesis_broken", "toggle", False),
        ("security_type", "choice", False),
        ("ack_caution", "button", False),
        ("rescreen", "button", False),
        ("clear_pause", "button", True),
    ]
    assert env.errors() == []


async def test_panel_tables_and_actions(env: Env) -> None:
    """Each action changes the right row, with who and when; none of them submits an order."""
    env.add_stock("G")
    env.market.set_facts("F", eps_growth_yoy=None)  # a caution to acknowledge on F
    env.approve("F")
    later = env.clock.now() + timedelta(minutes=5)
    env.clock.set(later)

    assert await act(env, "add_ticker", value=" g ") == (True, "G added as a candidate")
    added = env.store.ticker("G")
    assert added is not None
    assert (added.status, added.origin, added.updated_by, added.last_verdict) == (
        "candidate",
        "manual",
        "owner:panel",
        "NEEDS_REVIEW",
    )

    assert await act(env, "approve", "G", "it prints cash") == (True, "G approved")
    assert await act(env, "set_reason", "F", "a new reason") == (True, "F: ownership reason saved")
    assert await act(env, "thesis_broken", "F", True) == (True, "F: thesis broken")
    assert await act(env, "security_type", "F", "SECTOR_ETF") == (True, "F: security type SECTOR_ETF")
    assert (await act(env, "rescreen", "F"))[0]
    panel = await env.host.panel("wheel")
    g, f_row = rows(panel, "tickers")["G"], rows(panel, "tickers")["F"]
    assert (g.cells["status"], g.cells["would_own"], g.cells["ownership_reason"]) == (
        "approved",
        True,
        "it prints cash",
    )
    assert rows(panel, "candidates") == {}
    assert (f_row.cells["ownership_reason"], f_row.cells["thesis_broken"], f_row.cells["security_type"]) == (
        "a new reason",
        True,
        "SECTOR_ETF",
    )
    assert f_row.cells["verdict"] == "QUALIFIED"  # as a sector ETF the profitability test does not apply
    changed = env.store.ticker("F")
    assert changed is not None and (changed.updated_by, changed.updated_at) == ("owner:panel", later)

    assert await act(env, "security_type", "F", "AUTO") == (True, "F: security type AUTO")
    assert (await act(env, "rescreen", "F")) == (True, "F: NEEDS_REVIEW")
    assert "ack_caution" in rows(await env.host.panel("wheel"), "tickers")["F"].actions
    assert await act(env, "ack_caution", "F") == (True, "F: acknowledged profitability")
    acknowledged = env.store.ticker("F")
    assert acknowledged is not None and acknowledged.acknowledged == frozenset({"profitability"})
    assert "ack_caution" not in rows(await env.host.panel("wheel"), "tickers")["F"].actions

    assert await act(env, "reject", "G") == (True, "G rejected")
    rejected = rows(await env.host.panel("wheel"), "tickers")["G"]
    assert (rejected.cells["status"], rejected.cells["would_own"]) == ("rejected", False)
    assert rejected.actions == ("approve", "rescreen")
    assert await act(env, "add_ticker", value="G") == (True, "G added as a candidate")  # a second look

    state(env).put(ACCOUNT_SCOPE, {"new_positions_paused": True})
    paused = {kv.label: kv for kv in (await env.host.panel("wheel")).summary}["New positions"]
    assert (paused.value, paused.tone) == ("paused", "warn")
    assert (await act(env, "clear_pause"))[0]
    account = state(env).get(ACCOUNT_SCOPE)
    assert account is not None and account["new_positions_paused"] is False

    assert env.broker.submitted == []
    assert env.errors() == []


@pytest.mark.parametrize(
    ("action", "row_id", "value", "message"),
    [
        ("approve", "F", "  ", "Write one sentence on why you would own it"),
        ("approve", None, "why", "Pick a ticker row for this action"),
        ("reject", "NOPE", None, "Pick a ticker row for this action"),
        ("add_ticker", None, "not a ticker!", "That is not a ticker"),
        ("add_ticker", None, "ZZZ", "ZZZ is unknown to the market data"),
        ("add_ticker", None, "F", "F is already on the list (approved)"),
        ("security_type", "F", "BOND", "Unknown security type"),
        ("ack_caution", "F", None, "F has no caution waiting"),
        ("explode", "F", None, "Unknown action"),
    ],
)
async def test_refused_actions_change_nothing(
    env: Env, action: str, row_id: str | None, value: Any, message: str
) -> None:
    env.approve("F")
    before = env.store.tickers()
    assert await act(env, action, row_id, value) == (False, message)
    assert env.store.tickers() == before
