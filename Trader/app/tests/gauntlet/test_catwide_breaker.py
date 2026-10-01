"""CATWIDE breaker (checker lane, 73dc0c5): orb_sip's bearish-only catalyst rule, the walk past top_n
(extend_past_top_n / max_rank) and the paper doji rule (doji_body_pct_max = 0), with the live parameters
(require_catalyst false, reject_bearish_catalyst true, extend_past_top_n true, max_rank 100, top_n 20,
max_positions 10).

Covered: global rank numbers and no duplicate candidates across chunks; slots already partly used (entries
today, open positions, working entries, held or working names deep in the list); passes always outrank
`lower_rank`; the walk stops when the slots fill, at max_rank (also when not a multiple of top_n) or when
the list runs out; each chunk looks up only its own screen survivors; extend with max_rank == top_n equals
the old behaviour; explain_orb's first_failure equals the stored reject reason on a randomised universe;
the zero-body doji rule on zero-range and malformed bars; the real CatalystService (budget spent, Claude
failing) inside the walk; replay `stored` catalysts past rank 20.

Known gap pinned as a strict xfail (it flips to XPASS, and fails, when fixed):
- a catalyst lookup that raises in a later chunk discards the entries chunks already decided.
"""

import random
from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from tests.factories import add_symbol
from tests.gauntlet.test_p2_t12_breaker import FakeClient, FakeHeadlines, classifier
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
    working_entry,
)
from trader.adapters.claude.catalyst import CatalystService, CatalystStore
from trader.db import models as m
from trader.decisions.orb_explain import explain_orb, first_failure
from trader.market.clock import FixedClock
from trader.market.indicators import is_doji
from trader.replay.catalysts import ReplayCatalysts
from trader.strategies.base import EnterLong, StrategyContext
from trader.strategies.orb_sip import ORB_EVENT, OrbSip, OrbSipParams

LIVE = OrbSipParams(
    require_catalyst=False,
    reject_bearish_catalyst=True,
    extend_past_top_n=True,
    max_rank=100,
    doji_body_pct_max=Decimal("0"),
    max_positions=10,
)

BULL = ("21.00", "21.50", "20.90", "21.40")
BEAR = ("21.00", "21.50", "20.50", "20.60")


def _vol(rank: int) -> int:
    return 100_000 - rank * 10  # distinct rvol: rank order == symbol id order


async def _run(
    data: FakeData, cats: Any, params: OrbSipParams, **ctx_kw: Any
) -> tuple[StrategyContext, list[EnterLong]]:
    strategy = OrbSip(params)
    ctx = make_ctx(data, params, cats, **ctx_kw)
    event = next(e for e in strategy.schedule(CAL) if e.key == ORB_EVENT)
    intents = await strategy.on_event(ctx, event)
    return ctx, [i for i in intents if isinstance(i, EnterLong)]


def _universe(n: int, good: set[int]) -> FakeData:
    data = FakeData()
    for r in range(1, n + 1):
        o, h, lo, c = BULL if r in good else BEAR
        data.add(r, f"R{r:03d}", bar(o, h, lo, c, _vol(r)))
    return data


def _consistent(ctx: StrategyContext, params: OrbSipParams) -> None:
    for rec in ctx.candidates:
        checks = explain_orb(
            rec.data, rec.candle, params, catalyst=rec.data.get("catalyst"), reject_reason=rec.reject_reason
        )
        assert first_failure(checks) == rec.reject_reason, (rec.data["ticker"], rec.reject_reason, checks)


def _invariants(ctx: StrategyContext, intents: Sequence[EnterLong], slots: int, cats: FakeCatalysts) -> None:
    ranks = [r.rank for r in ctx.candidates]
    assert ranks == list(range(1, len(ranks) + 1))  # global, contiguous, in order
    sids = [r.symbol_id for r in ctx.candidates]
    assert len(sids) == len(set(sids))  # no duplicate candidate
    assert all(r.data["rank"] == r.rank for r in ctx.candidates)
    passed = [r for r in ctx.candidates if r.passed]
    assert [r.symbol_id for r in passed] == [i.symbol_id for i in intents]
    assert len(intents) <= slots
    lower = [r.rank for r in ctx.candidates if r.reject_reason == "lower_rank"]
    if lower:
        assert len(intents) == slots and max(r.rank for r in passed) < min(lower)
    # each lookup is one chunk's survivors: disjoint, never a non-survivor
    looked = [sid for chunk in cats.requested for sid in chunk]
    assert len(looked) == len(set(looked))
    for sid in looked:
        rec = next(r for r in ctx.candidates if r.symbol_id == sid)
        assert rec.reject_reason in (None, "lower_rank", "catalyst_bearish"), rec.reject_reason
    # and every screen survivor was looked up (its catalyst is recorded)
    for rec in ctx.candidates:
        if rec.passed or rec.reject_reason in ("lower_rank", "catalyst_bearish"):
            assert rec.symbol_id in looked


