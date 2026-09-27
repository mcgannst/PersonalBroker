"""Hand-built ORB scenarios (SPEC §16): rankings, rejects, and fills through the real quote fill model."""

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from tests.strategies.fakes import (
    CAL,
    NOW,
    SESSION,
    FakeCatalyst,
    FakeCatalysts,
    FakeData,
    bar,
    make_ctx,
    position,
    quote,
    working_entry,
)
from trader.adapters.questrade.models import QtQuote
from trader.broker.fill_model import FillParams, QuoteFillModel
from trader.broker.types import FillDecision, FillEvent, OrderSpec
from trader.market.types import Candle, UniverseStatus
from trader.strategies.base import Cancel, EnterLong, Exit, Intent, ScheduledEvent, StrategyContext
from trader.strategies.orb_sip import CANCEL_EVENT, FLATTEN_EVENT, ORB_EVENT, OrbSip, OrbSipParams
from trader.strategies.registry import load_plugin

MODEL = QuoteFillModel(FillParams())
AAA, BBB, CCC, DDD, EEE = 1, 2, 3, 4, 5
BULL = bar("21.00", "21.50", "20.90", "21.40", 5000)  # rvol 5 against an average of 1000


def orb_event(strategy: OrbSip) -> ScheduledEvent:
    return next(e for e in strategy.schedule(CAL) if e.key == ORB_EVENT)


def event(strategy: OrbSip, key: str) -> ScheduledEvent:
    return next(e for e in strategy.schedule(CAL) if e.key == key)


def standard() -> tuple[FakeData, FakeCatalysts]:
    data = FakeData()
    data.add(AAA, "AAA", BULL)
    data.add(BBB, "BBB", bar("30.00", "30.60", "29.90", "30.50", 3000))
    data.add(CCC, "CCC", bar("10.00", "10.40", "9.95", "10.30", 800))  # rvol 0.8: not ranked
    cats = FakeCatalysts({AAA: FakeCatalyst(), BBB: FakeCatalyst()})
    return data, cats


async def run_orb(
    data: FakeData, cats: FakeCatalysts, params: OrbSipParams | None = None, **ctx_kw: Any
) -> tuple[OrbSip, StrategyContext, list[Intent]]:
    strategy = OrbSip(params)
    ctx = make_ctx(data, strategy.params, cats, **ctx_kw)
    intents = await strategy.on_event(ctx, orb_event(strategy))
    return strategy, ctx, intents


def first_fill(spec: OrderSpec, quotes: Sequence[QtQuote]) -> FillDecision | None:
    for q in quotes:
        d = MODEL.evaluate(spec, q, q.last_trade_time + timedelta(seconds=1))  # type: ignore[operator]
        if d is not None:
            return d
    return None


def entry_spec(intent: EnterLong, qty: int = 10) -> OrderSpec:
    return OrderSpec(
        intent.symbol_id, "buy", intent.order_type, qty, stop=intent.stop, stop_loss=intent.stop_loss
    )


def as_event(spec: OrderSpec, d: FillDecision, position_id: int = 1) -> FillEvent:
    return FillEvent(
        1,
        1,
        1,
        spec.symbol_id,
        spec.side,
        spec.purpose,
        d.qty,
        d.price,
        NOW,
        position_id,
        1,
        spec.stop_loss,
        None,
    )


async def test_breakout_fills_and_places_a_protective_stop() -> None:
    data, cats = standard()
    strategy, ctx, intents = await run_orb(data, cats)
    (e,) = intents
    assert isinstance(e, EnterLong) and e.symbol_id == AAA and e.order_type == "stop"
    assert e.stop == Decimal("21.5100") and e.stop_loss == Decimal("21.4100")  # 21.51 - 0.10 x 1.00
    # BR-13: the rule values behind the signal
    assert e.evidence["rvol"] == "5.0000" and e.evidence["direction"] == "bullish"
    assert e.evidence["atr14"] == "1.00" and e.evidence["entry"] == "21.5100"
    assert e.evidence["stop_loss"] == "21.4100" and e.evidence["candle"]["high"] == "21.50"
    spec = entry_spec(e)
    at = NOW + timedelta(minutes=1)
    assert first_fill(spec, [quote(AAA, "21.43", "21.45", "21.44", at)]) is None  # no breakout yet
    d = first_fill(spec, [quote(AAA, "21.52", "21.55", "21.53", at)])
    assert d is not None and d.price == Decimal("21.5608")  # max(21.51, 21.55) + 0.0108
    (stop,) = await strategy.on_fill(ctx, as_event(spec, d))
    assert stop == Exit(1, "stop", Decimal("21.4100"), "protective_stop")


