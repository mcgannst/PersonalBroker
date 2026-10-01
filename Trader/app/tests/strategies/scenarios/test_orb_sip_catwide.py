"""CATWIDE: orb_sip's bearish-only catalyst policy, walking past top_n until the slots are full, and the paper
doji rule (doji_body_pct_max = 0). Every default keeps the 1.0.0 behaviour."""

from collections.abc import Sequence
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from tests.strategies.fakes import CAL, FakeCatalyst, FakeCatalysts, FakeData, bar, make_ctx, position
from trader.decisions.orb_explain import explain_orb, first_failure
from trader.market.clock import FixedClock
from trader.market.indicators import is_doji
from trader.strategies.base import EnterLong, StrategyContext
from trader.strategies.orb_sip import ORB_EVENT, OrbSip, OrbSipParams


def _bull(volume: int) -> Any:
    return bar("21.00", "21.50", "20.90", "21.40", volume)


BEARISH_BAR = bar("21.00", "21.50", "20.50", "20.60", 9000)


async def _run(
    data: FakeData, cats: FakeCatalysts, params: OrbSipParams, **ctx_kw: Any
) -> tuple[StrategyContext, list[EnterLong]]:
    strategy = OrbSip(params)
    ctx = make_ctx(data, params, cats, **ctx_kw)
    event = next(e for e in strategy.schedule(CAL) if e.key == ORB_EVENT)
    intents = await strategy.on_event(ctx, event)
    return ctx, [i for i in intents if isinstance(i, EnterLong)]


def _explained(ctx: StrategyContext, params: OrbSipParams) -> None:
    """The decision log's explain_orb agrees with every stored reject reason."""
    for rec in ctx.candidates:
        checks = explain_orb(
            rec.data, rec.candle, params, catalyst=rec.data.get("catalyst"), reject_reason=rec.reject_reason
        )
        assert first_failure(checks) == rec.reject_reason, (rec.data["ticker"], checks)


# --- A: catalyst policy -------------------------------------------------------------------------------------
CATS: dict[str, FakeCatalyst | None] = {
    "none_found": None,
    "unclassified": FakeCatalyst(classified=False),
    "type_none": FakeCatalyst(catalyst_type="none"),
    "neutral": FakeCatalyst(direction="neutral"),
    "low_quality": FakeCatalyst(quality=10),
    "no_quality": FakeCatalyst(quality=None),
    "bullish": FakeCatalyst(),
    "bearish": FakeCatalyst(direction="bearish"),
    "bearish_low_quality": FakeCatalyst(direction="bearish", quality=5),
    "bearish_unclassified": FakeCatalyst(direction="bearish", classified=False),
}
LABELS = list(CATS)


def _catalyst_universe() -> tuple[FakeData, FakeCatalysts, dict[str, int]]:
    data = FakeData()
    by_symbol: dict[int, FakeCatalyst] = {}
    sids: dict[str, int] = {}
    for i, label in enumerate(LABELS, start=1):
        data.add(i, f"T{i:02d}", _bull(9000 - i * 10))  # distinct rvol: rank i
        sids[label] = i
        cat = CATS[label]
        if cat is not None:
            by_symbol[i] = cat
    return data, FakeCatalysts(by_symbol), sids


REQUIRED = {
    "none_found": "catalyst_missing",
    "unclassified": "catalyst_missing",
    "type_none": "catalyst_missing",
    "neutral": None,
    "low_quality": "catalyst_low_quality",
    "no_quality": "catalyst_low_quality",
    "bullish": None,
    "bearish": "catalyst_bearish",
    "bearish_low_quality": "catalyst_bearish",
    "bearish_unclassified": "catalyst_missing",
}
BEARISH_ONLY = {label: None for label in LABELS} | {
    "bearish": "catalyst_bearish",
    "bearish_low_quality": "catalyst_bearish",
}
NO_CHECK = dict.fromkeys(LABELS)


