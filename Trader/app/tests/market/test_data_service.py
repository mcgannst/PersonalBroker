import asyncio
from bisect import bisect_left
from collections.abc import Sequence
from contextvars import ContextVar
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from structlog.testing import capture_logs

from tests.factories import add_symbol
from tests.fakes_questrade import FakeQuestrade
from trader.adapters.questrade.auth import AccessToken
from trader.adapters.questrade.client import QuestradeApiError, QuestradeClient, TokenBucket
from trader.adapters.questrade.models import CandleRequest
from trader.db import models as m
from trader.market import repository as repo
from trader.market.calendar import SessionCalendar
from trader.market.clock import FixedClock
from trader.market.data_service import FETCH_DEADLINE_S, MarketDataService
from trader.market.types import Candle, UniverseStatus

pytestmark = pytest.mark.db
CAL = SessionCalendar()
DAY = date(2026, 10, 6)
PREV = date(2026, 10, 5)
OPEN = CAL.session_open(DAY)  # 13:30Z
AFTER_BAR = datetime(2026, 10, 6, 13, 35, 5, tzinfo=UTC)
QT_BASE = "https://api05.iq.questrade.com/v1/"


def c5(start: datetime, volume: int = 5000, close: str = "21.40") -> Candle:
    return Candle(
        start,
        start + timedelta(minutes=5),
        Decimal("21.00"),
        Decimal("21.50"),
        Decimal("20.90"),
        Decimal(close),
        volume,
        None,
    )


@pytest.fixture
def ids(db_factory: sessionmaker[Session]) -> dict[str, int]:
    """AAA/BBB/CCC have Questrade ids 101-103; DDD has none."""
    out: dict[str, int] = {}
    with db_factory() as s:
        for i, t in enumerate(["AAA", "BBB", "CCC"]):
            out[t] = add_symbol(s, t, questrade_id=101 + i)
        out["DDD"] = add_symbol(s, "DDD")
        for sid in out.values():
            s.add(
                m.UniverseSnapshot(
                    session_date=DAY,
                    symbol_id=sid,
                    price=Decimal("20"),
                    avg_volume=2_000_000,
                    atr14=Decimal("1.0000"),
                    source="finviz",
                )
            )
            s.add(
                m.OpenBarStat(
                    symbol_id=sid,
                    session_date=DAY,
                    avg_open_vol_14d=Decimal("1000.00"),
                    atr14=Decimal("1.0000"),
                )
            )
        s.commit()
    return out


def service(
    factory: sessionmaker[Session], qt: FakeQuestrade, now: datetime = AFTER_BAR
) -> MarketDataService:
    return MarketDataService(factory, FixedClock(now), CAL, qt)


async def test_universe_and_stats_come_from_the_cache(
    db_factory: sessionmaker[Session], ids: dict[str, int]
) -> None:
    qt = FakeQuestrade()
    svc = service(db_factory, qt)
    members = await svc.universe(DAY)
    assert [x.ticker for x in members] == ["AAA", "BBB", "CCC", "DDD"]
    assert members[0].atr14 == Decimal("1.0000") and members[0].source == "finviz"
    stats = await svc.open_bar_stats(DAY)
    assert stats[ids["AAA"]].avg_open_vol_14d == Decimal("1000.00")
    assert await svc.universe(date(2026, 10, 7)) == []
    assert await svc.symbol_ids(["AAA", "ZZZ"]) == {"AAA": ids["AAA"]}
    assert qt.calls == []


async def test_universe_status_reads_the_nightly_detail(
    db_factory: sessionmaker[Session], ids: dict[str, int]
) -> None:
    svc = service(db_factory, FakeQuestrade())
    assert await svc.universe_status(DAY) == UniverseStatus("finviz", None, False, None)
    with db_factory() as s:
        s.add(
            m.JobRun(
                job="nightly",
                session_date=DAY,
                started_at=OPEN,
                finished_at=OPEN,
                status="succeeded",
                error=None,
                detail={
                    "source": "fallback",
                    "fallback_from": "2026-09-30",
                    "fallback_stale": True,
                    "fallback_age_sessions": 4,
                },
            )
        )
        s.commit()
    assert await svc.universe_status(DAY) == UniverseStatus("fallback", date(2026, 9, 30), True, 4)
    assert await svc.universe_status(date(2026, 10, 7)) == UniverseStatus(None, None, False, None)


