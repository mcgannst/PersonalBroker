"""P2-T8/T9 gauntlet breaker tests: orb_sip and spy_overlay edge cases (fakes only, no DB, no network)."""

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest

from tests.strategies.fakes import (
    CAL,
    NOW,
    FakeCatalyst,
    FakeCatalysts,
    FakeData,
    bar,
    make_ctx,
    position,
    quote,
    working_entry,
)
from trader.broker.types import OrderView
from trader.market.types import UniverseStatus
from trader.strategies.base import EnterLong, Exit, Intent
from trader.strategies.orb_sip import CANCEL_EVENT, FLATTEN_EVENT, ORB_EVENT, OrbSip, OrbSipParams
from trader.strategies.spy_overlay import DECISION_EVENT, SpyOverlay

AAA, BBB, CCC, DDD, EEE, FFF, GGG, HHH, III = 1, 2, 3, 4, 5, 6, 7, 8, 9
ZZZ = 26
SPY = 99
BULL = bar("21.00", "21.50", "20.90", "21.40", 5000)  # rvol 5 against the fake average of 1000
AT_1530 = datetime(2026, 10, 6, 19, 30, tzinfo=UTC)  # 15:30 ET on SESSION


async def run_orb(
    data: FakeData, cats: FakeCatalysts | None = None, params: OrbSipParams | None = None, **kw: Any
) -> tuple[Any, list[Intent]]:
    s = OrbSip(params)
    ctx = make_ctx(data, s.params, cats or FakeCatalysts(), **kw)
    event = next(e for e in s.schedule(CAL) if e.key == ORB_EVENT)
    return ctx, await s.on_event(ctx, event)


def all_cats(*sids: int) -> FakeCatalysts:
    return FakeCatalysts({s: FakeCatalyst() for s in sids})


def reasons(ctx: Any) -> dict[int, str | None]:
    return {c.symbol_id: c.reject_reason for c in ctx.candidates}


# --- orb_sip ------------------------------------------------------------------------------------------


async def test_flat_bar_zero_volume_and_missing_bars_never_crash() -> None:
    """A high == low bar is a doji, a zero-volume bar is never ranked, missing bars are noted."""
    data = FakeData()
    data.add(AAA, "AAA", BULL)
    data.add(BBB, "BBB", bar("30.00", "30.00", "30.00", "30.00", 9000))  # high == low, rvol 9
    data.add(CCC, "CCC", bar("10.00", "10.40", "9.95", "10.30", 0))  # zero volume
    data.add(DDD, "DDD", None)  # no opening bar
    data.add(EEE, "EEE", bar("15.00", "15.40", "14.95", "15.30", 9000), avg_open_vol="0")  # zero baseline
    cats = all_cats(AAA, BBB, CCC, EEE)
    ctx, intents = await run_orb(data, cats)
    assert [(i.symbol_id, type(i)) for i in intents] == [(AAA, EnterLong)]
    assert [(c.symbol_id, c.rank, c.reject_reason) for c in ctx.candidates] == [
        (BBB, 1, "doji"),
        (AAA, 2, None),
    ]
    assert cats.requested == [[AAA]]
    missing = next(n for n in ctx.notes if "no opening bar" in n.message)
    assert missing.data["missing"] == {str(DDD): "no_bar_at_open"}
    baseline = next(n for n in ctx.notes if "baseline" in n.message)
    assert baseline.data["symbol_ids"] == [EEE]

    # every symbol missing its bar: nothing ranked, no catalyst spend, no exception
    empty = FakeData()
    for sid, t in ((AAA, "AAA"), (BBB, "BBB")):
        empty.add(sid, t, None)
    cats2 = all_cats(AAA, BBB)
    ctx2, intents2 = await run_orb(empty, cats2)
    assert intents2 == [] and ctx2.candidates == [] and cats2.requested == []


