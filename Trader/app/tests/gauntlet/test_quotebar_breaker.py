"""QUOTEBAR breaker (checker attempt 1): tries to break the 9:35 opening bar built from live quotes.

Quote semantics at the edges, the candle-scale volume factor, the quotes pass on a virtual clock, 401 code
1022 versus a real token failure, the 09:47 shadow check and the isolation of replay / orb_sip from the new
bar source. No real service is touched: FakeQuestrade, respx and the test Postgres only."""

import dataclasses
import json
import re
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from tests.factories import add_symbol
from tests.fakes_questrade import FakeQuestrade
from tests.strategies.fakes import FakeCatalyst, FakeCatalysts, FakeData, make_ctx
from trader.adapters.questrade.auth import AccessToken
from trader.adapters.questrade.client import QuestradeApiError, QuestradeClient
from trader.adapters.questrade.models import QtQuote
from trader.db import models as m
from trader.jobs.openbar_check import OpenbarCheckDeps, OpenbarNotReady, run_openbar_check
from trader.market.calendar import SessionCalendar
from trader.market.clock import FixedClock
from trader.market.data_service import QUOTES_DEADLINE_S, MarketDataService
from trader.market.indicators import rvol
from trader.market.quote_bars import (
    DEFAULT_VOLUME_FACTOR,
    NO_TRADE,
    QUOTE_DELAYED,
    QUOTE_INCOMPLETE,
    VolumeScale,
    compare_bars,
    quote_bar,
    scale_volume,
)
from trader.market.types import Candle, OpeningBars
from trader.notify.messages import quote_bars_line
from trader.notify.types import QuoteBarsLineView
from trader.strategies.base import EnterLong
from trader.strategies.orb_sip import ORB_EVENT, OrbSip, OrbSipParams

CAL = SessionCalendar()
DAY = date(2026, 10, 6)
OPEN = CAL.session_open(DAY)
BAR_END = OPEN + timedelta(minutes=5)
T_ORB = BAR_END + timedelta(seconds=5)
QT_BASE = "https://api05.iq.questrade.com/v1/"
APP = Path(__file__).resolve().parents[2]


def q(qid: int, o: str | None, h: str | None, low: str | None, c: str, volume: int, **kw: Any) -> QtQuote:
    values: dict[str, Any] = {
        "symbol_id": qid,
        "symbol": f"Q{qid}",
        "bid": Decimal(c) - Decimal("0.01"),
        "ask": Decimal(c) + Decimal("0.01"),
        "last": Decimal(c),
        "last_regular": Decimal(c),
        "volume": volume,
        "last_trade_time": T_ORB - timedelta(seconds=1),
        "delay": 0,
        "is_halted": False,
        "vwap": None,
        "open": None if o is None else Decimal(o),
        "high": None if h is None else Decimal(h),
        "low": None if low is None else Decimal(low),
    }
    values.update(kw)
    return QtQuote(**values)


def c5(o: str, h: str, low: str, c: str, v: int) -> Candle:
    return Candle(OPEN, BAR_END, Decimal(o), Decimal(h), Decimal(low), Decimal(c), v, None)


# --- 1. quote semantics at the edges (pure) ---
@pytest.mark.parametrize(
    ("kw", "reason"),
    [
        ({"open": None}, NO_TRADE),  # no regular-session open trade yet
        ({"open": Decimal("0")}, NO_TRADE),
        ({"last_trade_time": OPEN - timedelta(seconds=1)}, NO_TRADE),  # a stale premarket print
        ({"last_trade_time": None}, NO_TRADE),
        ({"volume": 0}, NO_TRADE),
        ({"volume": -5}, NO_TRADE),  # negative volume never becomes a bar
        ({"last": None, "last_regular": None}, NO_TRADE),
        ({"delay": 15}, QUOTE_DELAYED),
        ({"high": None}, QUOTE_INCOMPLETE),
        ({"low": Decimal("0")}, QUOTE_INCOMPLETE),
    ],
)
def test_a_quote_that_is_not_a_bar_is_a_missing_reason_never_a_candle(
    kw: dict[str, Any], reason: str
) -> None:
    quote = dataclasses.replace(q(1, "20.00", "20.60", "19.90", "20.50", 10_000), **kw)
    assert quote_bar(quote, OPEN, BAR_END) == reason


