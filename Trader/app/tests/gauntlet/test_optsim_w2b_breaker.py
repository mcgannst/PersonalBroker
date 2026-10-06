"""OPTSIM wave 2b breaker (T4 collateral, T6 lifecycle, T7 host, T9 wheel rules): cases the builders' tests do
not already prove. The four defects it first pinned as `xfail(strict=True)` are fixed (round OPTSIM-fix-B)
and their tests now run as plain tests.

The builders' own helpers are reused: the collateral book builders, the lifecycle `Env`, the host `Env` and
`Probe`, and the wheel rule builders."""

from collections.abc import Callable
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest

from tests.option_strategies.test_host import (  # noqa: F401  (`env` and `_reset` are fixtures)
    GO,
    Probe,
    _delivered_at,
    _lifecycle_row,
    _open,
    _reset,
    env,
)
from tests.option_strategies.test_host import Env as HostEnv
from tests.option_strategies.wheel import (
    CFG,
    ROLL_CFG,
    TODAY,
    account,
    eval_call,
    eval_put,
    expiry_info,
    facts,
    owner,
    row,
)
from tests.option_strategies.wheel import EXPIRY as WHEEL_EXPIRY
from tests.options.factories import EXPIRY
from tests.options.test_collateral import (
    BUY_WRITE,
    C15,
    C15_DEC,
    C16,
    C16_DEC,
    CALL_ON_SHARES,
    ENGINE,
    P13,
    P145,
    PUT_CREDIT_SPREAD,
    SHARES_100,
    book,
    buy,
    close,
    order,
    sell,
    shares,
    structure,
    working,
)
from tests.options.test_lifecycle import EARLY_DAY, LATER, covered_call, csp, summary
from tests.options.test_lifecycle import Env as LifeEnv
from trader.option_strategies.base import CancelOrder, CloseStructure, Reprice
from trader.option_strategies.wheel import evaluate, screen, select, stops
from trader.options.types import OptOrderView, OrderRequest, StructureView

D = Decimal


# --- T4: no naked short, ever (decision D5) -----------------------------------------------------------------


def test_two_calls_on_two_share_lots_keep_both_lots_committed() -> None:
    lots = [structure(1, "shares", [(None, 100, "15")]), structure(9, "shares", [(None, 100, "15")])]
    opened = ENGINE.evaluate(order([sell(C15, 1), sell(C16, 2)], "0.80"), book(structures=lots))
    if not opened.accepted:  # refusing an order that needs two lots is a valid fix too
        return
    assert sorted(p.cover_ref for p in opened.pairs if p.cover_ref is not None) == [1, 9]
    # the book after the fill: the decision (and the structure row) carries ONE cover_structure_id
    calls = structure(2, "custom", [(C15, -1, "0.45"), (C16, -1, "0.40")], cover=opened.cover_structure_id)
    other_lot = 9 if opened.cover_structure_id == 1 else 1

    sold = ENGINE.evaluate(
        close([shares("sell", effect="close")], other_lot, "14.90"), book(structures=[*lots, calls])
    )

    assert (sold.accepted, sold.reject_reason) == (False, "shares_committed")


def test_a_call_left_on_another_structures_shares_keeps_them_committed() -> None:
    spread = structure(4, "debit_spread", [(C15, 1, "0.60"), (C16, -1, "0.20")])
    left = ENGINE.evaluate(
        close([sell(C15, effect="close")], 4, "0.40"), book(structures=[SHARES_100, spread])
    )
    if not left.accepted:  # refusing the close is a valid fix too
        return
    assert left.cover_structure_id == 1
    # the book after that fill, as SimOptionBroker writes it: positions and reserve, cover link unchanged
    bare = structure(4, "debit_spread", [(C15, 0, "0.60"), (C16, -1, "0.20")])
    after = book(structures=[SHARES_100, bare])

    sold = ENGINE.evaluate(close([shares("sell", effect="close")], 1, "14.90"), after)
    second_call = ENGINE.evaluate(order([sell(C15_DEC)]), after)

    assert (sold.accepted, sold.reject_reason) == (False, "shares_committed")
    assert (second_call.accepted, second_call.reject_reason) == (False, "naked_short")