async def test_screen_limits_are_inclusive_at_the_exact_thresholds() -> None:
    """price 5.00 / 50.00, ATR 0.50, avg volume 1,000,000 and rvol 1.0000 all pass; one tick past fails.

    (SPEC §5.2 defines no gap-up/gap-down filter, so the price band is the only price gate.)
    """
    data = FakeData()
    data.add(AAA, "AAA", bar("4.80", "5.10", "4.75", "5.00", 5000))  # close == price_min
    data.add(BBB, "BBB", bar("49.00", "50.20", "48.90", "50.00", 4900))  # close == price_max
    data.add(CCC, "CCC", bar("10.00", "10.40", "9.95", "10.30", 4800), atr="0.50", avg_volume=1_000_000)
    data.add(DDD, "DDD", bar("12.00", "12.40", "11.95", "12.30", 1000))  # rvol exactly 1.0000
    data.add(EEE, "EEE", bar("20.00", "20.50", "19.50", "20.10", 4700))  # body/range exactly 0.10: doji
    data.add(FFF, "FFF", bar("49.00", "50.20", "48.90", "50.01", 4600))  # one cent over price_max
    data.add(GGG, "GGG", bar("10.00", "10.40", "9.95", "10.30", 4500), atr="0.4999")
    data.add(HHH, "HHH", bar("10.00", "10.40", "9.95", "10.30", 4400), avg_volume=999_999)
    data.add(III, "III", bar("10.00", "10.40", "9.95", "10.30", 999))  # rvol 0.999: not ranked
    params = OrbSipParams(require_catalyst=False, max_positions=10)
    ctx, intents = await run_orb(data, None, params)
    assert reasons(ctx) == {
        AAA: None,
        BBB: None,
        CCC: None,
        DDD: None,
        EEE: "doji",
        FFF: "price_out_of_range",
        GGG: "atr_below_min",
        HHH: "avg_volume_below_min",
    }
    assert sorted(i.symbol_id for i in intents if isinstance(i, EnterLong)) == [AAA, BBB, CCC, DDD]


@pytest.mark.parametrize(
    ("status", "mode", "trades", "level"),
    [
        (UniverseStatus("fallback", date(2026, 9, 30), True, 4), "skip", False, "error"),
        (UniverseStatus("fallback", date(2026, 9, 30), True, 4), "trade", True, "warning"),
        (UniverseStatus("fallback", date(2026, 10, 5), False, 1), "skip", True, "warning"),
        (UniverseStatus("fallback", date(2026, 10, 5), False, 1), "trade", True, "warning"),
        (UniverseStatus("finviz", None, False, None), "skip", True, None),
    ],
    ids=["stale-skip", "stale-trade", "fresh-fallback-skip", "fresh-fallback-trade", "finviz-skip"],
)
async def test_stale_universe_matrix(
    status: UniverseStatus, mode: str, trades: bool, level: str | None
) -> None:
    data = FakeData()
    data.add(AAA, "AAA", BULL)
    data.status = status
    cats = all_cats(AAA)
    ctx, intents = await run_orb(data, cats, OrbSipParams(stale_universe=mode))  # type: ignore[arg-type]
    assert [i.symbol_id for i in intents if isinstance(i, EnterLong)] == ([AAA] if trades else [])
    if not trades:  # a skipped session spends nothing on catalysts and ranks nothing
        assert cats.requested == [] and ctx.candidates == []
    universe_notes = [n for n in ctx.notes if "fallback" in n.message or "stale" in n.message]
    if level is None:
        assert universe_notes == []
    else:
        assert [n.level for n in universe_notes] == [level]


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (  # Christmas Eve 2026: 13:00 ET early close, EST
            date(2026, 12, 24),
            {
                ORB_EVENT: datetime(2026, 12, 24, 14, 35, 5, tzinfo=UTC),
                CANCEL_EVENT: datetime(2026, 12, 24, 16, 30, tzinfo=UTC),
                FLATTEN_EVENT: datetime(2026, 12, 24, 17, 50, tzinfo=UTC),
                DECISION_EVENT: datetime(2026, 12, 24, 17, 30, tzinfo=UTC),
            },
        ),
        (  # first session after the DST change (Sunday 2026-11-01)
            date(2026, 11, 2),
            {
                ORB_EVENT: datetime(2026, 11, 2, 14, 35, 5, tzinfo=UTC),
                CANCEL_EVENT: datetime(2026, 11, 2, 16, 30, tzinfo=UTC),
                FLATTEN_EVENT: datetime(2026, 11, 2, 20, 50, tzinfo=UTC),
                DECISION_EVENT: datetime(2026, 11, 2, 20, 30, tzinfo=UTC),
            },
        ),
    ],
    ids=["christmas-eve-early-close", "after-dst-ends"],
)
def test_schedule_events_follow_early_close_and_dst(day: date, expected: dict[str, datetime]) -> None:
    events = [*OrbSip().schedule(CAL), *SpyOverlay().schedule(CAL)]
    times = {e.key: e.at.resolve(CAL, day) for e in events}
    assert times == expected
    close = CAL.session_close(day)
    assert times[DECISION_EVENT] < times[FLATTEN_EVENT] < close  # the overlay decides before the flatten


