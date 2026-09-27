"""P5-T5: the replay data source (SPEC §8): archive and caches first, Questrade second, no lookahead,
no writes, bounded memory, windowed Questrade requests."""

from collections import Counter
from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from tests.fakes_questrade import FakeQuestrade
from tests.fakes_replay import candle, minute_series, seed_replay_world
from trader.adapters.questrade.client import INTERVAL_LENGTH, MAX_CANDLES_PER_REQUEST, QuestradeApiError
from trader.adapters.questrade.models import CandleRequest
from trader.db import models as m
from trader.db.session import session_scope
from trader.jobs.nightly import MIN_OPENING_BARS, NightlyDeps, _fetch_chunk
from trader.market.calendar import SessionCalendar
from trader.market.clock import ET, Clock, FixedClock
from trader.market.indicators import atr, average_volume
from trader.market.types import Candle, Interval, OpenBarStats
from trader.replay.data import BIASED_SOURCE, ReplayData
from trader.replay.types import ReplayMarket
from trader.settings_store import RuntimeSettings
from trader.strategies.spy_overlay import SpyOverlay

pytestmark = pytest.mark.db
CAL = SessionCalendar()
D1 = date(2026, 11, 17)
D2 = date(2026, 11, 18)
WALL = FixedClock(datetime(2026, 11, 28, 23, 0, tzinfo=UTC))
RUN = 4242
Q4 = Decimal("0.0001")


def et(d: date, hh: int, mm: int, ss: int = 0) -> datetime:
    return datetime.combine(d, time(hh, mm, ss), tzinfo=ET).astimezone(UTC)


def c5(d: date, o: str = "10.00", volume: int = 5000) -> Candle:
    """The 09:30 opening 5-minute bar of session `d`."""
    return candle(CAL.session_open(d), o, "10.50", "9.90", "10.20", volume, minutes=5)


def day_bar(d: date, close: str, *, rng: str = "1.00", volume: int = 1_000_000) -> Candle:
    start = datetime.combine(d, time(0), tzinfo=ET)
    c = Decimal(close)
    return Candle(start, start + timedelta(days=1), c, c + Decimal(rng), c - Decimal(rng), c, volume, None)


class RecordingQuestrade(FakeQuestrade):
    """FakeQuestrade that records every candle request and enforces the client's window limit."""

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[CandleRequest] = []

    def _record(self, r: CandleRequest) -> None:
        assert (r.end - r.start) / INTERVAL_LENGTH[r.interval] <= MAX_CANDLES_PER_REQUEST
        self.requests.append(r)

    async def candles(
        self, symbol_id: int, start: datetime, end: datetime, interval: Interval
    ) -> list[Candle]:
        self._record(CandleRequest(symbol_id, start, end, interval))
        return await super().candles(symbol_id, start, end, interval)

    async def candles_many(
        self, reqs: Sequence[CandleRequest]
    ) -> dict[CandleRequest, list[Candle] | QuestradeApiError]:
        for r in reqs:
            self._record(r)
        return await super().candles_many(reqs)

    def of(self, interval: Interval) -> list[CandleRequest]:
        return [r for r in self.requests if r.interval == interval]


def make_data(
    factory: sessionmaker[Session],
    clock: Clock,
    client: FakeQuestrade | None = None,
    *,
    wall: Clock = WALL,
    date_from: date = D1,
    date_to: date = D2,
    window_days: int = 85,
    lookback: int = 14,
) -> ReplayData:
    return ReplayData(
        factory,
        clock,
        wall,
        CAL,
        client,
        run_id=RUN,
        date_from=date_from,
        date_to=date_to,
        half_spread_bps=Decimal("5"),
        questrade_window_days=window_days,
        lookback_sessions=lookback,
    )


def add_intraday(factory: sessionmaker[Session], sid: int, interval: str, bars: Sequence[Candle]) -> None:
    with session_scope(factory) as s:
        for b in bars:
            s.add(
                m.IntradayCandle(
                    symbol_id=sid,
                    interval=interval,
                    ts=b.start,
                    open=b.open,
                    high=b.high,
                    low=b.low,
                    close=b.close,
                    volume=b.volume,
                    vwap=b.vwap,
                )
            )