def test_high_below_open_and_low_above_last_are_widened_and_the_close_prefers_regular_hours() -> None:
    # high trails the open, low above the last trade: the bar must still contain open and close
    quote = q(1, "20.00", "19.95", "20.40", "20.30", 10_000, last=Decimal("20.35"))
    bar = quote_bar(quote, OPEN, BAR_END)
    assert isinstance(bar, Candle)
    assert bar.close == Decimal("20.30")  # lastTradePriceTrHrs wins over lastTradePrice
    assert (bar.open, bar.high, bar.low) == (Decimal("20.00"), Decimal("20.30"), Decimal("20.00"))
    assert bar.low <= min(bar.open, bar.close) <= max(bar.open, bar.close) <= bar.high
    assert (bar.start, bar.end, bar.volume) == (OPEN, BAR_END, 10_000)  # raw volume: scaled by the caller
    # last_regular 0 falls back to lastTradePrice, never a zero close
    other = quote_bar(q(2, "20.00", "20.60", "19.90", "20.50", 1, last_regular=Decimal("0")), OPEN, BAR_END)
    assert isinstance(other, Candle) and other.close == Decimal("20.50")


@respx.mock
async def test_the_client_parses_null_open_null_volume_and_exact_decimals() -> None:
    body = {
        "quotes": [
            {
                "symbol": "AAA",
                "symbolId": 11,
                "lastTradePrice": 19.09,
                "lastTradePriceTrHrs": 19.09,
                "volume": None,
                "lastTradeTime": "2026-10-06T09:35:04.000000-04:00",
                "delay": 0,
                "openPrice": None,
                "highPrice": 19.09,
                "lowPrice": 18.87,
            },
            {
                "symbol": "BBB",
                "symbolId": 12,
                "lastTradePrice": 0.1,
                "volume": -3,
                "lastTradeTime": "2026-10-06T09:35:04.000000-04:00",
                "delay": 0,
                "openPrice": 0.3,
                "highPrice": 0.30000000000000004,
                "lowPrice": 0.1,
            },
        ]
    }
    respx.get(QT_BASE + "markets/quotes").mock(
        return_value=httpx.Response(200, content=json.dumps(body).encode(), headers={"content-type": "json"})
    )
    async with QuestradeClient(Tokens(), FixedClock(T_ORB), sleep=_nosleep, monotonic=lambda: 0.0) as client:
        a, b = await client.quotes([11, 12])
    assert a.open is None and a.volume == 0 and a.high == Decimal("19.09") and a.low == Decimal("18.87")
    assert quote_bar(a, OPEN, BAR_END) == NO_TRADE
    assert b.open == Decimal("0.3") and b.high == Decimal("0.30000000000000004")  # Decimal, never float
    assert quote_bar(b, OPEN, BAR_END) == NO_TRADE  # negative volume


# --- 2. the volume factor ---
def test_absurd_factors_are_never_used_and_the_median_or_default_takes_over() -> None:
    absurd = {1: Decimal("0"), 2: Decimal("1000"), 3: Decimal("-0.7"), 4: Decimal("0.049999")}
    scale = VolumeScale(absurd, date(2026, 10, 5))
    for sid in absurd:
        assert scale.factor_for(sid) == (DEFAULT_VOLUME_FACTOR, "default")  # no usable one: no median either
    # few samples: one usable -> it is the median; two -> their mean, the absurd ones ignored
    one = VolumeScale({**absurd, 5: Decimal("0.6")}, date(2026, 10, 5))
    assert one.factor_for(1) == (Decimal("0.6"), "median")
    two = VolumeScale({**absurd, 5: Decimal("0.6"), 6: Decimal("0.8")}, date(2026, 10, 5))
    assert two.factor_for(2) == (Decimal("0.7"), "median") and two.factor_for(6) == (Decimal("0.8"), "symbol")
    # day 1: nothing measured at all
    assert VolumeScale({}, None).factor_for(9) == (DEFAULT_VOLUME_FACTOR, "default")