async def test_no_breakout_is_cancelled_at_the_entry_cancel_time() -> None:
    data, cats = standard()
    strategy, ctx, (e,) = await run_orb(data, cats)
    spec = entry_spec(e)
    quotes = [quote(AAA, "21.40", "21.42", "21.41", NOW + timedelta(minutes=i)) for i in range(1, 60)]
    assert first_fill(spec, quotes) is None
    cancel_event = event(strategy, CANCEL_EVENT)
    assert cancel_event.at.resolve(CAL, SESSION) == datetime(2026, 10, 6, 15, 30, tzinfo=UTC)  # 11:30 ET
    later = make_ctx(data, strategy.params, cats, orders=[working_entry(77, AAA)])
    assert await strategy.on_event(later, cancel_event) == [Cancel(77, "entry_cancel_at")]


async def test_stop_hit_after_the_entry() -> None:
    data, cats = standard()
    strategy, ctx, (e,) = await run_orb(data, cats)
    d = first_fill(entry_spec(e), [quote(AAA, "21.52", "21.55", "21.53")])
    assert d is not None
    (stop,) = await strategy.on_fill(ctx, as_event(entry_spec(e), d))
    assert isinstance(stop, Exit) and stop.stop is not None
    stop_spec = OrderSpec(AAA, "sell", "stop", 10, stop=stop.stop, purpose="stop", position_id=1)
    assert first_fill(stop_spec, [quote(AAA, "21.45", "21.46", "21.45")]) is None
    hit = first_fill(stop_spec, [quote(AAA, "21.38", "21.40", "21.39")])
    assert hit is not None and hit.price == Decimal("21.3693")  # min(21.41, 21.38) - 0.0107


@pytest.mark.parametrize(
    ("aaa_bar", "reason"),
    [
        (bar("21.00", "21.50", "20.90", "21.02", 5000), "doji"),  # body 0.02 / range 0.60
        (bar("21.40", "21.50", "20.90", "21.00", 5000), "bearish_candle"),
        (bar("21.00", "20.50", "20.90", "21.40", 5000), "malformed_bar"),  # high < low
    ],
)
async def test_bad_opening_candles_are_skipped(aaa_bar: object, reason: str) -> None:
    data, cats = standard()
    data.bars[AAA] = aaa_bar  # type: ignore[assignment]
    _, ctx, (e,) = await run_orb(data, cats)
    assert e.symbol_id == BBB  # the next-ranked name is chosen instead
    rejected = {c.symbol_id: c.reject_reason for c in ctx.candidates}
    assert rejected[AAA] == reason


async def test_price_atr_and_volume_limits() -> None:
    data = FakeData()
    data.add(AAA, "AAA", bar("54.00", "55.50", "53.90", "55.40", 5000))
    data.add(BBB, "BBB", bar("30.00", "30.60", "29.90", "30.50", 4000), atr="0.40")
    data.add(CCC, "CCC", bar("10.00", "10.40", "9.95", "10.30", 3000), avg_volume=500_000)
    data.add(DDD, "DDD", bar("12.00", "12.40", "11.95", "12.30", 2000), atr=None)
    cats = FakeCatalysts({i: FakeCatalyst() for i in (AAA, BBB, CCC, DDD)})
    _, ctx, intents = await run_orb(data, cats)
    assert intents == []
    assert {c.symbol_id: c.reject_reason for c in ctx.candidates} == {
        AAA: "price_out_of_range",
        BBB: "atr_below_min",
        CCC: "avg_volume_below_min",
        DDD: "atr_missing",
    }
    assert cats.requested == []  # nothing survived the screen, so no catalyst lookups


async def test_catalyst_missing_bearish_or_weak_is_rejected() -> None:
    data = FakeData()
    for sid, t in ((AAA, "AAA"), (BBB, "BBB"), (DDD, "DDD"), (EEE, "EEE")):
        data.add(sid, t, bar("21.00", "21.50", "20.90", "21.40", 6000 - sid * 100))
    cats = FakeCatalysts(
        {
            BBB: FakeCatalyst(direction="bearish"),
            DDD: FakeCatalyst(quality=30),
            EEE: FakeCatalyst(catalyst_type="unknown", quality=None, classified=False),
        }
    )
    _, ctx, intents = await run_orb(data, cats)
    assert intents == []
    assert {c.symbol_id: c.reject_reason for c in ctx.candidates} == {
        AAA: "catalyst_missing",
        BBB: "catalyst_bearish",
        DDD: "catalyst_low_quality",
        EEE: "catalyst_missing",
    }
    _, _, relaxed = await run_orb(data, cats, OrbSipParams(require_catalyst=False))
    assert [i.symbol_id for i in relaxed] == [AAA]  # type: ignore[union-attr]