# --- chunking and slots ------------------------------------------------------------------------------------
async def test_live_params_slots_partly_used_by_entries_today() -> None:
    data = _universe(150, good={5, 33, 41, 62, 64, 99, 120})
    cats = FakeCatalysts({s: FakeCatalyst() for s in range(1, 151)})
    ctx, intents = await _run(data, cats, LIVE, entries_today=7)  # 3 slots left
    assert [i.symbol_id for i in intents] == [5, 33, 41]
    assert [r.rank for r in ctx.candidates][-1] == 60  # chunk 3 (41..60) filled the last slot
    assert cats.requested == [[5], [33], [41]]
    _invariants(ctx, intents, 3, cats)
    _consistent(ctx, LIVE)


async def test_held_and_working_names_deep_in_the_list_count_and_are_skipped() -> None:
    data = _universe(100, good={3, 25, 30, 45, 47, 80})
    cats = FakeCatalysts({s: FakeCatalyst() for s in range(1, 101)})
    params = LIVE.model_copy(update={"max_positions": 5})
    # 2 open positions (rank 25 among them) + 1 working entry on rank 30: 2 slots left
    ctx, intents = await _run(
        data,
        cats,
        params,
        positions=[position(1, 25), position(2, 777)],
        orders=[working_entry(9, 30)],
        entries_today=1,
    )
    by = {r.rank: r.reject_reason for r in ctx.candidates}
    assert by[25] == "already_held" and by[30] == "entry_working"
    assert [i.symbol_id for i in intents] == [3, 45]
    assert cats.requested == [[3], [45, 47]]
    assert by[47] == "lower_rank"
    _invariants(ctx, intents, 2, cats)
    _consistent(ctx, params)


async def test_slots_full_exactly_at_a_chunk_boundary_stops_the_walk() -> None:
    data = _universe(100, good={1, 20, 21})
    cats = FakeCatalysts({s: FakeCatalyst() for s in range(1, 101)})
    params = LIVE.model_copy(update={"max_positions": 2})
    ctx, intents = await _run(data, cats, params)
    assert [i.symbol_id for i in intents] == [1, 20]
    assert len(ctx.candidates) == 20 and cats.requested == [[1, 20]]
    note = next(n for n in ctx.notes if n.message == "orb: ranked")
    assert note.data["evaluated_through"] == 20


async def test_max_rank_not_a_multiple_of_top_n_and_names_past_it_never_evaluated() -> None:
    data = _universe(80, good={50, 51})
    cats = FakeCatalysts({s: FakeCatalyst() for s in range(1, 81)})
    params = LIVE.model_copy(update={"max_rank": 50})
    ctx, intents = await _run(data, cats, params)
    assert [i.symbol_id for i in intents] == [50]
    assert len(ctx.candidates) == 50 and cats.requested == [[50]]


async def test_extend_with_max_rank_equal_top_n_is_the_old_behaviour() -> None:
    rng = random.Random(7)
    for _ in range(20):
        good = {r for r in range(1, 61) if rng.random() < 0.3}
        data = _universe(60, good)
        catmap = {s: FakeCatalyst(direction=rng.choice(["bullish", "bearish", "neutral"])) for s in good}
        old_p = OrbSipParams(require_catalyst=False, reject_bearish_catalyst=True, max_positions=10)
        new_p = old_p.model_copy(update={"extend_past_top_n": True, "max_rank": 20})
        old_ctx, old_i = await _run(data, FakeCatalysts(dict(catmap)), old_p)
        new_ctx, new_i = await _run(data, FakeCatalysts(dict(catmap)), new_p)
        assert [(r.symbol_id, r.reject_reason, r.passed) for r in old_ctx.candidates] == [
            (r.symbol_id, r.reject_reason, r.passed) for r in new_ctx.candidates
        ]
        assert old_i == new_i


async def test_worst_case_lookups_every_survivor_bearish() -> None:
    """Every survivor classified bearish: the walk never fills a slot, so it reaches max_rank and looks up
    every survivor, one call per chunk (5 calls, up to 100 names, for the live params)."""
    data = _universe(150, good=set(range(1, 151)))
    cats = FakeCatalysts({s: FakeCatalyst(direction="bearish") for s in range(1, 151)})
    ctx, intents = await _run(data, cats, LIVE)
    assert intents == []
    assert len(cats.requested) == 5 and sum(len(c) for c in cats.requested) == 100
    assert {r.reject_reason for r in ctx.candidates} == {"catalyst_bearish"}
    _consistent(ctx, LIVE)