def table_counts(factory: sessionmaker[Session]) -> dict[str, int]:
    with factory() as s:
        return {
            t.name: s.execute(select(func.count()).select_from(t)).scalar_one()
            for t in m.Base.metadata.sorted_tables
        }


def replay_events(factory: sessionmaker[Session]) -> list[m.EventLog]:
    with factory() as s:
        return list(s.execute(select(m.EventLog).order_by(m.EventLog.id)).scalars())


def test_satisfies_replay_market(db_factory: sessionmaker[Session]) -> None:
    market: ReplayMarket = make_data(db_factory, FixedClock(et(D1, 9, 0)))
    assert market.biased_days == frozenset()
    assert market.progress_counts() == {
        "missing_opening_bars": 0,
        "missing_minute_bars": 0,
        "questrade_requests": 0,
    }


# 1 ----------------------------------------------------------------------------------------------------------
async def test_stored_universe_and_stats_and_biased_day(db_factory: sessionmaker[Session]) -> None:
    w = seed_replay_world(
        db_factory,
        universe_days=[D1],
        stats={("SPY", D1): ("1000", "2.5"), ("AAA", D1): ("2000", "1.1"), ("BBB", D1): (None, "0.9")},
        strategies=False,
    )
    clock = FixedClock(et(D1, 9, 0))
    rd = make_data(db_factory, clock)

    await rd.prepare_day(D1)
    uni = await rd.universe(D1)
    assert [(u.ticker, u.source, u.price, u.avg_volume, u.atr14) for u in uni] == [
        (t, "finviz", Decimal("20"), 2_000_000, Decimal("1.0")) for t in ("AAA", "BBB", "SPY")
    ]
    assert [u.symbol_id for u in uni] == [w.symbols[t] for t in ("AAA", "BBB", "SPY")]
    stats = await rd.open_bar_stats(D1)
    assert stats == {
        w.symbols["SPY"]: OpenBarStats(w.symbols["SPY"], Decimal("1000"), Decimal("2.5")),
        w.symbols["AAA"]: OpenBarStats(w.symbols["AAA"], Decimal("2000"), Decimal("1.1")),
        w.symbols["BBB"]: OpenBarStats(w.symbols["BBB"], None, Decimal("0.9")),
    }
    assert (await rd.universe_status(D1)).source == "finviz"
    assert rd.biased_days == frozenset()

    clock.set(et(D2, 9, 0))
    await rd.prepare_day(D2)
    biased = await rd.universe(D2)
    assert [u.symbol_id for u in biased] == [u.symbol_id for u in uni]
    assert {u.source for u in biased} == {BIASED_SOURCE}
    status = await rd.universe_status(D2)
    assert (status.source, status.fallback_from, status.stale, status.age_sessions) == (
        "biased",
        None,
        False,
        None,
    )
    assert rd.biased_days == frozenset({D2})
    assert rd.progress_counts()["missing_opening_bars"] == 6  # offline, nothing archived: 3 per day