@pytest.mark.xfail(
    strict=True,
    raises=InvalidOperation,
    reason="QUOTEBAR nit: usable_factor compares a NaN Decimal (InvalidOperation); only reachable if a NaN "
    "ever lands in quote_volume_scale.factor, and then the 9:35 scan raises (no trades, not wrong trades)",
)
def test_a_nan_factor_falls_back_instead_of_crashing_the_scan() -> None:
    """Postgres NUMERIC can hold 'NaN'; a NaN row must not raise InvalidOperation inside the 9:35 scan."""
    scale = VolumeScale({1: Decimal("NaN"), 2: Decimal("0.6")}, date(2026, 10, 5))
    assert scale.factor_for(1) == (Decimal("0.6"), "median")


def test_rvol_on_the_scaled_volume_is_exact_decimal() -> None:
    raw = 14_003
    scaled = scale_volume(raw, DEFAULT_VOLUME_FACTOR)  # 10,002.3429 -> 10,002
    assert scaled == 10_002
    assert scale_volume(7, Decimal("0.5")) == 4  # half up, never banker's rounding
    r = rvol(scaled, Decimal("3333.33"))
    assert r == Decimal("3.0006") and isinstance(r, Decimal)


# --- 3. the quotes pass, the window, the fallback (test Postgres) ---
class Tokens:
    def __init__(self) -> None:
        self.refreshes: list[str | None] = []

    def access(self) -> AccessToken:
        return AccessToken(f"tok{len(self.refreshes)}", QT_BASE, T_ORB + timedelta(minutes=30))

    def force_refresh(self, rejected: str | None = None) -> AccessToken:
        self.refreshes.append(rejected)
        return self.access()


async def _nosleep(seconds: float) -> None:
    return None


class Recording(FakeQuestrade):
    def __init__(self) -> None:
        super().__init__()
        self.quote_requests: list[list[int]] = []
        self.quote_errors: dict[int, BaseException] = {}
        self.extra: list[QtQuote] = []  # appended to every quotes() response (duplicates / unrequested)
        self.candle_ids: list[int] = []  # the Questrade ids of every candles_many request

    async def candles_many(self, reqs: Sequence[Any], *, deadline_s: float | None = None) -> Any:
        self.candle_ids.extend(r.symbol_id for r in reqs)
        return await super().candles_many(reqs, deadline_s=deadline_s)

    async def quotes(self, ids: Sequence[int]) -> list[QtQuote]:
        n = len(self.quote_requests)
        self.quote_requests.append(list(ids))
        if n in self.quote_errors:
            raise self.quote_errors[n]
        return [*await super().quotes(ids), *self.extra]


def svc(factory: sessionmaker[Session], client: Any, now: datetime = T_ORB, **kw: Any) -> MarketDataService:
    return MarketDataService(factory, FixedClock(now), CAL, client, opening_bar_source="quotes", **kw)


def symbols(factory: sessionmaker[Session], n: int, base: int = 1000) -> list[int]:
    with factory() as s:
        out = [add_symbol(s, f"B{i:03d}", questrade_id=base + i) for i in range(n)]
        s.commit()
    return out