def test_closing_a_spreads_long_put_cannot_lift_the_underlying_over_the_cap() -> None:
    held = [PUT_CREDIT_SPREAD, structure(8, "csp", [(P13, -1, "0.30")], reserved="1300")]

    decision = ENGINE.evaluate(
        close([sell(P13, effect="close")], 5, "0.10"), book(cash="50000", structures=held)
    )

    assert (decision.exposure_after, decision.cap_limit) == (D("2750"), D("2500"))
    assert (decision.accepted, decision.reject_reason) == (False, "position_cap")


TWO_LONGS = structure(4, "custom", [(C15_DEC, 1, "0.60"), (C16_DEC, 1, "0.40")])
REFUSED: list[tuple[str, OrderRequest, list[StructureView], list[OptOrderView], set[str]]] = [
    (
        "roll_one_call_into_two",
        order([buy(C16, 1, "close"), sell(C16_DEC, 2, ratio=2)], "0.20", intent="roll", structure_id=2),
        [SHARES_100, CALL_ON_SHARES],
        [],
        {"naked_short"},
    ),
    (
        "shares_a_working_order_is_selling",
        order([sell(C15)]),
        [SHARES_100],
        [working([shares("sell", effect="close")], intent="close", structure_id=1)],
        {"naked_short"},
    ),
    (
        "buy_write_with_half_the_shares",
        order([shares("buy", 1, ratio=50), sell(C15, 2)], "-7.10"),
        [],
        [],
        {"naked_short"},
    ),
    (
        "long_call_of_another_structure",
        order([sell(C15)]),
        [structure(3, "long_call", [(C15_DEC, 1, "0.50")])],
        [],
        {"naked_short"},
    ),
    (
        "long_leg_a_working_order_is_closing",
        order([sell(C16_DEC, 1, "close"), sell(C15, 2)], "0.80", intent="roll", structure_id=4),
        [TWO_LONGS],
        [working([sell(C15_DEC, effect="close")], intent="close", structure_id=4)],
        {"naked_short"},
    ),
    (
        "one_long_for_two_short_legs",
        order([buy(C15_DEC, 1), sell(C15, 2), sell(C16, 3)], "0.30"),
        [],
        [],
        {"naked_short"},
    ),
    (
        "shares_of_another_underlying",
        order([sell(C15)]),
        [structure(1, "shares", [(None, 100, "15")], underlying="T")],
        [],
        {"naked_short"},
    ),
    (
        "half_the_shares_of_a_buy_write",
        close([shares("sell", effect="close", ratio=50)], 6, "14.90"),
        [BUY_WRITE],
        [],
        {"shares_committed"},
    ),
    (
        "shares_needed_by_a_call_and_a_working_call",
        close([shares("sell", effect="close")], 1, "14.90"),
        [structure(1, "shares", [(None, 200, "15")]), CALL_ON_SHARES],
        [working([sell(C15)])],
        {"shares_committed"},
    ),
    (
        "roll_that_sells_the_shares_and_writes_a_new_call",
        order(
            [shares("sell", 1, "close"), buy(C16, 2, "close"), sell(C16_DEC, 3)],
            "14.60",
            intent="roll",
            structure_id=6,
        ),
        [BUY_WRITE],
        [],
        {"naked_short", "shares_committed"},
    ),
    (
        "cap_counts_the_other_sources_put",  # one pool (feature plan §3.7)
        order([sell(P145)]),
        [structure(3, "csp", [(P13, -1, "0.30")], reserved="1300", source="wheel")],
        [],
        {"position_cap"},
    ),
]