# 2 ----------------------------------------------------------------------------------------------------------
async def test_opening_bar_sources_archive_then_cache_then_questrade(
    db_factory: sessionmaker[Session],
) -> None:
    arch = c5(D1, "11.00")
    w = seed_replay_world(db_factory, universe_days=[D1], archive={("AAA", "5m"): [arch]}, strategies=False)
    spy, aaa, bbb = w.symbols["SPY"], w.symbols["AAA"], w.symbols["BBB"]
    cached = c5(D1, "12.00")
    add_intraday(db_factory, bbb, "5m", [cached])
    fq = RecordingQuestrade()
    from_qt = c5(D1, "13.00")
    fq.add_bars(
        1000, "FiveMinutes", [from_qt, candle(CAL.session_open(D1) + timedelta(minutes=5), 1, 1, 1, 1)]
    )
    fq.add_bars(1001, "FiveMinutes", [c5(D1, "99.00")])  # never used: the archive wins
    fq.add_bars(1002, "FiveMinutes", [c5(D1, "98.00")])  # never used: the cache wins

    clock = FixedClock(et(D1, 9, 0))
    rd = make_data(db_factory, clock, fq, date_to=D1, lookback=2)
    await rd.prepare_day(D1)
    clock.set(et(D1, 9, 40))
    ob = await rd.opening_bars(D1)
    assert ob.bars == {spy: from_qt, aaa: arch, bbb: cached}
    assert ob.missing == {}
    # one FiveMinutes request per symbol for the whole range (the look-back sessions were missing too)
    assert Counter(r.symbol_id for r in fq.of("FiveMinutes")) == {1000: 1, 1001: 1, 1002: 1}
    first, last_lookback = CAL.sessions_before(D1, 2)
    # the requested range spans the sessions still missing: SPY's whole range, the others' look-back only
    assert {r.symbol_id: (r.start, r.end) for r in fq.of("FiveMinutes")} == {
        1000: (CAL.session_open(first), CAL.session_close(D1)),
        1001: (CAL.session_open(first), CAL.session_close(last_lookback)),
        1002: (CAL.session_open(first), CAL.session_close(last_lookback)),
    }
    await rd.opening_bars(D1)
    assert len(fq.of("FiveMinutes")) == 3  # fetched once per symbol

    # offline: no client, the Questrade-only bar is missing
    off = make_data(db_factory, FixedClock(et(D1, 9, 40)), None, date_to=D1, lookback=2)
    await off.prepare_day(D1)
    ob_off = await off.opening_bars(D1)
    assert ob_off.bars == {aaa: arch, bbb: cached}
    assert ob_off.missing == {spy: "no_archived_bar"}

    # outside the Questrade window (measured from the wall clock): never fetched
    fq2 = RecordingQuestrade()
    fq2.add_bars(1000, "FiveMinutes", [from_qt])
    far = FixedClock(datetime(2027, 6, 1, 23, 0, tzinfo=UTC))
    old = make_data(db_factory, FixedClock(et(D1, 9, 40)), fq2, wall=far, date_to=D1, lookback=2)
    await old.prepare_day(D1)
    ob_old = await old.opening_bars(D1)
    assert ob_old.missing == {spy: "no_archived_bar"}
    assert fq2.requests == []


# 3 ----------------------------------------------------------------------------------------------------------
async def test_no_lookahead(db_factory: sessionmaker[Session]) -> None:
    open_ = CAL.session_open(D1)
    minutes = minute_series(open_, [str(Decimal("10.00") + Decimal(i) / 100) for i in range(60)])
    w = seed_replay_world(
        db_factory,
        universe_days=[D1],
        archive={("AAA", "5m"): [c5(D1)], ("AAA", "1m"): minutes},
        strategies=False,
    )
    aaa = w.symbols["AAA"]
    clock = FixedClock(et(D1, 9, 0))
    rd = make_data(db_factory, clock, None, date_to=D1, lookback=2)
    await rd.prepare_day(D1)

    clock.set(et(D1, 9, 34, 59))
    early = await rd.opening_bars(D1, [aaa])
    assert early.bars == {}
    assert early.missing == {aaa: "bar_not_complete"}
    clock.set(et(D1, 9, 35, 4))
    assert (await rd.opening_bars(D1, [aaa])).bars == {aaa: c5(D1)}

    clock.set(et(D1, 10, 0))
    bars = await rd.candles(aaa, open_, CAL.session_close(D1), "OneMinute")
    assert len(bars) == 30
    assert bars[-1].end == et(D1, 10, 0)
    assert all(b.end <= clock.now() for b in bars)
    assert await rd.candles(aaa, open_, CAL.session_close(D1), "FiveMinutes") == [c5(D1)]

    clock.set(et(D1, 10, 0, 30))
    q = (await rd.quotes([aaa]))[aaa]
    assert q.last == minutes[29].close
    assert q.last_trade_time == et(D1, 10, 0)

    # runner helpers never return a bar ending after `at`
    assert rd.bar_ending_at(aaa, et(D1, 10, 0)) == minutes[29]
    assert rd.bar_ending_at(aaa, et(D1, 10, 0, 30)) is None
    assert rd.last_close(aaa, et(D1, 10, 0, 30)) == minutes[29].close
    assert rd.last_close(aaa, et(D1, 9, 30)) is None

    # a prior close is the previous session's daily bar only
    assert await rd.prior_close(aaa, D1) is None  # no daily bars stored, offline