@pytest.mark.parametrize(
    ("require", "reject_bearish", "expected"),
    [
        (True, False, REQUIRED),
        (True, True, REQUIRED),  # require_catalyst wins: reject_bearish changes nothing
        (False, True, BEARISH_ONLY),
        (False, False, NO_CHECK),
    ],
    ids=["required", "required+bearish_flag", "bearish_only", "no_check"],
)
async def test_catalyst_modes(require: bool, reject_bearish: bool, expected: dict[str, str | None]) -> None:
    data, cats, sids = _catalyst_universe()
    params = OrbSipParams(
        require_catalyst=require, reject_bearish_catalyst=reject_bearish, max_positions=10, top_n=20
    )
    ctx, intents = await _run(data, cats, params)
    by_sid = {r.symbol_id: r for r in ctx.candidates}
    got = {label: by_sid[sid].reject_reason for label, sid in sids.items()}
    assert got == expected
    passed = sorted(sid for label, sid in sids.items() if expected[label] is None)
    assert sorted(i.symbol_id for i in intents) == passed
    # the catalyst is looked up once and recorded on every candidate, in every mode
    assert cats.requested == [sorted(sids.values())]
    for label, sid in sids.items():
        cat = CATS[label]
        want = (
            None
            if cat is None
            else {
                "type": cat.catalyst_type,
                "direction": cat.direction,
                "quality": cat.quality,
                "classified": cat.classified,
            }
        )
        assert by_sid[sid].data["catalyst"] == want
    _explained(ctx, params)


async def test_bearish_only_no_catalyst_trades_with_one_slot() -> None:
    data = FakeData()
    data.add(1, "AAA", _bull(9000))
    data.add(2, "BBB", _bull(8000))
    cats = FakeCatalysts({1: FakeCatalyst(direction="bearish")})  # BBB has no catalyst at all
    params = OrbSipParams(require_catalyst=False, reject_bearish_catalyst=True)
    ctx, intents = await _run(data, cats, params)
    assert [i.symbol_id for i in intents] == [2]
    assert [(r.symbol_id, r.reject_reason, r.passed) for r in ctx.candidates] == [
        (1, "catalyst_bearish", False),
        (2, None, True),
    ]
    assert intents[0].evidence["catalyst"] is None


def test_bearish_only_explain_has_a_single_catalyst_check() -> None:
    params = OrbSipParams(require_catalyst=False, reject_bearish_catalyst=True)
    data = {"rvol": "2", "rank": 1, "atr14": "1", "avg_volume": 2_000_000}
    candle = {
        "start": "2026-10-06T13:30:00+00:00",
        "open": "21",
        "high": "21.5",
        "low": "20.9",
        "close": "21.4",
    }
    checks = explain_orb(data, candle, params, catalyst=None, reject_reason=None)
    assert [c.name for c in checks if c.name.startswith("catalyst")] == ["catalyst_not_bearish"]
    assert first_failure(checks) is None
    bearish = {"type": "guidance_cut", "direction": "bearish", "quality": 90, "classified": True}
    checks = explain_orb(data, candle, params, catalyst=bearish, reject_reason="catalyst_bearish")
    assert first_failure(checks) == "catalyst_bearish"


def test_defaults_are_the_old_behaviour() -> None:
    p = OrbSipParams()
    assert (p.reject_bearish_catalyst, p.extend_past_top_n, p.max_rank) == (False, False, 100)
    # a stored 1.0.0 config (no new keys) still loads, and gets the defaults
    old = OrbSipParams().model_dump(mode="json")
    for k in ("reject_bearish_catalyst", "extend_past_top_n", "max_rank"):
        old.pop(k)
    assert OrbSipParams.model_validate(old) == p


# --- B: walking past top_n ----------------------------------------------------------------------------------
def _ranked_universe(
    n: int, good: set[int], *, extra_cats: dict[int, FakeCatalyst] | None = None
) -> tuple[FakeData, FakeCatalysts]:
    """n names ranked 1..n by rvol (sid == rank); the ones in `good` are bullish, the rest bearish bars."""
    data = FakeData()
    for rank in range(1, n + 1):
        candle = (
            _bull(9000 - rank * 10)
            if rank in good
            else bar("21.00", "21.50", "20.50", "20.60", 9000 - rank * 10)
        )
        data.add(rank, f"R{rank:03d}", candle)
    cats = {s: FakeCatalyst() for s in range(1, n + 1)}
    cats.update(extra_cats or {})
    return data, FakeCatalysts(cats)


async def test_off_stops_at_top_n_even_with_slots_left() -> None:
    data, cats = _ranked_universe(12, good={7, 8})
    params = OrbSipParams(top_n=5, max_positions=2)
    ctx, intents = await _run(data, cats, params)
    assert intents == []
    assert [r.rank for r in ctx.candidates] == [1, 2, 3, 4, 5]
    assert cats.requested == []  # no survivors in the top 5: no lookup