async def test_opening_bars_use_the_cache_first(
    db_factory: sessionmaker[Session], ids: dict[str, int]
) -> None:
    with db_factory() as s:
        repo.upsert_intraday_candles(s, ids["AAA"], "5m", [c5(OPEN)])
        s.commit()
    qt = FakeQuestrade()
    got = await service(db_factory, qt).opening_bars(DAY, [ids["AAA"]])
    assert got.bars[ids["AAA"]].volume == 5000 and got.missing == {}
    assert qt.calls == []


async def test_opening_bars_fetch_the_rest_in_one_batch_and_cache_them(
    db_factory: sessionmaker[Session], ids: dict[str, int]
) -> None:
    with db_factory() as s:
        repo.upsert_intraday_candles(s, ids["AAA"], "5m", [c5(OPEN)])
        s.commit()
    qt = FakeQuestrade()
    qt.add_bars(102, "FiveMinutes", [c5(OPEN, volume=7000), c5(OPEN + timedelta(minutes=5))])
    got = await service(db_factory, qt).opening_bars(DAY)  # the whole universe
    assert set(got.bars) == {ids["AAA"], ids["BBB"]}
    assert got.bars[ids["BBB"]].volume == 7000
    assert got.missing == {ids["CCC"]: "no_bar_at_open", ids["DDD"]: "no_questrade_id"}
    assert qt.calls == [("candles_many", 2)]  # BBB and CCC only, in one batch
    with db_factory() as s:
        cached = (
            s.execute(select(m.IntradayCandle.volume).where(m.IntradayCandle.symbol_id == ids["BBB"]))
            .scalars()
            .all()
        )
    assert cached == [7000]


async def test_opening_bars_report_api_errors_per_symbol(
    db_factory: sessionmaker[Session], ids: dict[str, int]
) -> None:
    """Review Focus 4: a failing symbol is reported with a reason, never raised."""
    qt = FakeQuestrade()
    qt.errors[103] = 500
    qt.add_bars(101, "FiveMinutes", [c5(OPEN)])
    got = await service(db_factory, qt).opening_bars(DAY, [ids["AAA"], ids["CCC"]])
    assert set(got.bars) == {ids["AAA"]}
    assert got.missing == {ids["CCC"]: "questrade_error: HTTP 500"}


async def test_an_incomplete_opening_bar_is_neither_used_nor_cached(
    db_factory: sessionmaker[Session], ids: dict[str, int]
) -> None:
    qt = FakeQuestrade()
    qt.add_bars(101, "FiveMinutes", [c5(OPEN)])
    early = datetime(2026, 10, 6, 13, 34, 0, tzinfo=UTC)
    got = await service(db_factory, qt, now=early).opening_bars(DAY, [ids["AAA"]])
    assert got.bars == {} and got.missing == {ids["AAA"]: "bar_not_complete"}
    with db_factory() as s:
        assert s.execute(select(m.IntradayCandle)).first() is None


async def test_quotes_are_keyed_by_database_id(
    db_factory: sessionmaker[Session], ids: dict[str, int]
) -> None:
    qt = FakeQuestrade()
    qt.add_symbol("AAA", 101)
    qt.set_quote(101, "21.00", "21.02", "21.01", AFTER_BAR)
    got = await service(db_factory, qt).quotes([ids["AAA"], ids["DDD"]])
    assert list(got) == [ids["AAA"]]
    assert got[ids["AAA"]].symbol_id == ids["AAA"] and got[ids["AAA"]].ask == Decimal("21.02")
    assert qt.calls == [("quotes", 1)]