async def test_only_screen_survivors_are_sent_for_catalysts() -> None:
    data, cats = standard()
    data.bars[AAA] = bar("21.40", "21.50", "20.90", "21.00", 5000)  # bearish
    await run_orb(data, cats)
    assert cats.requested == [[BBB]]


async def test_every_ranked_candidate_is_saved_with_its_reason() -> None:
    data, cats = standard()
    data.add(DDD, "DDD", bar("15.00", "15.40", "14.95", "15.30", 2000))
    cats.by_symbol[DDD] = FakeCatalyst()
    _, ctx, (e,) = await run_orb(data, cats, OrbSipParams(top_n=2))
    assert e.symbol_id == AAA
    assert [(c.symbol_id, c.rank, c.passed, c.reject_reason) for c in ctx.candidates] == [
        (AAA, 1, True, None),
        (BBB, 2, False, "lower_rank"),
    ]
    assert ctx.candidates[0].rvol == Decimal("5.0000") and ctx.candidates[0].candle is not None


async def test_missing_bars_and_baselines_are_noted_not_raised() -> None:
    """Review Focus 4: no 9:30 bar or no volume history skips the symbol with a recorded reason."""
    data, cats = standard()
    data.add(DDD, "DDD", None)
    data.add(EEE, "EEE", bar("15.00", "15.40", "14.95", "15.30", 9000), avg_open_vol=None)
    _, ctx, (e,) = await run_orb(data, cats)
    assert e.symbol_id == AAA  # EEE would rank first on volume but has no baseline
    missing = next(n for n in ctx.notes if "no opening bar" in n.message)
    assert missing.data["missing"] == {str(DDD): "no_bar_at_open"} and missing.level == "warning"
    baseline = next(n for n in ctx.notes if "baseline" in n.message)
    assert baseline.data["symbol_ids"] == [EEE]
    assert all(c.symbol_id not in (DDD, EEE) for c in ctx.candidates)


async def test_no_new_entry_once_max_positions_is_used() -> None:
    data, cats = standard()
    _, ctx, intents = await run_orb(data, cats, entries_today=1)
    assert intents == [] and ctx.candidates == []
    assert "max_positions" in ctx.notes[0].message


async def test_a_stale_fallback_universe_skips_entries_by_default() -> None:
    data, cats = standard()
    data.status = UniverseStatus("fallback", date(2026, 9, 30), True, 4)
    _, ctx, intents = await run_orb(data, cats)
    assert intents == [] and ctx.notes[0].level == "error" and "stale" in ctx.notes[0].message
    _, ctx2, traded = await run_orb(data, cats, OrbSipParams(stale_universe="trade"))
    assert [i.symbol_id for i in traded] == [AAA]  # type: ignore[union-attr]
    assert any(n.level == "warning" and "fallback" in n.message for n in ctx2.notes)


def test_an_early_close_moves_the_flatten() -> None:
    s = OrbSip()
    day = date(2026, 11, 27)  # 13:00 ET close; EST
    times = {e.key: e.at.resolve(CAL, day) for e in s.schedule(CAL)}
    assert times == {
        ORB_EVENT: datetime(2026, 11, 27, 14, 35, 5, tzinfo=UTC),
        CANCEL_EVENT: datetime(2026, 11, 27, 16, 30, tzinfo=UTC),
        FLATTEN_EVENT: datetime(2026, 11, 27, 17, 50, tzinfo=UTC),  # 12:50 ET
    }
    assert {e.key for e in OrbSip(OrbSipParams(entry_cancel_at=None)).schedule(CAL)} == {
        ORB_EVENT,
        FLATTEN_EVENT,
    }


async def test_flatten_exits_positions_and_cancels_working_entries() -> None:
    data, cats = standard()
    s = OrbSip()
    ctx = make_ctx(data, s.params, cats, positions=[position(5, AAA)], orders=[working_entry(9, BBB)])
    assert await s.on_event(ctx, event(s, FLATTEN_EVENT)) == [
        Cancel(9, "flatten_close"),
        Exit(5, "market", None, "flatten_close"),
    ]


async def test_non_entry_fills_need_nothing() -> None:
    data, cats = standard()
    s = OrbSip()
    ctx = make_ctx(data, s.params, cats)
    stop_fill = FillEvent(
        2, 2, 1, AAA, "sell", "stop", 10, Decimal("21.37"), NOW, 1, 1, Decimal("21.41"), None, 3
    )
    assert await s.on_fill(ctx, stop_fill) == []