async def test_on_next_chunk_fills_the_slots() -> None:
    data, cats = _ranked_universe(30, good={2, 7, 9, 12})
    params = OrbSipParams(top_n=5, max_positions=3, extend_past_top_n=True, max_rank=100)
    ctx, intents = await _run(data, cats, params)
    assert [i.symbol_id for i in intents] == [2, 7, 9]
    # chunk 1 (1..5) gave one pass; chunk 2 (6..10) filled the other two; chunk 3 never evaluated
    assert [r.rank for r in ctx.candidates] == list(range(1, 11))
    assert cats.requested == [[2], [7, 9]]
    by_rank = {r.rank: r for r in ctx.candidates}
    assert by_rank[7].data["rank"] == 7 and by_rank[7].passed
    assert by_rank[6].reject_reason == "bearish_candle"
    # ranks are global, and each evaluated record is a full one
    assert all(r.symbol_id == r.rank for r in ctx.candidates)
    assert all(r.data["catalyst"] is not None for r in ctx.candidates if r.reject_reason is None or r.passed)
    note = next(n for n in ctx.notes if n.message == "orb: ranked")
    assert note.data["evaluated_through"] == 10 and note.data["ranked"] == 10
    _explained(ctx, params)


async def test_on_lower_rank_once_full_within_a_chunk() -> None:
    data, cats = _ranked_universe(20, good={3, 6, 7, 8})
    params = OrbSipParams(top_n=5, max_positions=2, extend_past_top_n=True)
    ctx, intents = await _run(data, cats, params)
    assert [i.symbol_id for i in intents] == [3, 6]
    by_rank = {r.rank: r for r in ctx.candidates}
    assert by_rank[7].reject_reason == "lower_rank" and by_rank[8].reject_reason == "lower_rank"
    assert by_rank[7].data["catalyst"] is not None  # its chunk's lookup covered it
    assert [r.rank for r in ctx.candidates] == list(range(1, 11)) and cats.requested == [[3], [6, 7, 8]]
    _explained(ctx, params)


async def test_on_stops_when_chunk_one_fills_the_slots() -> None:
    data, cats = _ranked_universe(30, good={1, 2, 20})
    params = OrbSipParams(top_n=5, max_positions=1, extend_past_top_n=True)
    ctx, intents = await _run(data, cats, params)
    assert [i.symbol_id for i in intents] == [1]
    assert [r.rank for r in ctx.candidates] == [1, 2, 3, 4, 5]
    assert cats.requested == [[1, 2]]  # no lookups for later chunks
    assert {r.rank: r.reject_reason for r in ctx.candidates}[2] == "lower_rank"


async def test_on_stops_at_max_rank() -> None:
    data, cats = _ranked_universe(40, good={13, 30})
    params = OrbSipParams(top_n=5, max_positions=2, extend_past_top_n=True, max_rank=13)
    ctx, intents = await _run(data, cats, params)
    assert [i.symbol_id for i in intents] == [13]
    assert [r.rank for r in ctx.candidates] == list(range(1, 14))  # the last chunk is cut at max_rank
    assert cats.requested == [[13]]


async def test_on_stops_when_the_list_is_exhausted() -> None:
    data, cats = _ranked_universe(8, good={8})
    data.add(99, "LOW", bar("21.00", "21.50", "20.90", "21.40", 500))  # rvol 0.5: never ranked
    params = OrbSipParams(top_n=3, max_positions=3, extend_past_top_n=True)
    ctx, intents = await _run(data, cats, params)
    assert [i.symbol_id for i in intents] == [8]
    assert [r.rank for r in ctx.candidates] == list(range(1, 9))


async def test_on_keeps_held_and_catalyst_rules_in_later_chunks() -> None:
    data, cats = _ranked_universe(
        10, good={4, 5, 6, 7}, extra_cats={5: FakeCatalyst(direction="bearish"), 6: FakeCatalyst(quality=1)}
    )
    params = OrbSipParams(top_n=3, max_positions=3, extend_past_top_n=True)
    ctx, intents = await _run(data, cats, params, positions=[position(100, 4)])
    by_rank = {r.rank: r.reject_reason for r in ctx.candidates}
    assert by_rank[4] == "already_held"
    assert by_rank[5] == "catalyst_bearish" and by_rank[6] == "catalyst_low_quality"
    assert [i.symbol_id for i in intents] == [7]  # one slot used by the held position
    _explained(ctx, params)


def test_max_rank_validation() -> None:
    with pytest.raises(ValidationError, match="max_rank"):
        OrbSipParams(top_n=20, max_rank=10, extend_past_top_n=True)
    assert OrbSipParams(top_n=20, max_rank=10).max_rank == 10  # only checked when extending
    with pytest.raises(ValidationError):
        OrbSipParams(max_rank=0)
    with pytest.raises(ValidationError):
        OrbSipParams(max_rank=1001)