async def test_prior_close_from_the_cache_then_questrade(
    db_factory: sessionmaker[Session], ids: dict[str, int]
) -> None:
    with db_factory() as s:
        s.add(
            m.DailyCandle(
                symbol_id=ids["AAA"],
                date=PREV,
                open=Decimal("20"),
                high=Decimal("21"),
                low=Decimal("19"),
                close=Decimal("20.50"),
                volume=1,
                vwap=None,
            )
        )
        s.commit()
    qt = FakeQuestrade()
    prev_start = datetime(2026, 10, 5, 4, 0, tzinfo=UTC)  # 00:00 ET
    qt.add_bars(
        102,
        "OneDay",
        [
            Candle(
                prev_start,
                prev_start + timedelta(days=1),
                Decimal("30"),
                Decimal("31"),
                Decimal("29"),
                Decimal("30.25"),
                1,
                None,
            )
        ],
    )
    svc = service(db_factory, qt)
    assert await svc.prior_close(ids["AAA"], DAY) == Decimal("20.50")
    assert qt.calls == []
    assert await svc.prior_close(ids["BBB"], DAY) == Decimal("30.25")
    assert await svc.prior_close(ids["CCC"], DAY) is None
    assert await svc.prior_closes([ids["AAA"], ids["BBB"], ids["CCC"]], DAY) == {
        ids["AAA"]: Decimal("20.50"),
        ids["BBB"]: Decimal("30.25"),  # cached by the fallback above
    }


async def test_candles_come_from_the_cache_when_complete(
    db_factory: sessionmaker[Session], ids: dict[str, int]
) -> None:
    qt = FakeQuestrade()
    bars = [c5(OPEN + timedelta(minutes=5 * i)) for i in range(3)]
    qt.add_bars(101, "FiveMinutes", bars)
    end = OPEN + timedelta(minutes=15)
    svc = service(db_factory, qt, now=end)  # all three bars complete (a forming bar is never cached)
    first = await svc.candles(ids["AAA"], OPEN, end, "FiveMinutes")
    second = await svc.candles(ids["AAA"], OPEN, end, "FiveMinutes")
    assert [c.start for c in first] == [c.start for c in second] == [b.start for b in bars]
    assert qt.calls == [("candles", 1)]  # the second read was served from the cache


# --- P2-T7 attempt 2 regression tests (gauntlet findings) ---


def _cached_starts(factory: sessionmaker[Session], sid: int) -> list[datetime]:
    with factory() as s:
        return list(
            s.execute(
                select(m.IntradayCandle.ts)
                .where(m.IntradayCandle.symbol_id == sid)
                .order_by(m.IntradayCandle.ts)
            ).scalars()
        )


async def test_a_forming_bar_is_returned_but_never_cached(
    db_factory: sessionmaker[Session], ids: dict[str, int]
) -> None:
    qt = FakeQuestrade()
    bars = [c5(OPEN + timedelta(minutes=5 * i)) for i in range(3)]
    qt.add_bars(101, "FiveMinutes", bars)
    end = OPEN + timedelta(minutes=15)
    mid = OPEN + timedelta(minutes=12)  # the 13:40 bar is still forming
    got = await service(db_factory, qt, now=mid).candles(ids["AAA"], OPEN, end, "FiveMinutes")
    assert [c.start for c in got] == [b.start for b in bars]
    assert _cached_starts(db_factory, ids["AAA"]) == [bars[0].start, bars[1].start]


async def test_candles_refuse_naive_datetimes(db_factory: sessionmaker[Session], ids: dict[str, int]) -> None:
    svc = service(db_factory, FakeQuestrade())
    naive = OPEN.replace(tzinfo=None)
    with pytest.raises(ValueError, match="timezone-aware"):
        await svc.candles(ids["AAA"], naive, OPEN + timedelta(minutes=15), "FiveMinutes")
    with pytest.raises(ValueError, match="timezone-aware"):
        await svc.candles(ids["AAA"], OPEN, naive + timedelta(minutes=15), "FiveMinutes")