@pytest.mark.db
async def test_duplicate_and_unrequested_quotes_never_leak_into_other_symbols(
    db_factory: sessionmaker[Session],
) -> None:
    sids = symbols(db_factory, 3)
    fq = Recording()
    fq.quote_map[1000] = q(1000, "20.00", "20.60", "19.90", "20.50", 10_000)
    fq.quote_map[1001] = q(1001, "30.00", "30.10", "29.00", "29.50", 20_000)
    # a second, different quote for 1000 and one for an id never asked for
    fq.extra = [
        q(1000, "20.00", "20.60", "19.90", "20.50", 10_000),
        q(4242, "99.00", "99.00", "99.00", "99.00", 1),
    ]
    got = await svc(db_factory, fq).opening_bars(DAY, sids)
    assert set(got.bars) == {sids[0], sids[1]}
    assert got.missing == {sids[2]: "no_quote"}
    assert got.bars[sids[0]].open == Decimal("20.00")
    with db_factory() as s:
        assert s.execute(select(func.count()).select_from(m.OpeningBarQuote)).scalar_one() == 2
        assert s.execute(select(func.count()).select_from(m.IntradayCandle)).scalar_one() == 0


@pytest.mark.db
@pytest.mark.parametrize(
    ("offset", "quotes_used"),
    [
        (timedelta(seconds=4), True),  # 09:35:04
        (timedelta(minutes=4, seconds=59), True),
        (timedelta(minutes=5), False),  # 09:40:00: the window is half open
        (timedelta(minutes=5, seconds=1), False),  # 09:40:01
    ],
)
async def test_the_five_minute_quote_window_edges(
    db_factory: sessionmaker[Session], offset: timedelta, quotes_used: bool
) -> None:
    (sid,) = symbols(db_factory, 1)
    fq = Recording()
    fq.quote_map[1000] = q(1000, "20.00", "20.60", "19.90", "20.50", 10_000)
    fq.add_bars(1000, "FiveMinutes", [c5("21.00", "21.50", "20.90", "21.40", 5_000)])
    got = await svc(db_factory, fq, now=BAR_END + offset).opening_bars(DAY, [sid])
    assert bool(fq.quote_requests) is quotes_used
    assert got.bars[sid].open == (Decimal("20.00") if quotes_used else Decimal("21.00"))
    assert got.sources[sid] == ("quotes" if quotes_used else "candles")


@pytest.mark.db
@respx.mock
async def test_the_quotes_pass_with_a_429_stays_well_under_its_budget_on_a_virtual_clock(
    db_factory: sessionmaker[Session],
) -> None:
    sids = symbols(db_factory, 550)
    calls = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        calls[0] += 1
        if calls[0] == 2:  # the second request is rate limited once (no Reset header)
            return httpx.Response(429, json={"code": 1006, "message": "Too many requests"})
        asked = [int(x) for x in request.url.params["ids"].split(",")]
        quotes = [
            {
                "symbol": f"S{i}",
                "symbolId": i,
                "lastTradePrice": 20.5,
                "lastTradePriceTrHrs": 20.5,
                "volume": 1400,
                "lastTradeTime": "2026-10-06T09:35:04.000000-04:00",
                "delay": 0,
                "openPrice": 20.0,
                "highPrice": 20.6,
                "lowPrice": 19.9,
            }
            for i in asked
        ]
        return httpx.Response(200, json={"quotes": quotes})

    respx.get(QT_BASE + "markets/quotes").mock(side_effect=handler)
    virtual = [0.0]

    async def sleep(seconds: float) -> None:
        virtual[0] += seconds

    async with QuestradeClient(
        Tokens(), FixedClock(T_ORB), sleep=sleep, monotonic=lambda: virtual[0]
    ) as client:
        got = await svc(db_factory, client).opening_bars(DAY, sids)
    assert len(got.bars) == 550 and got.missing == {}
    assert calls[0] == 7  # 6 batches of <= 100 ids + the one 429 retry
    assert virtual[0] < QUOTES_DEADLINE_S / 5  # well under the 15 s budget, even with the 429 pause