# --- C: the paper doji rule ---------------------------------------------------------------------------------
def test_doji_with_zero_body_pct_is_only_open_equals_close() -> None:
    zero = Decimal("0")
    assert is_doji(bar("21.00", "21.50", "20.90", "21.00", 100), zero)  # open == close
    assert is_doji(bar("21.00", "21.00", "21.00", "21.00", 100), zero)  # zero range
    assert not is_doji(bar("21.00", "21.50", "20.90", "21.01", 100), zero)  # 1-cent bullish body
    assert not is_doji(bar("21.01", "21.50", "20.90", "21.00", 100), zero)  # 1-cent bearish body


async def test_strategy_with_zero_doji_pct_trades_a_one_cent_body() -> None:
    data = FakeData()
    data.add(1, "AAA", bar("21.00", "21.50", "20.90", "21.01", 9000))
    params = OrbSipParams(doji_body_pct_max=Decimal("0"))
    ctx, intents = await _run(data, FakeCatalysts({1: FakeCatalyst()}), params)
    assert [i.symbol_id for i in intents] == [1]
    assert ctx.candidates[0].data["direction"] == "bullish"
    ctx, intents = await _run(data, FakeCatalysts({1: FakeCatalyst()}), OrbSipParams())
    assert intents == [] and ctx.candidates[0].reject_reason == "doji"  # the default 10% still says doji


# --- D (CATWIDE-b): the walk's stop reason, time limit and a failing later lookup ---------------------------
def _ranked_note(ctx: StrategyContext) -> dict[str, Any]:
    return next(n.data for n in ctx.notes if n.message == "orb: ranked")


class SlowCatalysts(FakeCatalysts):
    """Each lookup advances the strategy context's (fake) clock."""

    def __init__(self, by_symbol: dict[int, FakeCatalyst], step: timedelta) -> None:
        super().__init__(by_symbol)
        self.step = step
        self.clock: FixedClock | None = None

    async def get(self, symbol_ids: Sequence[int], session_date: date) -> dict[int, FakeCatalyst]:
        assert self.clock is not None
        self.clock.advance(self.step)
        return await super().get(symbol_ids, session_date)


class FailingCatalysts(FakeCatalysts):
    def __init__(self, by_symbol: dict[int, FakeCatalyst], fail_on: int) -> None:
        super().__init__(by_symbol)
        self.fail_on = fail_on

    async def get(self, symbol_ids: Sequence[int], session_date: date) -> dict[int, FakeCatalyst]:
        if len(self.requested) + 1 == self.fail_on:
            self.requested.append(list(symbol_ids))
            raise RuntimeError("database went away")
        return await super().get(symbol_ids, session_date)


async def _run_slow(
    n: int, good: set[int], params: OrbSipParams, step: timedelta
) -> tuple[StrategyContext, list[EnterLong], SlowCatalysts]:
    data, plain = _ranked_universe(n, good)
    cats = SlowCatalysts(plain.by_symbol, step)
    strategy = OrbSip(params)
    ctx = make_ctx(data, params, cats)
    assert isinstance(ctx.clock, FixedClock)
    cats.clock = ctx.clock
    event = next(e for e in strategy.schedule(CAL) if e.key == ORB_EVENT)
    intents = await strategy.on_event(ctx, event)
    return ctx, [i for i in intents if isinstance(i, EnterLong)], cats


def test_extend_max_seconds_default_and_bounds() -> None:
    assert OrbSipParams().extend_max_seconds == 60
    assert OrbSipParams(extend_max_seconds=0).extend_max_seconds == 0
    assert OrbSipParams(extend_max_seconds=600).extend_max_seconds == 600
    for bad in (-1, 601):
        with pytest.raises(ValidationError):
            OrbSipParams(extend_max_seconds=bad)
    old = OrbSipParams().model_dump(mode="json")
    old.pop("extend_max_seconds")
    assert OrbSipParams.model_validate(old) == OrbSipParams()


@pytest.mark.parametrize(
    ("n", "good", "max_rank", "max_positions", "stopped", "through"),
    [
        (30, {2, 7}, 100, 2, "slots_full", 10),  # filled in chunk 2: chunk 3 never starts
        (30, {2, 9, 10}, 100, 2, "slots_full", 10),  # filled by chunk 2's last rank
        (8, {8}, 100, 3, "list_end", 8),
        (40, {13}, 13, 3, "max_rank", 13),
        (13, {13}, 13, 3, "list_end", 13),  # max_rank == list length: the list ran out
    ],
)
async def test_ranked_note_says_why_the_walk_stopped(
    n: int, good: set[int], max_rank: int, max_positions: int, stopped: str, through: int
) -> None:
    data, cats = _ranked_universe(n, good)
    params = OrbSipParams(top_n=5, max_positions=max_positions, extend_past_top_n=True, max_rank=max_rank)
    ctx, _ = await _run(data, cats, params)
    note = _ranked_note(ctx)
    assert (note["stopped"], note["evaluated_through"]) == (stopped, through)
    assert len(ctx.candidates) == through