async def test_a_questrade_error_serves_the_cached_bars_with_a_warning(
    db_factory: sessionmaker[Session], ids: dict[str, int]
) -> None:
    """Review Focus 4: a failed fetch at a decision point returns what the cache has, never raises."""
    with db_factory() as s:
        repo.upsert_intraday_candles(s, ids["AAA"], "5m", [c5(OPEN)])
        s.commit()
    qt = FakeQuestrade()
    qt.errors[101] = 503
    qt.errors[102] = 503
    svc = service(db_factory, qt, now=OPEN + timedelta(minutes=30))
    end = OPEN + timedelta(minutes=15)
    with capture_logs() as logs:
        partial = await svc.candles(ids["AAA"], OPEN, end, "FiveMinutes")
        empty = await svc.candles(ids["BBB"], OPEN, end, "FiveMinutes")
    assert [c.start for c in partial] == [OPEN] and empty == []
    warned = [e for e in logs if e["event"] == "market.candles_fetch_failed"]
    assert [(e["log_level"], e["status"], e["served_from_cache"]) for e in warned] == [
        ("warning", 503, 1),
        ("warning", 503, 0),
    ]
    # No Questrade id: the cached bars, not a fetch.
    with db_factory() as s:
        repo.upsert_intraday_candles(s, ids["DDD"], "5m", [c5(OPEN)])
        s.commit()
    assert [c.start for c in await svc.candles(ids["DDD"], OPEN, end, "FiveMinutes")] == [OPEN]


class HangingQuestrade(FakeQuestrade):
    """Ignores the deadline it is given and never returns: the service's own guard must still stop it."""

    async def candles_many(
        self, reqs: Sequence[CandleRequest], *, deadline_s: float | None = None
    ) -> dict[CandleRequest, list[Candle] | QuestradeApiError]:
        self.calls.append(("candles_many", len(reqs)))
        await asyncio.Event().wait()  # never returns
        raise AssertionError("unreachable")


async def test_opening_bars_stop_at_the_deadline_and_report_timeouts(
    db_factory: sessionmaker[Session], ids: dict[str, int]
) -> None:
    assert FETCH_DEADLINE_S <= 45  # well inside the 60 s budget for the 9:35 scan
    with db_factory() as s:
        repo.upsert_intraday_candles(s, ids["AAA"], "5m", [c5(OPEN)])
        s.commit()
    qt = HangingQuestrade()
    svc = MarketDataService(db_factory, FixedClock(AFTER_BAR), CAL, qt, fetch_deadline_s=0.05)
    with capture_logs() as logs:
        got = await svc.opening_bars(DAY)
    assert set(got.bars) == {ids["AAA"]}  # the cached bar is still served
    assert got.missing == {ids["BBB"]: "timeout", ids["CCC"]: "timeout", ids["DDD"]: "no_questrade_id"}
    assert [e["event"] for e in logs] == ["market.opening_bars_timeout"]
    assert (logs[0]["completed"], logs[0]["outstanding"]) == (0, 2)


async def test_universe_status_without_a_nightly_row_treats_a_fallback_as_stale(
    db_factory: sessionmaker[Session],
) -> None:
    with db_factory() as s:
        sid = add_symbol(s, "FALL", questrade_id=901)
        for day, source in ((DAY, "fallback"), (PREV, "manual")):
            s.add(
                m.UniverseSnapshot(
                    session_date=day, symbol_id=sid, price=None, avg_volume=None, atr14=None, source=source
                )
            )
        s.add(
            m.JobRun(
                job="nightly",
                session_date=DAY,
                started_at=OPEN,
                finished_at=OPEN,
                status="failed",
                error="boom",
                detail={"source": "fallback", "fallback_stale": False},
            )
        )
        s.commit()
    svc = service(db_factory, FakeQuestrade())
    assert await svc.universe_status(DAY) == UniverseStatus("fallback", None, True, None)
    assert await svc.universe_status(PREV) == UniverseStatus("manual", None, False, None)


async def test_quote_ids_are_cached_and_dropped_on_a_miss(
    db_factory: sessionmaker[Session], ids: dict[str, int]
) -> None:
    qt = FakeQuestrade()
    qt.add_symbol("AAA", 101)
    qt.set_quote(101, "21.00", "21.02", "21.01", AFTER_BAR)
    svc = service(db_factory, qt)
    assert (await svc.quotes([ids["AAA"]]))[ids["AAA"]].ask == Decimal("21.02")
    # AAA is re-mapped to a new Questrade id; the cached mapping misses once, then is re-read.
    with db_factory() as s:
        s.get(m.Symbol, ids["AAA"]).questrade_id = 111  # type: ignore[union-attr]
        s.commit()
    del qt.quote_map[101]
    qt.set_quote(111, "22.00", "22.02", "22.01", AFTER_BAR)
    assert await svc.quotes([ids["AAA"]]) == {}
    assert (await svc.quotes([ids["AAA"]]))[ids["AAA"]].ask == Decimal("22.02")


