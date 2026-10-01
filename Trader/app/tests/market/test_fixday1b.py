"""FIX-DAY1b (the FIX-DAY1 check's should-fix list, Thu 2026-10-01):

1. The two timed captures send their <= 100-id requests concurrently (the client's bucket still paces them),
   and `quote_late` is judged against when each request was SENT: a quote is late when its last trade is more
   than QUOTE_LATE_AFTER after max(09:35:00, sent), with `sent` counted at most REQUEST_LAG_MAX past 09:35:00
   (so no trade more than 4 s after the bar's end is ever part of it).
3. The open capture starts 5 s before the open.
4. A capture whose rows are already stored for the session is not run again (a worker restart).
5. The volume factor measured late counts the after-hours candles too: the quote's day volume includes them.
"""

import asyncio
from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from tests.factories import add_capture
from tests.market.test_data_service_fixday1 import (  # the fixture `ids` and its helpers
    DAY,
    OPEN,
    Quotes,
    ids,  # noqa: F401
    quote,
    seed,
    svc,
)
from trader.adapters.questrade.client import QuestradeApiError
from trader.adapters.questrade.models import CandleRequest, QtQuote
from trader.db import models as m
from trader.market import data_service as ds
from trader.market.calendar import SessionCalendar
from trader.market.clock import FixedClock
from trader.market.quote_bars import (
    OPEN_CAPTURE_LEAD,
    QUOTE_LATE,
    REQUEST_LAG_MAX,
    late_cutoff,
    quote_bar,
)
from trader.market.types import Candle

CAL = SessionCalendar()
BAR_END = OPEN + timedelta(minutes=5)
S = timedelta(seconds=1)


# --- the late rule ------------------------------------------------------------------------------------------
def test_late_cutoff_is_two_seconds_after_the_later_of_the_bar_end_and_the_send() -> None:
    assert late_cutoff(BAR_END) == BAR_END + 2 * S  # no send time: the bar's end (the scan's own pass)
    assert late_cutoff(BAR_END, BAR_END - S) == BAR_END + 2 * S  # asked early (never happens): the bar's end
    assert late_cutoff(BAR_END, BAR_END + 0.8 * S) == BAR_END + 2.8 * S  # a paced or 429-delayed request
    assert REQUEST_LAG_MAX == 2 * S
    assert late_cutoff(BAR_END, BAR_END + 6 * S) == BAR_END + 4 * S  # never more than 4 s past the bar


def _bar_quote(last_trade: datetime, requested_at: datetime | None = None) -> QtQuote:
    return quote(1, 5_000, last_trade, requested_at=requested_at)


def test_quote_bar_judges_late_against_the_send_time_only_when_given() -> None:
    q = _bar_quote(BAR_END + 2.5 * S, requested_at=BAR_END + S)
    assert quote_bar(q, OPEN, BAR_END) == QUOTE_LATE  # without asked_at: as before, bar end + 2 s
    assert isinstance(quote_bar(q, OPEN, BAR_END, asked_at=BAR_END + S), Candle)
    assert quote_bar(q, OPEN, BAR_END, asked_at=BAR_END + 0.2 * S) == QUOTE_LATE
    assert quote_bar(_bar_quote(BAR_END + 4.5 * S), OPEN, BAR_END, asked_at=BAR_END + 3 * S) == QUOTE_LATE


def test_the_open_capture_starts_five_seconds_before_the_open() -> None:
    assert OPEN_CAPTURE_LEAD == 5 * S


# --- concurrent capture requests (no database) --------------------------------------------------------------
class _NoDb:
    def __call__(self) -> Any:
        raise RuntimeError("no database in this test")


class InFlight:
    """Counts the requests in flight at once; each request takes a few loop turns."""

    def __init__(self, clock: FixedClock, *, fail: dict[int, int] | None = None, hang: bool = False) -> None:
        self.clock = clock
        self.now = 0
        self.peak = 0
        self.calls = 0
        self.fail = fail or {}
        self.hang = hang
        self.cancelled = 0

    async def quotes(self, wanted: Sequence[int]) -> list[QtQuote]:
        i = self.calls
        self.calls += 1
        self.now += 1
        self.peak = max(self.peak, self.now)
        try:
            if i in self.fail:
                raise QuestradeApiError(self.fail[i], "fake")
            if self.hang and i > 0:
                await asyncio.Event().wait()
            for _ in range(3):
                await asyncio.sleep(0)
            return [quote(qid, 1_000, BAR_END - S) for qid in wanted]
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        finally:
            self.now -= 1