@pytest.mark.parametrize(
    ("req", "structures", "orders", "reasons"), [c[1:] for c in REFUSED], ids=[c[0] for c in REFUSED]
)
def test_more_ways_in_are_refused(
    req: OrderRequest, structures: list[StructureView], orders: list[OptOrderView], reasons: set[str]
) -> None:
    decision = ENGINE.evaluate(req, book(cash="50000", structures=structures, orders=orders))

    assert not decision.accepted
    assert decision.reject_reason in reasons, decision.detail


# --- T6: lifecycle money ------------------------------------------------------------------------------------


async def test_two_calls_on_one_share_lot_are_both_called_away_once() -> None:
    life = LifeEnv()
    lot = life.structure("shares", (None, 200, "14.50"))
    first = life.structure("covered_call", (life.contract("15", "call"), -1, "0.30"), cover=lot)
    second = life.structure("covered_call", (life.contract("15.50", "call"), -1, "0.20"), cover=lot)
    life.set_close("16")

    events = await life.engine.run_expiry(EXPIRY)

    assert summary(events) == [("called_away", -100, D(1500)), ("called_away", -100, D(1550))]
    assert life.book.cash() == D(8050)
    for sid in (lot, first, second):
        view = life.book.structure(sid)
        assert (view.state, view.close_reason) == ("closed", "called_away")
        assert all(p.qty == 0 for p in view.positions)
    assert await life.engine.run_expiry(EXPIRY) == [] and life.book.cash() == D(8050)


@pytest.mark.parametrize("early_first", [False, True])
async def test_a_call_expiring_before_an_ex_dividend_day_is_settled_once(early_first: bool) -> None:
    life = LifeEnv()
    covered_call(life)
    life.market.set_facts("F", next_ex_dividend_date=date(2026, 11, 23), dividend_per_share=D("0.15"))
    life.set_close("16")

    kinds: list[str] = []
    if early_first:  # the early-assignment check leaves a contract that expires today to the expiry run
        assert await life.engine.run_early_assignment(EXPIRY) == []
    kinds += [e.kind for e in await life.engine.run_expiry(EXPIRY)]
    kinds += [e.kind for e in await life.engine.run_early_assignment(EXPIRY)]

    assert kinds == ["called_away"]
    assert life.book.cash() == D(6500) and len(life.book.lifecycle) == 1


@pytest.mark.parametrize(
    ("cash", "close_price", "expected", "cash_after", "share_lots"),
    [
        # the short 14.50 put alone in the money: 100 shares at the strike, the 150 reserve released
        ("5000", "14", [("expired", 0, D(0)), ("assigned", 100, D(-1450))], "3550", 1),
        # both legs in the money, with only the reserve as cash: bought at 14.50, sold at 13, no shares
        ("150", "12", [("assigned", 100, D(-1450)), ("exercised", -100, D(1300))], "0", 0),
    ],
    ids=["short_leg_only", "both_legs"],
)
async def test_put_credit_spread_at_expiry(
    cash: str, close_price: str, expected: list[Any], cash_after: str, share_lots: int
) -> None:
    life = LifeEnv(cash)
    legs = ((life.contract("14.50", "put"), -1, "0.60"), (life.contract("13", "put"), 1, "0.20"))
    sid = life.structure("credit_spread", *legs, reserved="150")
    life.set_close(close_price)

    events = await life.engine.run_expiry(EXPIRY)

    assert summary(events) == expected
    spread = life.book.structure(sid)
    assert (spread.state, spread.close_reason, spread.reserved_cash) == ("closed", "assigned", D(0))
    assert life.book.reserved() == 0 and life.book.cash() == D(cash_after)
    lots = life.book.structures()
    assert [(s.kind, s.positions[0].qty, s.positions[0].avg_price) for s in lots] == (
        [("shares", 100, D("14.50"))] * share_lots
    )
    assert await life.engine.run_expiry(EXPIRY) == [] and life.book.cash() == D(cash_after)


