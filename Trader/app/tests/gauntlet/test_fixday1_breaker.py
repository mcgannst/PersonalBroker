"""FIX-DAY1 breaker (checker attempt 1 of 776954a): the failure modes most likely to cost Thursday's 9:35.

- quote_late false positives: the 09:35:00 capture is ~6 SEQUENTIAL quotes requests (~550 ids); an active
  name's last trade tracks the moment Questrade answered, so every request answered after 09:35:02 marks its
  active names late. Simulated with per-request latency, pacing and a 429 pause.
- worker timing: restarts, DST, early close.
- the delta volume edge cases, the factor's pre-market-candle fallback.
- fill freshness: book vs last trade.
"""

import random
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from trader.adapters.questrade.models import QtQuote
from trader.broker.fill_model import FillParams, QuoteFillModel
from trader.broker.types import FillDecision, NoFill, OrderSpec
from trader.market.calendar import SessionCalendar
from trader.market.clock import ET, FixedClock
from trader.market.data_service import MarketDataService
from trader.market.quote_bars import (
    QUOTE_LATE,
    open_snapshot_volume,
    opening_delta,
    quote_bar,
    regular_factor,
)
from trader.market.quote_captures import OpeningCaptures

CAL = SessionCalendar()
DAY = date(2026, 10, 1)  # Thursday
OPEN = CAL.session_open(DAY)
BAR_END = OPEN + timedelta(minutes=5)
PACE_S = 1 / 17  # the market-data bucket


# --- 1. quote_late false positives in a realistic 09:35:00 capture ------------------------------------------
class TimedQuotes:
    """A Questrade stand-in on a FixedClock: each request waits for the pacing bucket, optionally a 429 pause,
    then `latency` seconds; Questrade answers at the request's midpoint, and an active name's last trade is an
    exponential gap (mean `mean_gap_s`) before that moment. Default 0.3 s: an ORB candidate (high opening
    rvol) prints several times a second. Probe results (late share of 550, gaps 0.3 / 1.0 / 2.2 s): typical
    0.1-0.4 s latency 0/0/0%; all 0.4 s 14/6/4%; 429 1 s on #2 32/15/8%; 429 2 s 77/55/32%; start +1.5 s
    54/32/18%; start +3 s 100/84/60%."""

    def __init__(
        self,
        clock: FixedClock,
        latencies: Sequence[float],
        *,
        pause_before: dict[int, float] | None = None,
        mean_gap_s: float = 0.3,
        seed: int = 7,
        floor: datetime = OPEN + timedelta(minutes=4),
    ) -> None:
        self.floor = floor
        self.clock = clock
        self.latencies = list(latencies)
        self.pause_before = pause_before or {}
        self.mean_gap_s = mean_gap_s
        self.rng = random.Random(seed)
        self.n = 0

    async def quotes(self, ids: Sequence[int]) -> list[QtQuote]:
        i = self.n
        self.n += 1
        self.clock.advance(timedelta(seconds=PACE_S + self.pause_before.get(i, 0.0)))
        lat = self.latencies[i % len(self.latencies)]
        served = self.clock.now() + timedelta(seconds=lat / 2)
        self.clock.advance(timedelta(seconds=lat))
        out = []
        for qid in ids:
            gap = self.rng.expovariate(1 / self.mean_gap_s)
            last_trade = min(served - timedelta(seconds=gap), served)
            out.append(
                QtQuote(
                    symbol_id=qid,
                    symbol=f"S{qid}",
                    bid=Decimal("9.99"),
                    ask=Decimal("10.01"),
                    last=Decimal("10"),
                    last_regular=Decimal("10"),
                    volume=100_000,
                    last_trade_time=max(last_trade, self.floor),
                    delay=0,
                    is_halted=False,
                    vwap=None,
                    open=Decimal("9.8"),
                    high=Decimal("10.2"),
                    low=Decimal("9.7"),
                    fetched_at=self.clock.now(),
                )
            )
        return out


class _NoDb:
    def __call__(self) -> Any:
        raise RuntimeError("no database in this test")