# 4 ----------------------------------------------------------------------------------------------------------
async def test_biased_day_stats_equal_the_nightly_formulas(db_factory: sessionmaker[Session]) -> None:
    lookback = CAL.sessions_before(D2, 14)
    opening_aaa = [c5(d, volume=1000 + 37 * i) for i, d in enumerate(lookback)]
    opening_bbb = [c5(d, volume=2000) for d in lookback[-5:]]  # fewer than MIN_OPENING_BARS
    days = [d for d in (D2 - timedelta(days=n) for n in range(45, 0, -1)) if CAL.is_session(d)]
    daily_aaa = [
        day_bar(d, str(Decimal("20") + Decimal(i) / 10), rng=str(Decimal("0.5") + i % 3))
        for i, d in enumerate(days)
    ]
    daily_bbb = [day_bar(d, "30", rng="2") for d in days]
    w = seed_replay_world(
        db_factory,
        universe_days=[D1],  # D2 has no snapshot: biased, and no stats rows
        universe_tickers=["AAA", "BBB"],
        archive={("AAA", "5m"): opening_aaa, ("BBB", "5m"): opening_bbb},
        daily={
            "AAA": [(d, b) for d, b in zip(days, daily_aaa, strict=True)],
            "BBB": [(d, b) for d, b in zip(days, daily_bbb, strict=True)],
        },
        strategies=False,
    )
    aaa, bbb = w.symbols["AAA"], w.symbols["BBB"]

    # the nightly job's own fetch-and-reduce over the same bars
    fq = FakeQuestrade()
    sym_a, sym_b = fq.add_symbol("AAA", 1001), fq.add_symbol("BBB", 1002)
    fq.add_bars(1001, "FiveMinutes", opening_aaa)
    fq.add_bars(1002, "FiveMinutes", opening_bbb)
    fq.add_bars(1001, "OneDay", daily_aaa)
    fq.add_bars(1002, "OneDay", daily_bbb)
    deps = NightlyDeps(db_factory, WALL, CAL, cast(Any, None), fq, RuntimeSettings())
    fetched = await _fetch_chunk(deps, [(sym_a, None), (sym_b, None)], lookback)
    min_bars = min(MIN_OPENING_BARS, len(lookback))
    expected = {
        sid: OpenBarStats(
            sid, average_volume(f.opening) if len(f.opening) >= min_bars else None, atr(f.daily, 14)
        )
        for sid, f in zip((aaa, bbb), fetched, strict=True)
    }
    assert expected[aaa].avg_open_vol_14d is not None
    assert expected[aaa].atr14 is not None
    assert expected[bbb].avg_open_vol_14d is None

    clock = FixedClock(et(D2, 9, 0))
    rd = make_data(db_factory, clock, None, date_from=D2, date_to=D2)
    await rd.prepare_day(D2)
    assert D2 in rd.biased_days
    assert await rd.open_bar_stats(D2) == expected


# 5 ----------------------------------------------------------------------------------------------------------
def _world_questrade() -> RecordingQuestrade:
    fq = RecordingQuestrade()
    for qid in (1000, 1001, 1002):
        for d in CAL.sessions_before(D1, 3) + [D1, D2]:
            fq.add_bars(qid, "FiveMinutes", [c5(d, str(10 + qid - 1000), volume=4000 + qid)])
            fq.add_bars(
                qid, "OneMinute", minute_series(CAL.session_open(d), [str(20 + i) for i in range(40)])
            )
        for d in [d for d in (D1 - timedelta(days=n) for n in range(45, -2, -1)) if CAL.is_session(d)]:
            fq.add_bars(qid, "OneDay", [day_bar(d, str(50 + qid - 1000))])
    return fq