@pytest.mark.parametrize("held_as", ["open_position", "working_entry"])
async def test_a_symbol_already_held_or_working_is_not_entered_again(held_as: str) -> None:
    """With a second slot free (a re-fired orb_open, or max_positions=2), AAA must not be doubled up."""
    data = FakeData()
    data.add(AAA, "AAA", BULL)
    data.add(BBB, "BBB", bar("30.00", "30.60", "29.90", "30.50", 3000))
    kw: dict[str, Any] = (
        {"positions": [position(5, AAA)]}
        if held_as == "open_position"
        else {"orders": [working_entry(9, AAA)]}
    )
    ctx, intents = await run_orb(
        data, all_cats(AAA, BBB), OrbSipParams(max_positions=2), entries_today=1, **kw
    )
    assert [i.symbol_id for i in intents if isinstance(i, EnterLong)] == [BBB]
    assert reasons(ctx)[AAA] is not None


@pytest.mark.parametrize(
    ("aaa_bar", "atr", "params"),
    [
        (BULL, "0", OrbSipParams(min_atr=Decimal("0"))),  # ATR 0: stop_loss == entry
        # fix round 1: stop_atr_fraction is now capped at 1, so a large ATR drives the stop below zero
        (bar("5.00", "5.50", "4.95", "5.40", 5000), "6.00", OrbSipParams(stop_atr_fraction=Decimal("1"))),
    ],
    ids=["stop-equals-entry", "stop-below-zero"],
)
async def test_protective_stop_is_always_below_the_entry_and_positive(
    aaa_bar: Any, atr: str, params: OrbSipParams
) -> None:
    data = FakeData()
    data.add(AAA, "AAA", aaa_bar, atr=atr)
    _, intents = await run_orb(data, all_cats(AAA), params)
    for i in intents:
        assert isinstance(i, EnterLong) and i.stop is not None
        assert Decimal("0") < i.stop_loss < i.stop, f"stop_loss {i.stop_loss} vs entry {i.stop}"


def _walk_no_float(obj: Any, path: str = "") -> None:
    assert not isinstance(obj, float), f"float at {path}: {obj!r}"
    if isinstance(obj, dict):
        for k, v in obj.items():
            _walk_no_float(v, f"{path}.{k}")
    elif isinstance(obj, list | tuple):
        for n, v in enumerate(obj):
            _walk_no_float(v, f"{path}[{n}]")


async def test_ties_are_deterministic_pool_is_capped_and_everything_is_decimal() -> None:
    # equal rvol: the ticker breaks the tie, whatever order the data arrives in
    for order in (("ZZZ", "AAA"), ("AAA", "ZZZ")):
        data = FakeData()
        for t in order:
            data.add(AAA if t == "AAA" else ZZZ, t, BULL)
        ctx, intents = await run_orb(data, all_cats(AAA, ZZZ))
        assert [(c.symbol_id, c.rank) for c in ctx.candidates] == [(AAA, 1), (ZZZ, 2)]
        assert [i.symbol_id for i in intents if isinstance(i, EnterLong)] == [AAA]

    # 25 qualifying names: exactly top_n=20 ranked, max_positions=3 selected, the rest lower_rank
    data = FakeData()
    sids = list(range(101, 126))
    for n, sid in enumerate(sids):
        data.add(sid, f"T{n:02d}", bar("21.00", "21.50", "20.90", "21.40", 10_000 - n * 100))
    cats = all_cats(*sids)
    params = OrbSipParams.model_validate_json(
        '{"max_positions": 3, "entry_offset": 0.01, "stop_atr_fraction": 0.1, '
        '"min_atr": 0.5, "rvol_min": 1.0}'
    )
    ctx, intents = await run_orb(data, cats, params)
    assert [c.rank for c in ctx.candidates] == list(range(1, 21))
    assert cats.requested == [sids[:20]]
    assert [i.symbol_id for i in intents if isinstance(i, EnterLong)] == sids[:3]
    assert [c.reject_reason for c in ctx.candidates[3:]] == ["lower_rank"] * 17
    # Decimal only: params from JSON floats, intent prices, candidate rvol, evidence and candidate data
    for name in ("entry_offset", "stop_atr_fraction", "min_atr", "rvol_min"):
        assert type(getattr(params, name)) is Decimal
    for i in intents:
        assert isinstance(i, EnterLong)
        assert type(i.stop) is Decimal and type(i.stop_loss) is Decimal and i.limit is None
        assert i.stop == Decimal("21.5100") and i.stop_loss == Decimal("21.4100")
        _walk_no_float(i.evidence, "evidence")
    for c in ctx.candidates:
        assert type(c.rvol) is Decimal
        _walk_no_float(c.data, "data")
        _walk_no_float(c.candle, "candle")