def _svc(client: Any, clock: FixedClock, n: int = 550) -> ds.MarketDataService:
    service = ds.MarketDataService(_NoDb(), clock, CAL, client, opening_bar_source="quotes")  # type: ignore[arg-type]
    service._quote_qids.update({sid: 10_000 + sid for sid in range(1, n + 1)})
    return service


@pytest.mark.parametrize("kind", ["open", "bar"])
async def test_a_capture_sends_its_requests_concurrently(kind: str) -> None:
    clock = FixedClock(BAR_END)
    client = InFlight(clock)
    detail = await _svc(client, clock).capture_quotes(DAY, kind, list(range(1, 551)))
    assert client.calls == 6 and client.peak == 6
    assert detail["quoted"] == 550 and detail["failed"] == 0


async def test_the_scans_own_pass_stays_sequential() -> None:
    clock = FixedClock(BAR_END)
    client = InFlight(clock)
    quotes, errors = await _svc(client, clock)._quote_pass([(s, 10_000 + s) for s in range(1, 551)], 15.0)
    assert len(quotes) == 550 and not errors and client.peak == 1


async def test_a_401_in_a_capture_cancels_the_requests_still_outstanding() -> None:
    clock = FixedClock(BAR_END)
    client = InFlight(clock, fail={0: 401}, hang=True)
    detail = await _svc(client, clock).capture_quotes(DAY, "bar", list(range(1, 551)))
    assert detail["quoted"] == 0 and detail["failed"] == 550
    assert detail["missing_reasons"] == {"questrade_error: HTTP 401": 550}
    assert client.cancelled == 5


async def test_a_capture_past_its_deadline_reports_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ds, "CAPTURE_DEADLINE_S", 0.05)
    clock = FixedClock(BAR_END)
    client = InFlight(clock, hang=True)
    detail = await _svc(client, clock).capture_quotes(DAY, "bar", list(range(1, 551)))
    assert detail["quoted"] == 100 and detail["missing_reasons"] == {"timeout": 450}


async def test_a_failed_request_costs_only_its_own_symbols() -> None:
    clock = FixedClock(BAR_END)
    client = InFlight(clock, fail={2: 500})
    detail = await _svc(client, clock).capture_quotes(DAY, "bar", list(range(1, 551)))
    assert detail["quoted"] == 450 and detail["failed"] == 100


class Answers:
    """Every request answered with active names printing up to the moment Questrade answers, `answer_after`
    after the request's send time (the client's `requested_at`), which is `sent_after[i]` after 09:35:00."""

    def __init__(self, sent_after: Sequence[float], answer_after: float) -> None:
        self.sent_after = sent_after
        self.answer_after = answer_after
        self.calls = 0

    async def quotes(self, wanted: Sequence[int]) -> list[QtQuote]:
        sent = BAR_END + self.sent_after[self.calls] * S
        self.calls += 1
        served = sent + self.answer_after * S
        return [quote(qid, 1_000, served, requested_at=sent) for qid in wanted]


async def test_late_is_counted_against_each_requests_send_time() -> None:
    """Request 2 was held 1.2 s by a 429 pause; its names printed until it was answered (1.5 s past
    09:35:00, or 2.7 s with a slow 1.5 s answer): not late, it was asked late. Request 6, sent 3 s late, is
    judged against the 4 s ceiling: its trades at 3.3 s count, those at 4.5 s are late."""
    clock = FixedClock(BAR_END + 0.05 * S)
    detail = await _svc(Answers([0.0, 1.2, 0.1, 0.15, 0.2, 3.0], 0.3), clock).capture_quotes(
        DAY, "bar", list(range(1, 551))
    )
    assert detail["late"] == 0
    detail = await _svc(Answers([0.0, 1.2, 0.1, 0.15, 0.2, 3.0], 1.5), clock).capture_quotes(
        DAY, "bar", list(range(1, 551))
    )
    assert detail["late"] == 50  # only the last request's: 4.5 s past the bar