@pytest.mark.db
async def test_the_candle_fallback_covers_only_the_failed_batch(db_factory: sessionmaker[Session]) -> None:
    sids = symbols(db_factory, 250)
    fq = Recording()
    for i in range(250):
        fq.quote_map[1000 + i] = q(1000 + i, "20.00", "20.60", "19.90", "20.50", 10_000)
    fq.quote_errors[1] = QuestradeApiError(503, "unavailable")  # the second batch (ids 100-199) fails
    for i in range(100, 200):
        fq.add_bars(1000 + i, "FiveMinutes", [c5("21.00", "21.50", "20.90", "21.40", 5_000)])
    got = await svc(db_factory, fq).opening_bars(DAY, sids)
    assert sorted(fq.candle_ids) == [1000 + i for i in range(100, 200)]  # only the failed batch
    assert len(got.bars) == 250
    assert {got.sources[sids[i]] for i in range(100, 200)} == {"candles"}
    assert {got.sources[sids[i]] for i in [*range(100), *range(200, 250)]} == {"quotes"}


@pytest.mark.db
@pytest.mark.xfail(
    strict=True,
    reason="QUOTEBAR should-fix: a hung quotes pass (min(15 s, deadline)) is followed by the full candle "
    "deadline, so the worst-case 9:35 scan is ~60 s, not the prior 45 s",
)
async def test_a_hung_quotes_pass_then_hung_candles_stays_within_the_prior_scan_deadline(
    db_factory: sessionmaker[Session],
) -> None:
    import asyncio

    (sid,) = symbols(db_factory, 1)

    class Hung(Recording):
        async def quotes(self, ids: Sequence[int]) -> list[QtQuote]:
            await asyncio.sleep(10)
            return []

        async def candles_many(self, reqs: Sequence[Any], *, deadline_s: float | None = None) -> Any:
            await asyncio.sleep(10)
            return {}

    deadline = 0.3
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await svc(db_factory, Hung(), fetch_deadline_s=deadline).opening_bars(DAY, [sid])
    assert loop.time() - t0 <= deadline * 1.25


# --- 4. 401 code 1022 is not a token failure ---
@respx.mock
@pytest.mark.parametrize(("code", "refreshes"), [(1022, 0), (1017, 1)])
async def test_a_1022_never_forces_a_token_refresh_but_another_401_refreshes_once(
    code: int, refreshes: int
) -> None:
    tokens = Tokens()
    responses = iter(
        [
            httpx.Response(401, json={"code": code, "message": "not in your current market data package"}),
            httpx.Response(200, json={"quotes": []}),
        ]
    )
    route = respx.get(QT_BASE + "markets/quotes").mock(side_effect=lambda _r: next(responses))
    async with QuestradeClient(tokens, FixedClock(T_ORB), sleep=_nosleep, monotonic=lambda: 0.0) as client:
        if code == 1022:
            with pytest.raises(QuestradeApiError) as err:
                await client.quotes([1])
            assert err.value.status == 401 and err.value.code == 1022
        else:
            assert await client.quotes([1]) == []
    assert len(tokens.refreshes) == refreshes  # a refresh would rotate the refresh token for nothing
    assert route.call_count == 1 + refreshes


# --- 5. the shadow check ---
def _quote_row(sid: int, bar: Candle) -> m.OpeningBarQuote:
    return m.OpeningBarQuote(
        session_date=DAY,
        symbol_id=sid,
        captured_at=T_ORB,
        quote_time=T_ORB - timedelta(seconds=1),
        open=bar.open,
        high=bar.high,
        low=bar.low,
        close=bar.close,
        quote_volume=int(bar.volume * 1.4),
        volume=bar.volume,
        vol_factor=Decimal("0.714300"),
        factor_source="default",
    )