async def _exercise(rd: ReplayData, clock: FixedClock, ids: dict[str, int]) -> list[Any]:
    out: list[Any] = []
    all_ids = sorted(ids.values())
    for d in (D1, D2):
        clock.set(et(d, 9, 0))
        await rd.prepare_day(d)
        out.append(await rd.universe(d))
        out.append(await rd.universe_status(d))
        stats = await rd.open_bar_stats(d)
        assert list(stats) == sorted(stats)
        out.append(stats)
        for at in (et(d, 9, 35, 5), et(d, 9, 50, 30), et(d, 10, 5)):
            clock.set(at)
            ob = await rd.opening_bars(d)
            assert list(ob.bars) == sorted(ob.bars)
            out.append(ob)
            quotes = await rd.quotes(list(reversed(all_ids)))
            assert list(quotes) == sorted(quotes)
            out.append(quotes)
            out.append(await rd.candles(ids["AAA"], CAL.session_open(d), at, "OneMinute"))
        out.append(await rd.prior_close(ids["SPY"], d))
        out.append(await rd.prior_closes(list(reversed(all_ids)), d))
        out.append(await rd.symbol_ids(["SPY", "AAA", "ZZZ"]))
        await rd.load_minute_bars([ids["BBB"]], d)
        out.append(rd.bar_ending_at(ids["BBB"], et(d, 9, 45)))
        out.append(rd.last_close(ids["BBB"], et(d, 9, 45, 30)))
        out.append(dict(rd.progress_counts()))
    out.append(rd.biased_days)
    return out


async def test_determinism(db_factory: sessionmaker[Session]) -> None:
    w = seed_replay_world(db_factory, universe_days=[D1], strategies=False)
    runs = []
    for _ in range(2):
        clock = FixedClock(et(D1, 9, 0))
        rd = make_data(db_factory, clock, _world_questrade(), lookback=3)
        runs.append(await _exercise(rd, clock, w.symbols))
    assert runs[0] == runs[1]
    assert runs[0][-1] == frozenset({D2})


# 6 ----------------------------------------------------------------------------------------------------------
async def test_synthetic_quote_passes_the_overlay_stale_check(db_factory: sessionmaker[Session]) -> None:
    open_ = CAL.session_open(D1)
    closes = [str(Decimal("450.00") + Decimal(i) / 4) for i in range(390)]
    w = seed_replay_world(
        db_factory,
        universe_days=[D1],
        archive={("SPY", "1m"): minute_series(open_, closes)},
        strategies=False,
    )
    spy = w.symbols["SPY"]
    clock = FixedClock(et(D1, 9, 0))
    rd = make_data(db_factory, clock, None, date_to=D1, lookback=2)
    await rd.prepare_day(D1)

    clock.set(et(D1, 15, 30, 5))
    q = (await rd.quotes([spy]))[spy]
    last = Decimal(closes[359])  # the 15:29-15:30 bar
    hs = (last * Decimal("5") / Decimal(10_000)).quantize(Q4, ROUND_HALF_UP)
    assert (q.symbol_id, q.symbol, q.last, q.last_regular) == (spy, "SPY", last, last)
    assert (q.bid, q.ask) == (last - hs, last + hs)
    assert q.bid is not None and q.ask is not None and q.last is not None
    assert q.bid < q.last < q.ask
    assert (q.delay, q.is_halted, q.last_trade_time) == (0, False, et(D1, 15, 30))
    ctx = SimpleNamespace(clock=clock, session_date=D1)
    assert SpyOverlay()._stale_reason(cast(Any, ctx), q) is None


# 7 ----------------------------------------------------------------------------------------------------------
async def test_questrade_error_is_missing_counted_and_warned_once(db_factory: sessionmaker[Session]) -> None:
    w = seed_replay_world(
        db_factory,
        universe_days=[D1],
        stats={(t, D1): ("1000", "1.0") for t in ("SPY", "AAA", "BBB")},
        strategies=False,
    )
    spy, aaa, bbb = w.symbols["SPY"], w.symbols["AAA"], w.symbols["BBB"]
    fq = RecordingQuestrade()
    fq.add_bars(1000, "FiveMinutes", [c5(D1)])
    fq.add_bars(1002, "FiveMinutes", [c5(D1)])
    fq.errors[1001] = 500
    clock = FixedClock(et(D1, 9, 0))
    rd = make_data(db_factory, clock, fq, date_to=D1, lookback=2)

    await rd.prepare_day(D1)
    clock.set(et(D1, 9, 40))
    ob = await rd.opening_bars(D1)
    assert ob.bars == {spy: c5(D1), bbb: c5(D1)}
    assert ob.missing == {aaa: "questrade_error: HTTP 500"}
    assert rd.progress_counts()["missing_opening_bars"] == 1
    await rd.opening_bars(D1)  # asking again neither refetches nor warns again
    events = replay_events(db_factory)
    assert len(events) == 1
    ev = events[0]
    assert (ev.level, ev.run_id, ev.source) == ("warning", RUN, "replay.data")
    assert ev.data["session_date"] == D1.isoformat()
    assert ev.data["errors"] == {str(aaa): "questrade_error: HTTP 500"}