# --- persisted capture-done, the send time stored -----------------------------------------------------------
@pytest.mark.db
async def test_a_stored_capture_is_not_run_again(
    db_factory: sessionmaker[Session],
    ids: dict[str, int],  # noqa: F811
) -> None:
    clock = FixedClock(BAR_END)
    fq = Quotes(clock)
    fq.quote_map[201] = quote(201, 1_000, BAR_END - S)
    first = await svc(db_factory, fq, clock).capture_quotes(DAY, "bar")
    assert first["quoted"] == 1 and "skipped" not in first
    clock.set(BAR_END + 0.8 * S)  # the worker crashed and came back inside the window
    fq.quote_map[201] = quote(201, 9_999, BAR_END + 0.7 * S)
    again = await svc(db_factory, fq, clock).capture_quotes(DAY, "bar")
    assert again["skipped"] == "already_captured" and len(fq.requests) == 1
    with db_factory() as s:
        rows = list(s.execute(select(m.OpeningQuoteCapture)).scalars())
    assert [(r.kind, r.volume, r.capture_started_at) for r in rows] == [("bar", 1_000, BAR_END)]
    # the other kind (and another session) still runs
    opened = await svc(db_factory, fq, FixedClock(OPEN - 5 * S)).capture_quotes(DAY, "open")
    assert "skipped" not in opened and len(fq.requests) == 2


@pytest.mark.db
async def test_each_rows_send_time_is_stored_and_the_scan_judges_late_by_it(
    db_factory: sessionmaker[Session],
    ids: dict[str, int],  # noqa: F811
) -> None:
    clock = FixedClock(BAR_END + 0.02 * S)
    fq = Quotes(clock)
    sent = BAR_END + 1.4 * S  # a 429 pause held the request 1.4 s
    fq.quote_map[201] = quote(201, 339_533, BAR_END + 3 * S, requested_at=sent)  # 1.6 s after it was asked
    fq.quote_map[202] = quote(202, 100_000, BAR_END + 3 * S)  # no send time from the client: when handed over
    service = svc(db_factory, fq, clock)
    detail = await service.capture_quotes(DAY, "bar")
    assert detail["late"] == 1
    with db_factory() as s:
        rows = {r.symbol_id: r for r in s.execute(select(m.OpeningQuoteCapture)).scalars()}
        add_capture(s, DAY, ids["CLDX"], "open", OPEN - 5 * S, volume=303_905, quote_time=OPEN - 30 * S)
        add_capture(s, DAY, ids["NVTS"], "open", OPEN - 5 * S, volume=0, quote_time=OPEN - 30 * S)
        s.commit()
    assert rows[ids["CLDX"]].capture_started_at == sent
    assert rows[ids["NVTS"]].capture_started_at == BAR_END + 0.02 * S
    clock.set(BAR_END + 5 * S)
    got = await service.opening_bars(DAY, [ids["CLDX"], ids["NVTS"]])
    assert got.bars[ids["CLDX"]].volume == round((339_533 - 303_905) * 0.7)
    assert got.missing == {ids["NVTS"]: QUOTE_LATE}


@pytest.mark.db
async def test_the_scan_still_judges_its_own_pass_against_the_bar_end(
    db_factory: sessionmaker[Session],
    ids: dict[str, int],  # noqa: F811
) -> None:
    seed(db_factory, ids, opens={"CLDX": 0}, bars={})
    clock = FixedClock(BAR_END + 5 * S)
    fq = Quotes(clock)
    fq.quote_map[201] = quote(201, 50_000, BAR_END + 3 * S, requested_at=BAR_END + 5 * S)
    got = await svc(db_factory, fq, clock).opening_bars(DAY, [ids["CLDX"]])
    assert got.missing == {ids["CLDX"]: QUOTE_LATE}


# --- the factor on the same trading window ------------------------------------------------------------------
def _five(start: datetime, n: int, volume: int) -> list[Candle]:
    step = timedelta(minutes=5)
    return [
        Candle(start + step * i, start + step * (i + 1), *(Decimal("20"),) * 4, volume, None)
        for i in range(n)
    ]


class Recording(Quotes):
    def __init__(self, clock: FixedClock) -> None:
        super().__init__(clock)
        self.candle_reqs: list[CandleRequest] = []

    async def candles_many(
        self, reqs: Sequence[CandleRequest], *, deadline_s: float | None = None
    ) -> dict[CandleRequest, list[Candle] | QuestradeApiError]:
        self.candle_reqs.extend(reqs)
        return await super().candles_many(reqs, deadline_s=deadline_s)