@pytest.mark.db
async def test_the_shadow_check_caches_only_official_candles_and_its_maths_is_right(
    db_factory: sessionmaker[Session],
) -> None:
    a, b = symbols(db_factory, 2)
    quote_a = c5("20.00", "20.60", "19.90", "20.50", 1_100)  # volume +10% vs official: within
    quote_b = c5("30.00", "30.20", "29.00", "29.50", 2_000)  # high differs; volume +100%
    official_a = c5("20.00", "20.60", "19.90", "20.45", 1_000)
    official_b = c5("30.00", "30.10", "29.00", "29.50", 1_000)
    with db_factory() as s:
        for sid, bar in ((a, quote_a), (b, quote_b)):
            s.add(_quote_row(sid, bar))
            s.add(
                m.OpenBarStat(
                    symbol_id=sid, session_date=DAY, avg_open_vol_14d=Decimal("1000"), atr14=Decimal("1")
                )
            )
        s.commit()
    fq = FakeQuestrade()
    fq.add_bars(1000, "FiveMinutes", [official_a])
    fq.add_bars(1001, "FiveMinutes", [official_b])
    at = OPEN + timedelta(minutes=17)
    deps = OpenbarCheckDeps(
        factory=db_factory,
        clock=FixedClock(at),
        calendar=CAL,
        data=MarketDataService(db_factory, FixedClock(at), CAL, fq),
        run_id=None,
        params=OrbSipParams,
    )
    detail = await run_openbar_check(deps, DAY)
    assert detail["compared"] == 2 and detail["prices_exact"] == 1 and detail["volume_within_10pct"] == 1
    assert detail["median_volume_error"] == "1.0000"  # sorted [0.1000, 1.0000], index len // 2
    with db_factory() as s:
        cached = {r.symbol_id: r for r in s.execute(select(m.IntradayCandle)).scalars()}
        rows = {r.symbol_id: r for r in s.execute(select(m.OpeningBarQuote)).scalars()}
    # the candle table holds the OFFICIAL bars only, never the quote-built values
    assert (cached[a].close, cached[a].volume) == (Decimal("20.45"), 1_000)
    assert (cached[b].high, cached[b].volume) == (Decimal("30.10"), 1_000)
    # the quote rows keep what the scan decided on
    assert (rows[b].high, rows[b].volume, rows[b].official_high) == (
        Decimal("30.20"),
        2_000,
        Decimal("30.10"),
    )
    cmp = compare_bars(quote_b, official_b)
    assert not cmp.prices_exact and cmp.high_diff == Decimal("0.10") and cmp.volume_error == Decimal("1.0000")


@pytest.mark.db
async def test_the_shadow_check_raises_not_ready_on_401s_and_writes_nothing(
    db_factory: sessionmaker[Session],
) -> None:
    (sid,) = symbols(db_factory, 1)
    with db_factory() as s:
        s.add(_quote_row(sid, c5("20.00", "20.60", "19.90", "20.50", 1_100)))
        s.commit()

    class Refusing:
        async def opening_bars(
            self, session_date: date, symbol_ids: Sequence[int] | None = None
        ) -> OpeningBars:
            return OpeningBars({}, {sid: "questrade_error: HTTP 401 1022 not in your market data package"})

    at = OPEN + timedelta(minutes=17)
    deps = OpenbarCheckDeps(db_factory, FixedClock(at), CAL, Refusing(), None, OrbSipParams)
    with pytest.raises(OpenbarNotReady):
        await run_openbar_check(deps, DAY)
    with db_factory() as s:
        row = s.execute(select(m.OpeningBarQuote)).scalar_one()
    assert row.checked_at is None and row.check_status is None
    from trader.runtime import OPENBAR_CHECK_RETRY_DEADLINE

    assert OPENBAR_CHECK_RETRY_DEADLINE.isoformat() == "10:30:00"


def test_the_postclose_line_with_zero_compared_and_with_rounding() -> None:
    none = quote_bars_line(QuoteBarsLineView(540, 0, 0, 0, 0))
    assert none == "Opening bars from quotes: 540 built, none compared (shadow check missing)"
    line = quote_bars_line(QuoteBarsLineView(540, 3, 2, 1, 1))
    assert line.startswith("Opening bars from quotes: 3 compared, prices exact ")
    assert re.search(r"prices exact 67%, volume within ±10% 33%, 1 decision would differ$", line), line