async def test_extend_off_ranked_note_unchanged() -> None:
    data, cats = _ranked_universe(30, good={2})
    ctx, _ = await _run(data, cats, OrbSipParams(top_n=5))
    assert set(_ranked_note(ctx)) == {"ranked", "selected"}


async def test_time_limit_stops_before_starting_a_new_chunk() -> None:
    """Each lookup takes 25 s (fake clock): chunk 1 ends at 25 s, chunk 2 starts (25 <= 60) and ends at 50 s,
    chunk 3 starts (50 <= 60) and ends at 75 s, chunk 4 does not start (75 > 60)."""
    params = OrbSipParams(top_n=5, max_positions=10, extend_past_top_n=True)
    ctx, intents, cats = await _run_slow(40, {1, 6, 11, 16, 21}, params, timedelta(seconds=25))
    assert [i.symbol_id for i in intents] == [1, 6, 11]
    assert cats.requested == [[1], [6], [11]]
    assert [r.rank for r in ctx.candidates] == list(range(1, 16))
    note = _ranked_note(ctx)
    assert (note["stopped"], note["evaluated_through"]) == ("time_limit", 15)
    _explained(ctx, params)


async def test_time_limit_never_stops_chunk_one_and_zero_allows_only_chunk_one() -> None:
    params = OrbSipParams(top_n=5, max_positions=10, extend_past_top_n=True, extend_max_seconds=0)
    ctx, intents, cats = await _run_slow(40, {1, 6}, params, timedelta(seconds=120))
    assert [i.symbol_id for i in intents] == [1] and cats.requested == [[1]]
    assert _ranked_note(ctx)["stopped"] == "time_limit"
    # chunk 1 took longer than the limit and still decided every rank in it
    assert [r.rank for r in ctx.candidates] == [1, 2, 3, 4, 5]


async def test_time_limit_with_a_fast_walk_changes_nothing() -> None:
    params = OrbSipParams(top_n=5, max_positions=10, extend_past_top_n=True, extend_max_seconds=0)
    data, cats = _ranked_universe(20, good={1, 6, 11})
    ctx, intents = await _run(data, cats, params)  # the fake clock never moves: 0 s elapsed
    assert [i.symbol_id for i in intents] == [1, 6, 11]
    assert _ranked_note(ctx)["stopped"] == "list_end"


async def test_a_failing_later_lookup_keeps_earlier_entries_and_leaves_its_chunk_unevaluated() -> None:
    data, plain = _ranked_universe(30, good={2, 4, 7, 12})
    cats = FailingCatalysts(plain.by_symbol, fail_on=2)
    params = OrbSipParams(top_n=5, max_positions=5, extend_past_top_n=True)
    ctx, intents = await _run(data, cats, params)
    assert [i.symbol_id for i in intents] == [2, 4]
    # chunk 2 (6..10) is not recorded at all, chunk 3 never starts
    assert [r.rank for r in ctx.candidates] == [1, 2, 3, 4, 5]
    assert cats.requested == [[2, 4], [7]]
    errors = [n for n in ctx.notes if n.level == "error"]
    assert len(errors) == 1
    assert errors[0].message == "orb: catalyst lookup failed; the walk stops here"
    assert errors[0].data["first_rank"] == 6 and errors[0].data["last_rank"] == 10
    assert errors[0].data["entries_kept"] == 2
    assert "RuntimeError: database went away" in errors[0].data["error"]
    note = _ranked_note(ctx)
    assert (note["stopped"], note["evaluated_through"], note["selected"]) == (
        "lookup_failed",
        5,
        ["R002", "R004"],
    )
    _explained(ctx, params)


async def test_a_failing_chunk_one_lookup_still_fails_the_scan() -> None:
    data, plain = _ranked_universe(30, good={2, 7})
    cats = FailingCatalysts(plain.by_symbol, fail_on=1)
    params = OrbSipParams(top_n=5, max_positions=5, extend_past_top_n=True)
    with pytest.raises(RuntimeError, match="database went away"):
        await _run(data, cats, params)
    with pytest.raises(RuntimeError, match="database went away"):
        await _run(data, FailingCatalysts(plain.by_symbol, fail_on=1), OrbSipParams(top_n=5))