@pytest.mark.parametrize("seed", range(40))
async def test_randomised_universe_explain_matches_and_invariants_hold(seed: int) -> None:
    rng = random.Random(seed)
    n = rng.randint(1, 160)
    data = FakeData()
    catmap: dict[int, FakeCatalyst] = {}
    for r in range(1, n + 1):
        kind = rng.choice(["bull", "bull", "bull", "bear", "doji0", "zero", "malformed", "tiny"])
        o, h, lo, c = {
            "bull": BULL,
            "bear": BEAR,
            "doji0": ("21.00", "21.50", "20.90", "21.00"),
            "zero": ("21.00", "21.00", "21.00", "21.00"),
            "malformed": ("21.00", "20.00", "21.50", "21.40"),
            "tiny": ("21.00", "21.50", "20.90", "21.0001"),
        }[kind]
        price = rng.choice(["20", "20", "20", "3", "80"])  # price only matters through the bar close here
        if price != "20":
            o, h, lo, c = (str(Decimal(x) * Decimal(price) / 20) for x in (o, h, lo, c))
        data.add(
            r,
            f"S{r:03d}",
            bar(o, h, lo, c, _vol(r)),
            atr=rng.choice(["1.00", "1.00", "0.10", None]),
            avg_volume=rng.choice([2_000_000, 2_000_000, 10, None]),
        )
        roll = rng.random()
        if roll < 0.2:
            catmap[r] = FakeCatalyst(direction="bearish")
        elif roll < 0.3:
            catmap[r] = FakeCatalyst(direction="bearish", classified=False)
        elif roll < 0.4:
            catmap[r] = FakeCatalyst(catalyst_type="none", direction="neutral", quality=None)
        elif roll < 0.7:
            catmap[r] = FakeCatalyst()
    cats = FakeCatalysts(catmap)
    max_positions = rng.randint(1, 10)
    params = LIVE.model_copy(update={"max_positions": max_positions, "top_n": rng.choice([5, 20])})
    held = [
        position(i + 1, s) for i, s in enumerate(rng.sample(range(1, n + 1), k=min(n, rng.randint(0, 2))))
    ]
    entries_today = rng.randint(0, 2)
    ctx, intents = await _run(data, cats, params, positions=held, entries_today=entries_today)
    used = max(entries_today, len(held))
    slots = max_positions - used
    if slots <= 0:
        assert intents == [] and ctx.candidates == []
        return
    _invariants(ctx, intents, slots, cats)
    _consistent(ctx, params)
    # the walk stops only when the slots are full, at max_rank, or when the ranked list runs out
    if len(intents) < slots:
        assert len(ctx.candidates) == min(n, params.max_rank)


# --- a lookup failing in a later chunk ---------------------------------------------------------------------
class FailingOnCall(FakeCatalysts):
    def __init__(self, by_symbol: dict[int, FakeCatalyst], fail_on: int) -> None:
        super().__init__(by_symbol)
        self.fail_on = fail_on

    async def get(self, symbol_ids: Sequence[int], session_date: date) -> dict[int, FakeCatalyst]:
        if len(self.requested) + 1 == self.fail_on:
            self.requested.append(list(symbol_ids))
            raise RuntimeError("database went away")
        return await super().get(symbol_ids, session_date)


@pytest.mark.xfail(
    strict=True,
    reason="CATWIDE should-fix: a lookup raising in chunk 2+ discards chunk 1's already-decided entries",
)
async def test_a_failing_later_chunk_keeps_the_entries_already_decided() -> None:
    data = _universe(60, good={2, 30})
    cats = FailingOnCall({s: FakeCatalyst() for s in range(1, 61)}, fail_on=2)
    ctx, intents = await _run(data, cats, LIVE)
    assert [i.symbol_id for i in intents] == [2]