async def _late_fraction(
    client: TimedQuotes, clock: FixedClock, n: int = 550
) -> tuple[float, dict[str, Any]]:
    svc = MarketDataService(_NoDb(), clock, CAL, client, opening_bar_source="quotes")  # type: ignore[arg-type]
    svc._quote_qids.update({sid: 10_000 + sid for sid in range(1, n + 1)})  # warmed by the open capture
    detail = await svc.capture_quotes(DAY, "bar", list(range(1, n + 1)))
    return detail["late"] / detail["quoted"], detail


async def test_realistic_capture_marks_few_active_names_late() -> None:
    """Typical: 0.1-0.4 s per 100-id request, started on time. The 6th request is answered ~1.5 s in."""
    clock = FixedClock(BAR_END + timedelta(milliseconds=20))
    rng = random.Random(1)
    client = TimedQuotes(clock, [rng.uniform(0.1, 0.4) for _ in range(6)])
    frac, detail = await _late_fraction(client, clock)
    assert detail["quoted"] == 550
    assert frac < 0.05, detail


@pytest.mark.xfail(
    strict=True,
    reason="FINDING (should-fix): the OPEN capture has only a 2 s lead. At 0.4 s per request its last "
    "request (~13-17% of names) is answered after 09:30:00, their quotes then hold the opening print, "
    "open_snapshot_volume gives None, and those names fall back to unpublished candles: missing at 9:35",
)
async def test_open_capture_at_0_4_s_per_request_is_answered_before_the_open() -> None:
    clock = FixedClock(OPEN - timedelta(seconds=2))
    client = TimedQuotes(clock, [0.4] * 6, floor=OPEN - timedelta(minutes=10))
    svc = MarketDataService(_NoDb(), clock, CAL, client, opening_bar_source="quotes")  # type: ignore[arg-type]
    svc._quote_qids.update({sid: 10_000 + sid for sid in range(1, 551)})
    detail = await svc.capture_quotes(DAY, "open", list(range(1, 551)))
    assert detail["after_open"] / detail["quoted"] < 0.05, detail


async def test_slow_but_healthy_api_all_requests_at_0_4_s() -> None:
    """Every request at the top of the realistic range (0.4 s): requests 5-6 are answered after 09:35:02."""
    clock = FixedClock(BAR_END + timedelta(milliseconds=20))
    client = TimedQuotes(clock, [0.4] * 6)
    frac, detail = await _late_fraction(client, clock)
    assert frac < 0.20, detail  # ~14%: the 6th request's active names


@pytest.mark.xfail(
    strict=True,
    reason="FINDING (should-fix): one 429 window pause (~1 s) on the 2nd request pushes requests 2-6 past "
    "09:35:02: ~32% of active names are quote_late and not traded (the 6 requests are sequential)",
)
async def test_a_429_pause_on_the_second_request() -> None:
    clock = FixedClock(BAR_END + timedelta(milliseconds=20))
    client = TimedQuotes(clock, [0.25] * 6, pause_before={1: 1.0})
    frac, detail = await _late_fraction(client, clock)
    assert frac < 0.25, detail


@pytest.mark.xfail(
    strict=True,
    reason="FINDING (should-fix): a capture that starts 1.5 s late (a slow previous step, token refresh) at "
    "normal latency loses ~54% of active names to quote_late; CAPTURE_WINDOW (5 s) admits starts that cannot "
    "produce a usable bar",
)
async def test_a_capture_started_1_5_s_late() -> None:
    clock = FixedClock(BAR_END + timedelta(seconds=1.5))
    client = TimedQuotes(clock, [0.25] * 6)
    frac, detail = await _late_fraction(client, clock)
    assert frac < 0.25, detail


# --- 2. worker timing ---------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("day", "label"),
    [
        (date(2026, 11, 2), "first session after DST ends (EST)"),
        (date(2026, 10, 30), "last EDT session"),
        (date(2026, 11, 27), "early close day after Thanksgiving"),
    ],
)
def test_capture_times_are_09_29_58_and_09_35_00_et_across_dst_and_early_close(day: date, label: str) -> None:
    caps = OpeningCaptures(None, CAL)  # type: ignore[arg-type]
    times = dict(caps.times(day))
    assert times["open"].astimezone(ET).time().isoformat() == "09:29:58", label
    assert times["bar"].astimezone(ET).time().isoformat() == "09:35:00", label