async def test_early_assignment_only_for_a_share_covered_call() -> None:
    """P5: a short call covered by a long leg, and a deep in-the-money short put, are never assigned early."""
    life = LifeEnv()
    short = life.contract("15", "call")
    life.structure("diagonal", (life.contract("14", "call", LATER), 1, "1.50"), (short, -1, "0.60"))
    csp(life, strike="20")
    life.market.set_quote(short.id, "1.00", "1.10")
    life.market.set_facts("F", next_ex_dividend_date=date(2026, 11, 3), dividend_per_share=D("0.15"))
    life.set_close("16", EARLY_DAY)

    assert await life.engine.run_early_assignment(EARLY_DAY) == []
    assert life.book.lifecycle == [] and life.book.cash() == D(5000)


# --- T7: the host -------------------------------------------------------------------------------------------


@pytest.mark.db
async def test_a_disabled_plugin_trades_nothing_from_any_hook(env: HostEnv) -> None:  # noqa: F811
    mine = await env.hold("probe")
    fill = env.broker.fills[-1]
    order_id = await env.working("probe")
    event = _lifecycle_row(env, "probe")
    seeded = len(env.broker.submitted)
    env.registry.update("probe", enabled=False, actor="test")
    Probe.intents["probe"] = [
        _open(env.put_low),
        CloseStructure(mine, "market", None, "exit"),
        CancelOrder(order_id, "stale"),
        Reprice(order_id, D("0.01")),
    ]

    await env.host.deliver_lifecycle(event)
    await env.host.deliver_fill(fill)
    fired = await env.host.fire("probe", GO)

    assert fired.status == "skipped"
    assert len(env.broker.submitted) == seeded and env.broker.cancels == [] and env.broker.reprices == []
    assert [h for _, h, _ in Probe.seen] == ["on_lifecycle", "on_fill"]  # its records stay true
    assert _delivered_at(env, event) is not None


@pytest.mark.db
async def test_rubbish_from_one_plugin_does_not_reach_the_others(env: HostEnv) -> None:  # noqa: F811
    mine, theirs = _lifecycle_row(env, "probe"), _lifecycle_row(env, "other")
    Probe.fail.add(("probe", "on_lifecycle"))
    await env.host.deliver_lifecycle(mine)
    await env.host.deliver_lifecycle(theirs)
    assert _delivered_at(env, mine) is None and _delivered_at(env, theirs) is not None
    assert ("other", "on_lifecycle", theirs) in Probe.seen

    Probe.fail.clear()
    Probe.intents["probe"] = [object(), _open(env.put)]  # type: ignore[list-item]
    outcome = await env.host.fire("probe", GO)
    assert outcome.status == "succeeded"
    assert outcome.detail == {"intents": 2, "submitted": 1, "rejected": 0, "dropped": 1}


# --- T9: the wheel rules against the owner's spec -----------------------------------------------------------


def test_a_missing_growth_figure_alone_does_not_force_an_exit() -> None:
    gap = facts(eps_growth_yoy=None, debt_to_equity="1.5")

    assert screen.balance_sheet(gap, screen.profitability(gap).status, CFG).status == "CAUTION"
    assert evaluate.business_check(gap, owner(), CFG).passes
    assert eval_put(eps_growth_yoy=None, debt_to_equity="1.5").kind == "HOLD"


def _put_row(**changes: Any) -> Any:
    return row(50, "0.30", **{"bid": "1.00", **changes})


def _call_pick(net_cost: str, delta: str, bid: str, ask: str | None = None) -> str:
    option = row(50, delta, bid, ask="auto" if ask is None else ask, right="call")
    return select.call(D(net_cost), [option], [expiry_info()], None, TODAY, CFG).action