# --- spy_overlay --------------------------------------------------------------------------------------


def spy_data(last: str | None = "500.00", prior: str | None = "500.00") -> FakeData:
    d = FakeData()
    d.ids["SPY"] = SPY
    if prior is not None:
        d.closes[SPY] = Decimal(prior)
    if last is not None:
        d.quote_map[SPY] = quote(SPY, last, last, last, at=AT_1530)
    return d


async def overlay(
    d: FakeData, pids: list[int], orders: list[OrderView] | None = None
) -> tuple[list[Intent], dict[str, Any], str]:
    s = SpyOverlay()
    ctx = make_ctx(d, s.params, positions=[position(p, AAA) for p in pids], orders=orders or [], now=AT_1530)
    (event,) = s.schedule(CAL)
    intents = await s.on_event(ctx, event)
    note = next(n for n in ctx.notes if n.message.startswith("overlay"))
    return intents, note.data, note.level


@pytest.mark.parametrize(
    ("last", "pids", "decision", "exits"),
    [
        ("500.00", [5, 6], "exit", [5, 6]),  # exactly 0.00%: non-positive => exit
        ("500.00", [], "exit", []),  # no open positions: decision logged, nothing emitted
        ("499.9999", [5], "exit", [5]),  # a hair negative
        ("500.0001", [5], "hold", []),  # a hair positive: must not round to 0 and exit
        ("500.01", [5], "hold", []),
    ],
    ids=["zero", "zero-no-positions", "tiny-negative", "tiny-positive", "positive"],
)
async def test_overlay_sign_of_the_spy_day(
    last: str, pids: list[int], decision: str, exits: list[int]
) -> None:
    intents, data, level = await overlay(spy_data(last), pids)
    assert data["decision"] == decision and level == "info"
    assert data["position_ids"] == pids
    assert intents == [Exit(p, "market", None, "overlay_negative") for p in exits]


def _spy_quote(case: str) -> Any:
    q = quote(SPY, "497.00", "497.00", "497.00", at=AT_1530)
    if case == "halted":
        return replace(q, is_halted=True)
    if case == "no_last":
        return replace(q, last=None, last_regular=None)
    if case == "last_regular_only":
        return replace(q, last=None, last_regular=Decimal("497.00"))
    if case == "prior_session":  # yesterday's 15:59 ET print: not today's price
        return replace(q, last_trade_time=datetime(2026, 10, 5, 19, 59, tzinfo=UTC))
    return None


@pytest.mark.parametrize(
    ("case", "decision"),
    [
        ("absent", "hold"),
        ("halted", "hold"),
        ("no_last", "hold"),
        ("last_regular_only", "exit"),
        ("prior_session", "hold"),
    ],
)
async def test_overlay_missing_or_unusable_spy_quote(case: str, decision: str) -> None:
    d = spy_data(last=None)
    q = _spy_quote(case)
    if q is not None:
        d.quote_map[SPY] = q
    intents, data, level = await overlay(d, [5])
    assert data["decision"] == decision
    if decision == "hold":
        assert intents == [] and level in ("warning", "error")
    else:
        assert intents == [Exit(5, "market", None, "overlay_negative")]


async def test_overlay_exit_is_issued_only_once_per_position() -> None:
    """A re-fired overlay_decision (worker + cron backup) must not queue a second exit for position 5."""
    d = spy_data("497.00")
    first, _, _ = await overlay(d, [5, 6])
    assert first == [Exit(5, "market", None, "overlay_negative"), Exit(6, "market", None, "overlay_negative")]
    pending_exit = OrderView(
        id=40,
        symbol_id=AAA,
        side="sell",
        order_type="market",
        purpose="exit",
        qty=10,
        stop=None,
        limit=None,
        status="working",
        position_id=5,
        strategy_config_id=1,
        proposal_id=None,
        submitted_at=NOW,
    )
    again, data, _ = await overlay(d, [5, 6], orders=[pending_exit])
    assert data["decision"] == "exit"
    assert again == [Exit(6, "market", None, "overlay_negative")]