@pytest.mark.db
async def test_a_restart_at_09_33_skips_the_open_capture_but_runs_the_bar_one(
    db_factory: sessionmaker[Session],
) -> None:
    from tests.test_worker import TUE, Harness, et
    from tests.test_worker_captures import FakeCaptures
    from trader.worker import Worker

    clock = FixedClock(et(TUE, 9, 33, 0))
    h = Harness(db_factory, clock)
    caps = FakeCaptures(clock)
    w = Worker(h.deps(), captures=caps)
    while clock.now() < et(TUE, 9, 35, 3):
        await w.step()
        clock.advance(timedelta(seconds=min(w._interval(), 2.0)))
    assert [k for k, _, _ in caps.calls] == ["bar"]
    assert caps.calls[0][2] == et(TUE, 9, 35, 0)


@pytest.mark.db
@pytest.mark.xfail(
    strict=True,
    reason="FINDING (should-fix): the once-per-session set is in memory; a worker restarted at 09:35:03 "
    "(inside CAPTURE_WINDOW) re-runs the bar capture and REPLACES the good 09:35:00 rows with quotes read "
    "3 s late (most active names then quote_late)",
)
async def test_a_restart_inside_the_window_does_not_overwrite_the_stored_bar_capture(
    db_factory: sessionmaker[Session],
) -> None:
    from tests.test_worker import TUE, Harness, et
    from tests.test_worker_captures import FakeCaptures
    from trader.worker import Worker

    clock = FixedClock(et(TUE, 9, 35, 0))
    h = Harness(db_factory, clock)
    caps = FakeCaptures(clock)
    await Worker(h.deps(), captures=caps).step()  # the first worker captured at 09:35:00 ...
    clock.set(et(TUE, 9, 35, 3))
    await Worker(h.deps(), captures=caps).step()  # ... then crashed and came back
    assert [at for _, _, at in caps.calls] == [et(TUE, 9, 35, 0)]


# --- 3. delta volume ----------------------------------------------------------------------------------------
def _q(volume: int, at: datetime | None, delay: int | None = 0) -> QtQuote:
    return QtQuote(1, "X", None, None, Decimal("10"), Decimal("10"), volume, at, delay, False, None)


def test_open_capture_edge_cases_never_yield_the_raw_day_volume() -> None:
    yesterday = OPEN - timedelta(days=1, hours=-6)  # 15:30 ET the day before
    assert open_snapshot_volume(_q(4_000_000, yesterday), OPEN, DAY) == 0  # yesterday's volume: 0 today
    assert open_snapshot_volume(_q(4_000_000, None), OPEN, DAY) == 0
    assert open_snapshot_volume(_q(10, OPEN - timedelta(seconds=1), delay=None), OPEN, DAY) is None
    assert open_snapshot_volume(_q(10, OPEN), OPEN, DAY) is None  # already holds the opening print
    assert open_snapshot_volume(None, OPEN, DAY) is None
    assert open_snapshot_volume(_q(-5, OPEN - timedelta(minutes=1)), OPEN, DAY) == 0
    assert opening_delta(100, 150) is None  # the day volume shrank: bad data, no bar
    assert opening_delta(100, None) is None
    assert opening_delta(150, 150) == 0


def test_premarket_quote_larger_than_day_volume_gives_no_factor() -> None:
    assert regular_factor(500_000, 400_000, premarket_quote=400_000) is None
    assert regular_factor(500_000, 400_000, premarket_quote=500_000) is None


def test_no_premarket_candles_returned_falls_back_to_the_inflated_ratio() -> None:
    """Documents the risk for tonight's Wed re-measure: if Questrade returns no pre-market candles (or the
    request starting 04:00 ET yields RTH only), the factor is RTH / (quote day incl. pre-market), the OLD
    QUOTEBAR ratio, now applied to a pre-market-free delta: Thursday's opening volumes come out low by the
    pre-market share (~35% median on Wed)."""
    rth, day = 650_000, 1_000_000  # quote day volume, 35% pre-market
    with_pm = regular_factor(rth, day, premarket_candle=int(0.35 * day * 0.65))
    without_pm = regular_factor(rth, day, premarket_candle=0)
    assert without_pm == Decimal("0.650000")
    assert with_pm is not None and with_pm > without_pm