class _Tokens:
    def access(self) -> AccessToken:
        return AccessToken("tok", QT_BASE, AFTER_BAR + timedelta(minutes=30))

    def force_refresh(self) -> AccessToken:
        return self.access()


_released_at: ContextVar[float] = ContextVar("released_at")


@respx.mock
async def test_a_universe_of_550_fetches_within_the_rate_limit_and_the_budget(
    db_factory: sessionmaker[Session],
) -> None:
    """~550 opening bars through the real client and TokenBucket on virtual time: <= 20 req/s, < 60 s.

    The fake sleep jumps virtual time to the end of the wait without yielding, so each request's
    virtual send time is the moment the bucket released it (kept per task in a ContextVar, because
    the concurrent requests reach the HTTP layer in a different order).
    """
    n = 550
    with db_factory() as s:
        sids = [add_symbol(s, f"U{i:03d}", questrade_id=50_000 + i) for i in range(n)]
        s.commit()
    vt = {"now": 0.0}
    sent: list[float] = []

    async def virtual_sleep(seconds: float) -> None:
        vt["now"] = max(vt["now"], vt["now"] + seconds)

    class RecordingBucket(TokenBucket):
        async def acquire(self) -> None:
            await super().acquire()
            _released_at.set(vt["now"])

    bar = {
        "start": OPEN.isoformat(),
        "end": (OPEN + timedelta(minutes=5)).isoformat(),
        "open": 20,
        "high": 21,
        "low": 19,
        "close": 20.5,
        "volume": 9000,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(_released_at.get())
        return httpx.Response(200, json={"candles": [bar]})

    respx.get(url__regex=rf"{QT_BASE}markets/candles/\d+").mock(side_effect=handler)
    async with QuestradeClient(_Tokens(), FixedClock(AFTER_BAR), sleep=virtual_sleep) as client:
        client._buckets["market"] = RecordingBucket(20.0, monotonic=lambda: vt["now"], sleep=virtual_sleep)
        svc = MarketDataService(db_factory, FixedClock(AFTER_BAR), CAL, client)
        got = await svc.opening_bars(DAY, sids)
    assert got.missing == {} and len(got.bars) == n and len(sent) == n
    times = sorted(sent)
    busiest = max(bisect_left(times, t + 1.0 - 1e-6) - i for i, t in enumerate(times))
    assert busiest <= 20, f"{busiest} requests inside one second"
    span = times[-1] - times[0]
    assert span == pytest.approx((n - 1) / 20.0)  # evenly spaced at 20/s: ~27.5 s
    assert span < min(FETCH_DEADLINE_S, 60), f"took {span:.1f} s of virtual time"


# --- FIX-OPENBARS (Mon 2026-09-28): the deadline keeps the bars that already arrived ---


def _bar_json(volume: int) -> dict[str, object]:
    return {
        "start": OPEN.isoformat(),
        "end": (OPEN + timedelta(minutes=5)).isoformat(),
        "open": 21.00,
        "high": 21.50,
        "low": 20.90,
        "close": 21.40,
        "volume": volume,
    }


def _mock_opening_bars(slow_qids: set[int], volumes: dict[int, int]) -> None:
    """Questrade ids in `slow_qids` never answer; the others answer at once with their volume."""
    never = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        qid = int(request.url.path.rsplit("/", 1)[1])
        if qid in slow_qids:
            await never.wait()
        return httpx.Response(200, json={"candles": [_bar_json(volumes.get(qid, 5000))]})

    respx.get(url__regex=rf"{QT_BASE}markets/candles/\d+").mock(side_effect=handler)


@respx.mock
async def test_opening_bars_keep_the_bars_that_arrived_before_the_deadline(
    db_factory: sessionmaker[Session], ids: dict[str, int]
) -> None:
    """Mon 2026-09-28: 543 requests, the 45 s deadline fired and every symbol, fetched or not, was
    reported "timeout" (0 candidates). Only the requests still outstanding may be "timeout"."""
    _mock_opening_bars({103}, {101: 5000, 102: 3000})
    async with QuestradeClient(_Tokens(), FixedClock(AFTER_BAR), market_rps=1000.0) as client:
        svc = MarketDataService(db_factory, FixedClock(AFTER_BAR), CAL, client, fetch_deadline_s=0.3)
        with capture_logs() as logs:
            got = await asyncio.wait_for(svc.opening_bars(DAY), timeout=5)
    assert set(got.bars) == {ids["AAA"], ids["BBB"]}
    assert got.bars[ids["BBB"]].volume == 3000
    assert got.missing == {ids["CCC"]: "timeout", ids["DDD"]: "no_questrade_id"}
    assert _cached_starts(db_factory, ids["AAA"]) == [OPEN]  # the fetched bars are cached as before
    (warn,) = [e for e in logs if e["event"] == "market.opening_bars_timeout"]
    assert warn["log_level"] == "warning"
    assert (warn["symbols"], warn["completed"], warn["outstanding"]) == (3, 2, 1)
    assert warn["deadline_s"] == 0.3 and warn["elapsed_s"] >= 0.3
    assert warn["client_stats"]["requests"] == 3 and warn["client_stats"]["http_429"] == 0  # all 3 sent
    assert "pause_s" in warn["client_stats"]
    assert "tok" not in str(warn) and QT_BASE not in str(warn)


@respx.mock
async def test_opening_bars_log_the_fetch_when_it_finishes_in_time(
    db_factory: sessionmaker[Session], ids: dict[str, int]
) -> None:
    _mock_opening_bars(set(), {})
    async with QuestradeClient(_Tokens(), FixedClock(AFTER_BAR), market_rps=1000.0) as client:
        svc = MarketDataService(db_factory, FixedClock(AFTER_BAR), CAL, client, fetch_deadline_s=5.0)
        with capture_logs() as logs:
            got = await svc.opening_bars(DAY)
    assert got.missing == {ids["DDD"]: "no_questrade_id"}
    assert [e["event"] for e in logs] == ["market.opening_bars_fetched"]
    (info,) = logs
    assert (info["symbols"], info["completed"], info["outstanding"]) == (3, 3, 0)
    assert info["client_stats"]["requests"] == 3


@respx.mock
async def test_orb_scans_the_symbols_whose_bars_arrived_before_the_deadline(
    db_factory: sessionmaker[Session], ids: dict[str, int]
) -> None:
    """The ORB scan ranks the partial set it has; the timed-out symbols are only noted as missing."""
    from tests.strategies.fakes import FakeCatalyst, FakeCatalysts, make_ctx
    from trader.strategies.base import EnterLong
    from trader.strategies.orb_sip import ORB_EVENT, OrbSip

    _mock_opening_bars({103}, {101: 5000, 102: 3000})  # CCC never answers
    async with QuestradeClient(_Tokens(), FixedClock(AFTER_BAR), market_rps=1000.0) as client:
        svc = MarketDataService(db_factory, FixedClock(AFTER_BAR), CAL, client, fetch_deadline_s=0.3)
        strategy = OrbSip()
        cats = FakeCatalysts({ids["AAA"]: FakeCatalyst(), ids["BBB"]: FakeCatalyst()})
        ctx = make_ctx(svc, strategy.params, cats, now=AFTER_BAR, session=DAY)  # type: ignore[arg-type]
        orb = next(e for e in strategy.schedule(CAL) if e.key == ORB_EVENT)
        intents = await asyncio.wait_for(strategy.on_event(ctx, orb), timeout=5)
    (entry,) = intents
    assert isinstance(entry, EnterLong) and entry.symbol_id == ids["AAA"]  # rvol 5 ranks first
    assert {c.symbol_id for c in ctx.candidates} == {ids["AAA"], ids["BBB"]}
    note = next(n for n in ctx.notes if "no opening bar" in n.message)
    assert note.data["missing"] == {str(ids["CCC"]): "timeout", str(ids["DDD"]): "no_questrade_id"}