# 8 ----------------------------------------------------------------------------------------------------------
async def test_no_writes_after_a_full_day_of_reads(db_factory: sessionmaker[Session]) -> None:
    w = seed_replay_world(db_factory, universe_days=[D1], strategies=False)
    before = table_counts(db_factory)
    fq = _world_questrade()
    clock = FixedClock(et(D1, 9, 0))
    rd = make_data(db_factory, clock, fq, lookback=3)

    out = await _exercise(rd, clock, w.symbols)

    assert any(r.interval == "FiveMinutes" for r in fq.requests)
    assert any(r.interval == "OneMinute" for r in fq.requests)
    assert any(r.interval == "OneDay" for r in fq.requests)
    assert out[2]  # computed stats for D1 (no stored rows)
    assert table_counts(db_factory) == before


# 10 ---------------------------------------------------------------------------------------------------------
async def test_request_windows_and_bounded_memory(db_factory: sessionmaker[Session]) -> None:
    sessions = CAL.sessions_before(D2, 129) + [D2]
    first, day1, day2 = sessions[0], sessions[0], sessions[1]
    lookback = CAL.sessions_before(first, 14)
    span = lookback + sessions
    w = seed_replay_world(db_factory, universe_days=[D1], tickers=("SPY", "AAA"), strategies=False)
    aaa = w.symbols["AAA"]
    fq = RecordingQuestrade()
    for qid in (1000, 1001):
        for d in span:
            open_ = CAL.session_open(d)
            rest = [
                candle(open_ + k * timedelta(minutes=5), 10, 11, 9, 10, 100, minutes=5) for k in range(1, 78)
            ]
            fq.add_bars(qid, "FiveMinutes", [c5(d), *rest])  # 78 bars per session
    for d in (day1, day2):
        fq.add_bars(1001, "OneMinute", minute_series(CAL.session_open(d), [str(30 + i) for i in range(30)]))

    clock = FixedClock(et(day1, 9, 0))
    rd = make_data(db_factory, clock, fq, date_from=first, date_to=D2, window_days=400)
    await rd.prepare_day(day1)

    five = fq.of("FiveMinutes")
    for qid in (1000, 1001):
        windows = sorted((r for r in five if r.symbol_id == qid), key=lambda r: r.start)
        assert len(windows) >= 3  # ~210 calendar days of 5-minute bars, <= 20,000 intervals each
        assert windows[0].start == CAL.session_open(span[0])
        assert windows[-1].end == CAL.session_close(D2)
        assert all(a.end == b.start for a, b in zip(windows, windows[1:], strict=False))
    held = rd.held_counts()
    assert held["opening_bars"] == 2 * len(span)  # one kept bar per session per symbol, of 78 fetched

    await rd.load_minute_bars([aaa], day1)
    assert rd.held_counts()["minute_bars"] == 30
    clock.set(et(day2, 9, 0))
    await rd.prepare_day(day2)
    assert len(fq.of("FiveMinutes")) == len(five)  # each symbol's range was fetched once
    assert rd.held_counts()["minute_bars"] == 0  # day 1's 1-minute bars were released

    clock.set(et(day2, 10, 0, 30))
    minute_requests = len(fq.of("OneMinute"))
    q = (await rd.quotes([aaa]))[aaa]
    assert q.last == Decimal("59")  # the 09:59-10:00 bar
    await rd.quotes([aaa])
    assert len(fq.of("OneMinute")) == minute_requests + 1  # loaded once
    assert rd.progress_counts()["questrade_requests"] == len(fq.requests)