# --- 6. isolation: replay stays on candles, orb_sip decides the same on the same bar ---
def test_only_the_live_engine_builds_a_quotes_mode_service() -> None:
    users = [
        p.relative_to(APP).as_posix()
        for p in (APP / "trader").rglob("*.py")
        if "LIVE_OPENING_BAR_SOURCE" in p.read_text() or 'opening_bar_source="quotes"' in p.read_text()
    ]
    assert sorted(users) == ["trader/engine/orchestrator.py", "trader/market/data_service.py"]
    replay = (APP / "trader" / "replay").rglob("*.py")
    assert not any("opening_bar_source" in p.read_text() for p in replay)


class SourcedData(FakeData):
    def __init__(self, source: str | None) -> None:
        super().__init__()
        self.source = source

    async def opening_bars(self, session_date: date, symbol_ids: Sequence[int] | None = None) -> OpeningBars:
        got = await super().opening_bars(session_date, symbol_ids)
        if self.source is None:
            return got
        return dataclasses.replace(got, sources=dict.fromkeys(got.bars, self.source))


def _strip(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if k != "bar_source"}


async def _decide(source: str | None) -> tuple[list[Any], list[dict[str, Any]]]:
    data = SourcedData(source)
    data.add(1, "AAA", c5("21.00", "21.50", "20.90", "21.40", 5_000))
    data.add(2, "BBB", c5("30.00", "30.60", "29.90", "30.50", 3_000))
    data.add(3, "CCC", c5("10.00", "10.40", "9.95", "10.30", 800))
    cats = FakeCatalysts({1: FakeCatalyst(), 2: FakeCatalyst()})
    strategy = OrbSip(OrbSipParams())
    ctx = make_ctx(data, strategy.params, cats)
    event = next(e for e in strategy.schedule(CAL) if e.key == ORB_EVENT)
    intents = [
        dataclasses.replace(i, evidence=_strip(dict(i.evidence))) if isinstance(i, EnterLong) else i
        for i in await strategy.on_event(ctx, event)
    ]
    records = [
        {k: v for k, v in dataclasses.asdict(r).items() if k != "data"} | {"data": _strip(r.data)}
        for r in ctx.candidates
    ]
    sources = {r.data.get("bar_source") for r in ctx.candidates}
    assert sources == {source}  # the only difference: the ranked names say where their bar came from
    return intents, records


async def test_the_same_bar_gives_the_same_decision_whatever_its_source() -> None:
    candles_intents, candles_records = await _decide(None)
    quotes_intents, quotes_records = await _decide("quotes")
    assert candles_intents and candles_intents == quotes_intents
    assert candles_records == quotes_records


@pytest.mark.db
async def test_one_absurd_measured_factor_does_not_lose_the_whole_session(
    db_factory: sessionmaker[Session],
) -> None:
    good, bad = symbols(db_factory, 2)
    close = CAL.session_close(DAY)
    fq = Recording()
    fq.quote_map[1000] = q(1000, "20", "21", "19", "20.5", 1_400_000, last_trade_time=close)
    fq.quote_map[1001] = q(1001, "20", "21", "19", "20.5", 1, last_trade_time=close)
    day = [
        Candle(
            OPEN + timedelta(minutes=5 * i),
            OPEN + timedelta(minutes=5 * (i + 1)),
            *(Decimal("20"),) * 4,
            12_500,
            None,
        )
        for i in range(78)
    ]
    fq.add_bars(1000, "FiveMinutes", day)
    fq.add_bars(1001, "FiveMinutes", day)
    service = MarketDataService(db_factory, FixedClock(close + timedelta(minutes=15)), CAL, fq)
    await service.measure_volume_scale(DAY, [good, bad])
    with db_factory() as s:
        factors = dict(s.execute(select(m.QuoteVolumeScale.symbol_id, m.QuoteVolumeScale.factor)).all())
    assert factors.get(good) == Decimal("0.696429")