@pytest.mark.parametrize(
    "bad",
    [
        {"price_min": "50", "price_max": "5"},
        {"exit_at": "close-10"},
        {"entry_cancel_at": "noon"},
        {"top_n": 0},
        {"surprise": 1},
        {"stale_universe": "maybe"},
    ],
)
def test_invalid_params_are_rejected(bad: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        OrbSipParams.model_validate(bad)


def test_the_plugin_loads_through_its_entry_point() -> None:
    cls = load_plugin("orb_sip")
    assert cls is OrbSip and cls.version == "1.0.0" and cls.kind == "entry"


# --- fix round 1 (gauntlet findings) ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kw", "reason"),
    [
        ({"positions": [position(5, AAA)]}, "already_held"),
        ({"orders": [working_entry(9, AAA)]}, "entry_working"),
    ],
)
async def test_held_or_working_names_are_skipped_and_the_next_name_fills_the_slot(
    kw: dict[str, Any], reason: str
) -> None:
    data, cats = standard()
    _, ctx, intents = await run_orb(data, cats, OrbSipParams(max_positions=2), entries_today=1, **kw)
    assert [i.symbol_id for i in intents if isinstance(i, EnterLong)] == [BBB]
    assert {c.symbol_id: c.reject_reason for c in ctx.candidates}[AAA] == reason
    assert cats.requested == [[BBB]]  # no catalyst spend on a name that is already held


async def test_free_slots_never_double_count_held_names() -> None:
    """entries_today already counts today's position; a carried position with no entries still uses a slot."""
    data, cats = standard()
    _, _, used = await run_orb(data, cats, entries_today=0, positions=[position(5, DDD)])
    assert used == []
    _, _, both = await run_orb(data, cats, OrbSipParams(max_positions=3), entries_today=1)
    assert [i.symbol_id for i in both if isinstance(i, EnterLong)] == [AAA, BBB]


@pytest.mark.parametrize(
    ("aaa_bar", "atr"),
    [
        (BULL, "0"),  # ATR 0: stop_loss == entry
        (bar("5.00", "5.50", "4.95", "5.40", 5000), "6.00"),  # 5.51 - 6.00 < 0
    ],
)
async def test_an_invalid_stop_rejects_the_candidate_and_the_next_name_is_used(
    aaa_bar: Candle, atr: str
) -> None:
    data, cats = standard()
    data.members = [m for m in data.members if m.symbol_id != AAA]
    data.add(AAA, "AAA", aaa_bar, atr=atr)
    params = OrbSipParams(min_atr=Decimal("0"), stop_atr_fraction=Decimal("1"))
    _, ctx, intents = await run_orb(data, cats, params)
    assert {c.symbol_id: c.reject_reason for c in ctx.candidates}[AAA] == "stop_invalid"
    (e,) = intents
    assert isinstance(e, EnterLong) and e.symbol_id == BBB and e.stop is not None
    assert Decimal("0") < e.stop_loss < e.stop
    assert cats.requested == [[BBB]]


async def test_evidence_names_the_universe_and_atr_sources() -> None:
    data, cats = standard()
    _, _, (e,) = await run_orb(data, cats)
    assert isinstance(e, EnterLong)
    assert e.evidence["universe_source"] == "finviz" and e.evidence["atr_source"] == "open_bar_stats"
    data.stats[AAA] = replace(data.stats[AAA], atr14=None)
    _, _, (e2,) = await run_orb(data, cats)
    assert isinstance(e2, EnterLong) and e2.evidence["atr_source"] == "universe"


async def test_an_entry_fill_without_a_stop_loss_is_an_error_note() -> None:
    data, cats = standard()
    s = OrbSip()
    ctx = make_ctx(data, s.params, cats)
    fill = FillEvent(1, 1, 1, AAA, "buy", "entry", 10, Decimal("21.56"), NOW, 5, 1, None, None)
    assert await s.on_fill(ctx, fill) == []
    assert ctx.notes[-1].level == "error" and ctx.notes[-1].data["position_id"] == 5


@pytest.mark.parametrize(
    "bad",
    [
        {"stop_atr_fraction": "1.01"},
        {"stop_atr_fraction": "0"},
        {"exit_at": "close"},
        {"exit_at": "close+5m"},
        {"exit_at": "open+300m"},
        {"entry_cancel_at": "open+5m"},
        {"entry_cancel_at": "open+5m5s"},
        {"entry_cancel_at": "close"},
        {"entry_cancel_at": "close+10m"},
    ],
)
def test_offsets_and_stop_fraction_are_bounded(bad: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        OrbSipParams.model_validate(bad)


@pytest.mark.parametrize(
    "good",
    [
        {"stop_atr_fraction": "1"},
        {"entry_cancel_at": "open+5m6s"},
        {"entry_cancel_at": "close-60m"},
        {"exit_at": "close-1m"},
    ],
)
def test_boundary_offsets_are_accepted(good: dict[str, object]) -> None:
    OrbSipParams.model_validate(good)