def _day(fq: Quotes, close: datetime) -> None:
    """780,000 regular-session candle volume, 99,999 pre-market, 120,000 after hours (16:00-20:00)."""
    pre = _five(OPEN - timedelta(hours=1), 1, 99_999)
    fq.add_bars(201, "FiveMinutes", [*pre, *_five(OPEN, 78, 10_000), *_five(close, 48, 2_500)])


@pytest.mark.db
async def test_a_late_measure_counts_the_after_hours_candles_like_the_quote_does(
    db_factory: sessionmaker[Session],
    ids: dict[str, int],  # noqa: F811
) -> None:
    """Re-run at 02:15 ET the next night: the quote's day volume (1,571,429) holds the after-hours trades
    (quote scale 171,429 = 120,000 / 0.7), so the candles must too: (780,000 + 120,000) / (1,571,429 -
    400,000) = 0.768292, not 780,000 / 1,171,429 = 0.665853."""
    close = CAL.session_close(DAY)
    seed(db_factory, ids, opens={"CLDX": 400_000}, bars={})
    clock = FixedClock(close + timedelta(hours=10, minutes=15))
    fq = Recording(clock)
    fq.quote_map[201] = quote(201, 1_571_429, close + timedelta(hours=3))
    _day(fq, close)
    detail = await svc(db_factory, fq, clock).measure_volume_scale(DAY, [ids["CLDX"]])
    assert detail["measured"] == 1 and detail["afterhours_candle_volume"] == 120_000
    assert fq.candle_reqs[0].end == close + timedelta(hours=4)  # 20:00 ET
    with db_factory() as s:
        row = s.get(m.QuoteVolumeScale, (DAY, ids["CLDX"]))
    assert row is not None and row.factor == Decimal("0.768292")
    assert row.candle_volume == 780_000  # still the regular session's


@pytest.mark.db
async def test_the_1615_postclose_measure_is_unchanged(
    db_factory: sessionmaker[Session],
    ids: dict[str, int],  # noqa: F811
) -> None:
    """At 16:15 no after-hours candle is published yet (~10 min delay; a request ending inside that delay is
    refused): the candles end at the close as before and the factor is the FIX-DAY1 one."""
    close = CAL.session_close(DAY)
    seed(db_factory, ids, opens={"CLDX": 400_000}, bars={})
    clock = FixedClock(close + timedelta(minutes=15))
    fq = Recording(clock)
    fq.quote_map[201] = quote(201, 1_400_000, close)
    _day(fq, close)
    await svc(db_factory, fq, clock).measure_volume_scale(DAY, [ids["CLDX"]])
    assert fq.candle_reqs[0].end == close
    with db_factory() as s:
        row = s.get(m.QuoteVolumeScale, (DAY, ids["CLDX"]))
    assert row is not None and row.factor == Decimal("0.780000")


@pytest.mark.db
async def test_a_measure_an_hour_after_the_close_reads_only_published_after_hours_candles(
    db_factory: sessionmaker[Session],
    ids: dict[str, int],  # noqa: F811
) -> None:
    close = CAL.session_close(DAY)
    seed(db_factory, ids, opens={"CLDX": 400_000}, bars={})
    clock = FixedClock(close + timedelta(hours=1, minutes=2))
    fq = Recording(clock)
    fq.quote_map[201] = quote(201, 1_400_000, close + timedelta(minutes=50))
    _day(fq, close)
    detail = await svc(db_factory, fq, clock).measure_volume_scale(DAY, [ids["CLDX"]])
    assert fq.candle_reqs[0].end == close + timedelta(minutes=45)  # 17:02 - 15 min, on the 5-minute grid
    assert detail["afterhours_candle_volume"] == 9 * 2_500


@pytest.mark.parametrize("late_s", [0.0, 2.0])  # on time (09:29:55), and at the end of its window (09:29:58)
async def test_the_open_capture_at_0_4_s_per_request_is_answered_before_the_open(late_s: float) -> None:
    """The FIX-DAY1 breaker's open-capture finding (its stand-in serialises the requests, so concurrency does
    not show there) at the new 5 s lead: even six 0.4 s requests one after another end before 09:30:00."""
    from tests.gauntlet.test_fixday1_breaker import TimedQuotes

    clock = FixedClock(OPEN - OPEN_CAPTURE_LEAD + late_s * S)
    client = TimedQuotes(clock, [0.4] * 6, floor=OPEN - timedelta(minutes=10))
    detail = await _svc(client, clock).capture_quotes(DAY, "open", list(range(1, 551)))
    assert detail["quoted"] == 550 and detail["after_open"] == 0