LATER_PUT = row(50, "0.45", "3.40", expiry=TODAY + timedelta(days=48))
EDGES: list[tuple[str, Callable[[], Any], Any]] = [
    # WS §4.2 and §4.3: each threshold at its exact value, and one step past it
    ("t2_growth_zero", lambda: screen.profitability(facts(eps_growth_yoy=0)).status, "PASS"),
    (
        "t3_debt_at_limit",
        lambda: screen.balance_sheet(facts(debt_to_equity="1.0"), "PASS", CFG).status,
        "CAUTION",
    ),
    ("t3_debt_under", lambda: screen.balance_sheet(facts(debt_to_equity="0.99"), "PASS", CFG).status, "PASS"),
    ("t4_spread_10pct_oi_500", lambda: select.liquidity(_put_row(ask="1.10", oi=500), CFG)[0], "PASS"),
    ("t4_spread_15pct", lambda: select.liquidity(_put_row(ask="1.15"), CFG)[0], "CAUTION"),
    ("t4_spread_over_15pct", lambda: select.liquidity(_put_row(ask="1.16"), CFG)[0], "FAIL"),
    ("t4_oi_499", lambda: select.liquidity(_put_row(ask="1.05", oi=499), CFG)[0], "CAUTION"),
    ("t4_oi_100", lambda: select.liquidity(_put_row(ask="1.05", oi=100), CFG)[0], "CAUTION"),
    ("t4_oi_99", lambda: select.liquidity(_put_row(ask="1.05", oi=99), CFG)[0], "FAIL"),
    ("t5_iv_020", lambda: screen.volatility(_put_row(iv="0.20"), CFG).status, "PASS"),
    ("t5_iv_under_020", lambda: screen.volatility(_put_row(iv="0.1999"), CFG).status, "CAUTION"),
    ("t5_iv_050", lambda: screen.volatility(_put_row(iv="0.50"), CFG).status, "PASS"),
    ("t5_iv_060", lambda: screen.volatility(_put_row(iv="0.60"), CFG).status, "CAUTION"),
    ("t5_iv_over_060", lambda: screen.volatility(_put_row(iv="0.6001"), CFG).status, "FAIL"),
    ("t6_on_the_expiry", lambda: screen.earnings(WHEEL_EXPIRY, WHEEL_EXPIRY, TODAY).status, "FAIL"),
    ("t6_today", lambda: screen.earnings(TODAY, WHEEL_EXPIRY, TODAY).status, "FAIL"),
    (
        "t6_day_after_expiry",
        lambda: screen.earnings(WHEEL_EXPIRY + timedelta(days=1), WHEEL_EXPIRY, TODAY).status,
        "PASS",
    ),
    ("t7_fits_at_the_limit", lambda: screen.cash(D(50), account(10_000), CFG).status, "PASS"),
    ("t7_over_the_limit", lambda: screen.cash(D(50), account("9999.99"), CFG).status, "FAIL"),
    ("t7_nothing_left", lambda: screen.cash(D(50), account(10_000, collateral=5_000), CFG).status, "CAUTION"),
    ("t7_cent_short", lambda: screen.cash(D(50), account(10_000, collateral="5000.01"), CFG).status, "FAIL"),
    ("t7_five_pct_left", lambda: screen.cash(D(50), account(10_000, collateral=4_500), CFG).status, "PASS"),
    ("t8_at_pass", lambda: screen.size(facts(market_cap_usd=10_000_000_000), CFG).status, "PASS"),
    ("t8_at_caution", lambda: screen.size(facts(market_cap_usd=2_000_000_000), CFG).status, "CAUTION"),
    ("t8_under_caution", lambda: screen.size(facts(market_cap_usd=1_999_999_999), CFG).status, "FAIL"),
    ("t9_price_at_sma", lambda: screen.trend(facts(price=52), CFG).status, "PASS"),
    ("t9_sma_at_tolerance", lambda: screen.trend(facts(sma50="50.49", sma50_prior=51), CFG).status, "PASS"),
    ("t9_low_21_sessions_ago", lambda: screen.trend(facts(sessions_since_52w_low=21), CFG).status, "PASS"),
    ("t9_rsi_under_70", lambda: screen.trend(facts(rsi14="69.99"), CFG).status, "PASS"),
    ("t10_ror_at_pass", lambda: screen.premium(_put_row(bid="1.25"), CFG).status, "PASS"),
    ("t10_ror_at_fail_floor", lambda: screen.premium(_put_row(bid="0.25"), CFG).status, "CAUTION"),
    ("t10_ror_under_floor", lambda: screen.premium(_put_row(bid="0.24"), CFG).status, "FAIL"),
    # WS §8: the pin-risk band, and the first row that applies wins
    ("pin_band_exactly", lambda: eval_put(dte=0, price="50.50").kind, "PIN_RISK"),
    ("pin_band_just_outside_above", lambda: eval_put(dte=0, price="50.51").kind, "CLOSE_PUT_TIME"),
    ("pin_band_just_outside_below", lambda: eval_put(dte=0, price="49.49").kind, "TAKE_ASSIGNMENT"),
    ("row2_before_row6", lambda: eval_put(ask="0.60", dte=10, price=53).kind, "CLOSE_PUT_PROFIT"),
    (
        "row4_before_row5",
        lambda: eval_put(dte=0, price=50, ask="1.50", next_earnings_date=TODAY).kind,
        "REVIEW_BEFORE_EARNINGS",
    ),
    (
        "row5_before_the_roll",
        lambda: eval_put(dte=0, price="49.80", cfg=ROLL_CFG, next_puts=[LATER_PUT]).kind,
        "PIN_RISK",
    ),
    # WS §9.5: the profit target comes before the ex-dividend warning
    (
        "call_row2_before_row3",
        lambda: eval_call(ask="0.30", price=52, next_ex_dividend_date=TODAY + timedelta(days=5)).kind,
        "CLOSE_CALL_PROFIT",
    ),
    # WS §9.3: strike at the net cost, the delta bands and the return floors, all inclusive
    ("call_at_net_cost_delta_030_ror_1pct", lambda: _call_pick("50", "0.30", "0.50"), "SELL_CALL"),
    ("call_delta_020", lambda: _call_pick("50", "0.20", "0.50"), "SELL_CALL"),
    ("call_a_cent_under_net_cost", lambda: _call_pick("50.01", "0.30", "0.50"), "HOLD_UNCOVERED"),
    ("call_delta_over_030", lambda: _call_pick("50", "0.31", "0.50"), "HOLD_UNCOVERED"),
    ("call_ror_under_1pct", lambda: _call_pick("50", "0.30", "0.49"), "HOLD_UNCOVERED"),
    ("low_call_delta_010_ror_half_pct", lambda: _call_pick("50", "0.10", "0.25", "0.27"), "SELL_CALL"),
    ("low_call_delta_015", lambda: _call_pick("50", "0.15", "0.25", "0.27"), "SELL_CALL"),
    ("call_between_the_bands", lambda: _call_pick("50", "0.16", "0.25", "0.27"), "HOLD_UNCOVERED"),
    ("low_call_ror_under_half_pct", lambda: _call_pick("50", "0.12", "0.24", "0.26"), "HOLD_UNCOVERED"),
    # WS §10
    (
        "cash_exactly_enough",
        lambda: stops.can_open(False, account(5_000, collateral=3_550), D(1450), CFG),
        None,
    ),
    ("per_ticker_limit_exactly", lambda: stops.can_open(False, account(5_000), D(2500), CFG), None),
    (
        "per_ticker_limit_a_cent_over",
        lambda: stops.can_open(False, account(5_000), D("2500.01"), CFG),
        "PER_TICKER_LIMIT",
    ),
    ("benchmark_flat_and_trailing", lambda: stops.benchmark_review(D("0.02"), D("0.0199"), CFG), True),
    ("benchmark_matched", lambda: stops.benchmark_review(D("0.02"), D("0.02"), CFG), False),
    ("benchmark_rising", lambda: stops.benchmark_review(D("0.0201"), D("-0.50"), CFG), False),
]


@pytest.mark.parametrize(("rule", "expected"), [c[1:] for c in EDGES], ids=[c[0] for c in EDGES])
def test_spec_thresholds_at_their_exact_values_and_first_match_order(
    rule: Callable[[], Any], expected: Any
) -> None:
    assert rule() == expected