# --- the paper doji rule -----------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("ohlc", "reason"),
    [
        (("21.00", "21.50", "20.90", "21.00"), "doji"),  # open == close
        (("21.00", "21.00", "21.00", "21.00"), "doji"),  # zero range
        (("21.00", "20.00", "21.50", "21.40"), "malformed_bar"),  # high < low
        (("21.00", "21.50", "20.90", "21.0001"), None),  # the smallest bullish body trades
        (("21.0001", "21.50", "20.90", "21.00"), "bearish_candle"),
    ],
)
async def test_zero_doji_rule_on_edge_bars(ohlc: tuple[str, str, str, str], reason: str | None) -> None:
    data = FakeData()
    data.add(1, "AAA", bar(*ohlc, 9000))
    params = LIVE
    ctx, intents = await _run(data, FakeCatalysts({1: FakeCatalyst()}), params)
    (rec,) = ctx.candidates
    assert rec.reject_reason == reason
    assert bool(intents) is (reason is None)
    _consistent(ctx, params)


def test_is_doji_zero_boundary() -> None:
    zero = Decimal("0")
    assert is_doji(bar("5", "6", "4", "5", 1), zero)
    assert not is_doji(bar("5", "6", "4", "5.0001", 1), zero)
    with pytest.raises(ValueError):
        is_doji(bar("5", "4", "6", "5", 1), zero)


# --- the real catalyst service inside the walk (database) --------------------------------------------------
def _symbols(factory: sessionmaker[Session], n: int) -> list[int]:
    with factory() as s:
        ids = [add_symbol(s, f"W{i:03d}") for i in range(1, n + 1)]
        s.commit()
    return ids


def _data_for(ids: Sequence[int], good: set[int]) -> FakeData:
    data = FakeData()
    for r, sid in enumerate(ids, start=1):
        o, h, lo, c = BULL if r in good else BEAR
        data.add(sid, f"W{r:03d}", bar(o, h, lo, c, _vol(r)))
    return data


@pytest.mark.db
async def test_budget_spent_names_pass_unclassified_and_claude_is_not_called(
    db_factory: sessionmaker[Session],
) -> None:
    """claude.daily_budget_usd reached: every survivor is stored `unknown` and, under bearish-only, PASSES
    (no catalyst filter at all). The headlines are still fetched for each name (one FinViz page each, >= 2 s
    apart live): the budget is checked only after the fetch."""
    ids = _symbols(db_factory, 45)
    data = _data_for(ids, good={5, 25, 41})
    client = FakeClient()
    heads = FakeHeadlines()
    clock = FixedClock(NOW)
    svc = CatalystService(
        db_factory,
        clock,
        CatalystStore(db_factory, clock),
        classifier(client, claude_daily_budget_usd=Decimal("0")),
        heads,
    )
    ctx, intents = await _run(data, svc, LIVE)
    assert [i.symbol_id for i in intents] == [ids[4], ids[24], ids[40]]
    assert client.messages.calls == []
    assert len(heads.calls) == 3  # one per survivor, across three chunks
    for rec in ctx.candidates:
        if rec.passed:
            assert rec.data["catalyst"]["classified"] is False
    _consistent(ctx, LIVE)


@pytest.mark.db
async def test_claude_failing_mid_walk_names_pass_unknown(db_factory: sessionmaker[Session]) -> None:
    ids = _symbols(db_factory, 30)
    data = _data_for(ids, good={3, 22})

    def boom() -> Any:
        raise TimeoutError("claude timed out")

    client = FakeClient(boom)
    clock = FixedClock(NOW)
    svc = CatalystService(
        db_factory, clock, CatalystStore(db_factory, clock), classifier(client), FakeHeadlines()
    )
    ctx, intents = await _run(data, svc, LIVE)
    assert [i.symbol_id for i in intents] == [ids[2], ids[21]]
    assert len(client.messages.calls) == 2
    _consistent(ctx, LIVE)


@pytest.mark.db
async def test_replay_stored_catalysts_past_rank_20(db_factory: sessionmaker[Session]) -> None:
    """Replay (`stored`): pre-market rows exist for a few names only; a rank-25 survivor without a row is
    `unknown` and passes under bearish-only; a stored bearish one is still rejected."""
    ids = _symbols(db_factory, 40)
    data = _data_for(ids, good={2, 25, 33})
    now = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
    with db_factory() as s:
        s.add(
            m.Catalyst(
                symbol_id=ids[1],
                session_date=SESSION,
                headlines=[],
                catalyst_type="guidance",
                direction="bearish",
                quality=80,
                cost_usd=Decimal(0),
                classified_at=now,
                created_at=now,
            )
        )
        s.commit()
    ctx, intents = await _run(data, ReplayCatalysts(db_factory, "stored"), LIVE)
    by = {r.rank: r for r in ctx.candidates}
    assert by[2].reject_reason == "catalyst_bearish"
    assert by[25].passed and by[25].data["catalyst"]["type"] == "unknown"
    assert [i.symbol_id for i in intents] == [ids[24], ids[32]]
    _consistent(ctx, LIVE)