def test_quote_late_boundary_is_strictly_after_two_seconds() -> None:
    def bar_q(at: datetime) -> QtQuote:
        return QtQuote(
            1, "X", None, None, Decimal("10"), Decimal("10"), 5, at, 0, False, None,
            open=Decimal("9.9"), high=Decimal("10.1"), low=Decimal("9.8"),
        )  # fmt: skip

    assert quote_bar(bar_q(BAR_END + timedelta(seconds=2)), OPEN, BAR_END) != QUOTE_LATE
    assert quote_bar(bar_q(BAR_END + timedelta(seconds=2, microseconds=1)), OPEN, BAR_END) == QUOTE_LATE


# --- 4. fill freshness --------------------------------------------------------------------------------------
NOW = datetime(2026, 10, 1, 14, 0, tzinfo=UTC)
MODEL = QuoteFillModel(FillParams(stale_quote_seconds=10.0))


def fq(
    bid: str | None = "9.99",
    ask: str | None = "10.00",
    last: str | None = "10.50",
    *,
    trade_age: float | None = 900.0,
    fetch_age: float | None = 0.5,
    delay: int | None = 0,
    halted: bool = False,
) -> QtQuote:
    return QtQuote(
        1,
        "X",
        Decimal(bid) if bid is not None else None,
        Decimal(ask) if ask is not None else None,
        Decimal(last) if last is not None else None,
        None,
        1000,
        NOW - timedelta(seconds=trade_age) if trade_age is not None else None,
        delay,
        halted,
        None,
        fetched_at=NOW - timedelta(seconds=fetch_age) if fetch_age is not None else None,
    )


def test_fill_freshness_matrix() -> None:
    buy_stop = OrderSpec(1, "buy", "stop", 10, stop=Decimal("10.20"))
    sell_stop = OrderSpec(1, "sell", "stop", 10, stop=Decimal("10.20"), purpose="stop", position_id=1)
    # an old print above the buy stop (10.50) with the live ask below it does NOT trigger
    assert MODEL.assess(buy_stop, fq(), NOW) == NoFill("not_triggered")
    # an old print below a protective sell stop does not trigger either; the live bid does
    assert MODEL.assess(sell_stop, fq(last="9.00", bid="10.30", ask="10.31"), NOW) == NoFill("not_triggered")
    sold = MODEL.assess(sell_stop, fq(last="11.00", bid="10.10", ask="10.11"), NOW)
    assert isinstance(sold, FillDecision) and sold.trigger == "stop" and sold.price < Decimal("10.20")
    # live book, ask at the stop: fills at max(stop, ask) + slippage
    filled = MODEL.assess(buy_stop, fq(ask="10.25", bid="10.24"), NOW)
    assert isinstance(filled, FillDecision) and filled.price > Decimal("10.25")
    # locked book (bid == ask) is live
    assert isinstance(MODEL.assess(buy_stop, fq(bid="10.25", ask="10.25"), NOW), FillDecision)
    # never a fill on a stale fetch, a crossed or one-sided/zero book with an old print, delay or halt
    for bad in (
        fq(ask="10.25", bid="10.24", fetch_age=10.5),
        fq(bid="10.30", ask="10.25"),
        fq(bid="0", ask="10.25"),
        fq(bid=None, ask="10.25"),
        fq(ask="10.25", bid="10.24", delay=15),
        fq(ask="10.25", bid="10.24", delay=None),
        fq(ask="10.25", bid="10.24", halted=True),
    ):
        out = MODEL.assess(buy_stop, bad, NOW)
        assert isinstance(out, NoFill), bad


def test_replay_path_without_fetched_at_is_unchanged() -> None:
    buy_stop = OrderSpec(1, "buy", "stop", 10, stop=Decimal("10.20"))
    out = MODEL.assess(buy_stop, fq(ask="10.25", bid="10.24", fetch_age=None, trade_age=30), NOW)
    assert isinstance(out, NoFill) and out.reason == "stale_quote"
    out = MODEL.assess(buy_stop, fq(ask="10.25", bid="10.24", last="10.21", fetch_age=None, trade_age=2), NOW)
    assert isinstance(out, FillDecision)
