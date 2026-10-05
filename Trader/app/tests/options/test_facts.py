"""OPTSIM T8: underlying facts from Questrade details, the FinViz snapshot and daily bars."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from tests.options.factories import SESSION, T0, add_underlying
from tests.options.fakes import FakeQtOptions
from trader.adapters.finviz.fundamentals import parse_snapshot
from trader.adapters.finviz.scraper import FinvizBlocked
from trader.adapters.questrade.option_types import QtSymbolDetails
from trader.db import models as m
from trader.market.calendar import SessionCalendar
from trader.market.clock import ET, FixedClock
from trader.market.types import Candle
from trader.options.facts import (
    FACT_FIELDS,
    FINVIZ_FIELDS,
    FactsService,
    TrendInputs,
    classify_security,
    trend_inputs,
)
from trader.options.settings import OptionSettings
from trader.options.types import UnderlyingFacts

CAL = SessionCalendar()
SNAPSHOT = parse_snapshot(
    (Path(__file__).parents[1] / "fixtures/finviz/snapshot_F.html").read_text(encoding="utf-8")
)
QT_ID = 38526
LAST_DAY = date(2026, 10, 5)  # the last completed session at T0 (Tuesday 10:00 ET)


def _bars(closes: list[D], *, end: date = LAST_DAY, low_gap: D = D("0.05")) -> list[Candle]:
    """Daily bars for the sessions ending with `end`, oldest first; each low is `low_gap` under its close."""
    days = [*CAL.sessions_before(end, len(closes) - 1), end] if closes else []
    out = []
    for day, close in zip(days, closes, strict=True):
        start = datetime.combine(day, time(0), tzinfo=ET).astimezone(UTC)
        out.append(Candle(start, start + timedelta(days=1), close, close, close - low_gap, close, 1000, None))
    return out


def _line(n: int, first: str = "10", step: str = "0.01") -> list[D]:
    return [D(first) + D(step) * i for i in range(n)]


# --- pure -----------------------------------------------------------------------------------------------


def _with_low_five_back() -> list[Candle]:
    bars = _bars(_line(260))
    c = bars[-6]
    bars[-6] = Candle(c.start, c.end, c.open, c.high, D("1.23"), c.close, c.volume, c.vwap)
    return bars


@pytest.mark.parametrize(
    ("bars", "want"),
    [
        (_bars(_line(260)), TrendInputs(D("12.3450"), D("12.1450"), D("10.03"), 251)),  # rising
        (_bars([D("20")] * 260), TrendInputs(D("20.0000"), D("20.0000"), D("19.95"), 0)),  # flat: newest low
        (_bars(_line(260, "30", "-0.01")), TrendInputs(D("27.6550"), D("27.8550"), D("27.36"), 0)),  # falling
        (_with_low_five_back(), TrendInputs(D("12.3450"), D("12.1450"), D("1.23"), 5)),
        (_bars(_line(69)), TrendInputs(D("10.4350"), None, None, None)),  # too few for the prior average
        (_bars(_line(49)), TrendInputs()),
        ([], TrendInputs()),
    ],
)
def test_trend_inputs(bars: list[Candle], want: TrendInputs) -> None:
    assert trend_inputs(bars) == want
    assert trend_inputs(bars[::-1]) == want  # the order given does not matter


def _details(symbol: str, description: str, security_type: str = "Stock") -> QtSymbolDetails:
    return QtSymbolDetails(
        1,
        symbol,
        description,
        security_type,
        "NYSE",
        "USD",
        True,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
    )


@pytest.mark.parametrize(
    ("details", "want"),
    [
        (_details("F", "FORD MOTOR CO"), "STOCK"),
        (_details("UCTT", "ULTRA CLEAN HOLDINGS INC"), "STOCK"),  # a keyword in a company's name
        (_details("SPY", "SPDR S&P 500 ETF TRUST"), "BROAD_INDEX_ETF"),
        (_details("QQQ", "INVESCO QQQ TRUST SERIES 1"), "BROAD_INDEX_ETF"),  # by ticker alone
        (_details("XLE", "ENERGY SELECT SECTOR SPDR FUND"), "SECTOR_ETF"),
        (_details("ABCD", "SOME COMMODITY POOL", "MutualFund"), "SECTOR_ETF"),
        (_details("TQQQ", "PROSHARES ULTRAPRO QQQ"), "LEVERAGED_OR_INVERSE_ETF"),
        (_details("SPXL", "DIREXION DAILY S&P 500 BULL 3X SHARES"), "LEVERAGED_OR_INVERSE_ETF"),
        (_details("SH", "PROSHARES SHORT S&P500"), "LEVERAGED_OR_INVERSE_ETF"),
        (_details("XYZ", "XYZ INVERSE VIX ETN"), "LEVERAGED_OR_INVERSE_ETF"),
    ],
)
def test_classify_security(details: QtSymbolDetails, want: str) -> None:
    assert classify_security(details) == want


# --- the service ----------------------------------------------------------------------------------------


@dataclass
class Env:
    service: FactsService
    clock: FixedClock
    qt: FakeQtOptions
    symbol_id: int
    snapshot_calls: list[tuple[str, date]]
    bar_calls: list[tuple[str, date, date]]
    down: set[str]  # "finviz" and "candles" fail while named here
    factory: sessionmaker[Session]

    async def refresh(self, ticker: str = "F") -> Any:
        return (await self.service.refresh([ticker]))[ticker]

    def rows(self) -> int:
        with self.factory() as s:
            return int(s.execute(select(func.count()).select_from(m.UnderlyingFacts)).scalar_one())


def _symbol(qt: FakeQtOptions, **changes: Any) -> None:
    details: dict[str, Any] = {
        "description": "FORD MOTOR CO",
        "eps": D("1.10"),
        "market_cap": D("47000000000"),
        "dividend": D("0.15"),
        "ex_date": date(2026, 11, 10),
        "yield_pct": D("5.2"),
        "industry_sector": "Consumer Cyclical",
        **changes,
    }
    qt.add_symbol("F", QT_ID, **details)


@pytest.fixture
def env(db_factory: sessionmaker[Session]) -> Env:
    clock = FixedClock(T0)
    qt = FakeQtOptions(clock)
    _symbol(qt)
    qt.set_share_quote(QT_ID, "11.50")
    with db_factory() as s:
        symbol_id = add_underlying(s, "F", questrade_id=QT_ID)
        s.commit()
    snapshot_calls: list[tuple[str, date]] = []
    bar_calls: list[tuple[str, date, date]] = []
    down: set[str] = set()

    def snapshots(ticker: str, today: date) -> dict[str, str]:
        snapshot_calls.append((ticker, today))
        if "finviz" in down:
            raise FinvizBlocked("/quote.ashx?t=F: HTTP 403")
        return SNAPSHOT

    async def bars(ticker: str, start: date, end: date) -> list[Candle]:
        bar_calls.append((ticker, start, end))
        if "candles" in down:
            raise RuntimeError("no candles today")
        return _bars(_line(260), end=end)

    service = FactsService(db_factory, clock, CAL, qt, snapshots, bars, OptionSettings())
    return Env(service, clock, qt, symbol_id, snapshot_calls, bar_calls, down, db_factory)


def _fails(qt: FakeQtOptions, *methods: str) -> Callable[[], None]:
    for method in methods:
        qt.errors[method] = RuntimeError(f"{method} is down")
    return qt.errors.clear


@pytest.mark.db
async def test_refresh_merges_three_sources_with_sources_map(env: Env) -> None:
    facts = await env.refresh()
    assert facts == UnderlyingFacts(
        symbol_id=env.symbol_id,
        as_of=SESSION,
        ticker="F",
        security_type="STOCK",
        sector="Consumer Cyclical",
        price=D("11.50"),
        eps_ttm=D("1.10"),
        eps_growth_yoy=D("-0.1240"),
        debt_to_equity=D("3.56"),
        book_value_per_share=D("11.32"),
        market_cap_usd=D("47000000000"),  # Questrade's; FinViz's 47.12B is only the fallback
        sma50=D("12.3450"),
        sma50_prior=D("12.1450"),
        low_52w=D("10.03"),
        sessions_since_52w_low=251,
        rsi14=D("48.21"),
        next_earnings_date=date(2026, 10, 22),
        next_ex_dividend_date=date(2026, 11, 10),
        dividend_per_share=D("0.15"),
        dividend_yield=D("0.052"),
        payout_ratio=D("0.5420"),
        short_float=D("0.0310"),
        has_options=True,
        sources=facts.sources,
    )
    assert set(facts.sources) == set(FACT_FIELDS)
    by_source = {name: source for name, (source, _at) in facts.sources.items()}
    assert (by_source["price"], by_source["eps_ttm"]) == ("questrade", "questrade")
    assert {name for name, source in by_source.items() if source == "finviz"} == set(FINVIZ_FIELDS)
    assert by_source["sma50"] == by_source["sessions_since_52w_low"] == "candles"
    assert {at for _source, at in facts.sources.values()} == {T0}
    assert env.snapshot_calls == [("F", SESSION)]
    # 260 sessions that end with the last completed one: today's bar is not complete at 10:00
    assert env.bar_calls == [("F", CAL.sessions_before(SESSION, 260)[0], LAST_DAY)]
    assert await env.service.get("F") == facts


@pytest.mark.db
async def test_one_source_down_keeps_the_rest(env: Env) -> None:
    env.down.add("finviz")
    facts = await env.refresh()
    assert isinstance(facts, UnderlyingFacts)
    assert [name for name in FINVIZ_FIELDS if getattr(facts, name) is not None] == []
    assert not set(FINVIZ_FIELDS) & set(facts.sources)
    assert (facts.price, facts.eps_ttm, facts.sma50) == (D("11.50"), D("1.10"), D("12.3450"))

    # later the same day FinViz answers and Questrade's details do not: the row keeps what it had
    env.down.clear()
    restore = _fails(env.qt, "symbol_details")
    env.clock.advance(timedelta(hours=1))
    later = await env.refresh()
    restore()
    assert isinstance(later, UnderlyingFacts) and env.rows() == 1
    assert (later.eps_ttm, later.security_type, later.rsi14) == (D("1.10"), "STOCK", D("48.21"))
    assert later.sources["eps_ttm"] == ("questrade", T0)
    assert later.sources["rsi14"] == ("finviz", T0 + timedelta(hours=1))
    assert later.market_cap_usd == D("47120000000")  # FinViz stands in while Questrade has none
    assert later.sources["market_cap_usd"][0] == "finviz"


@pytest.mark.db
async def test_all_sources_down_returns_the_error(env: Env) -> None:
    env.down.update({"finviz", "candles"})
    _fails(env.qt, "symbol_details", "quotes")
    error = await env.refresh()
    assert isinstance(error, str)
    assert all(name in error for name in ("questrade details", "questrade quote", "finviz", "candles"))
    assert "HTTP 403" in error and env.rows() == 0
    assert await env.service.get("F") is None
    assert await env.service.refresh(["ZZZZ"]) == {"ZZZZ": "unknown symbol ZZZZ"}


@pytest.mark.db
async def test_get_respects_max_age(env: Env) -> None:
    assert await env.service.get("F") is None
    facts = await env.refresh()
    env.clock.advance(timedelta(hours=36))  # options.facts_max_age_hours
    assert await env.service.get("F") == facts
    env.clock.advance(timedelta(seconds=1))
    assert await env.service.get("F") is None


@pytest.mark.db
async def test_refresh_is_idempotent_per_day(env: Env) -> None:
    first = await env.refresh()
    env.clock.advance(timedelta(hours=2))
    second = await env.refresh()
    assert isinstance(first, UnderlyingFacts) and isinstance(second, UnderlyingFacts)
    assert env.rows() == 1
    assert {at for _source, at in second.sources.values()} == {T0 + timedelta(hours=2)}
    assert second == UnderlyingFacts(**{**_fields(first), "sources": second.sources})

    env.clock.advance(timedelta(days=1))  # Wednesday: a new row, and `get` answers with it
    third = await env.refresh()
    assert isinstance(third, UnderlyingFacts) and env.rows() == 2
    assert third.as_of == SESSION + timedelta(days=1)
    assert await env.service.get("F") == third


def _fields(facts: UnderlyingFacts) -> dict[str, Any]:
    return {name: getattr(facts, name) for name in UnderlyingFacts.__slots__}


@pytest.mark.db
async def test_past_ex_date_is_none(env: Env) -> None:
    _symbol(env.qt, ex_date=LAST_DAY, yield_pct=None)
    facts = await env.refresh()
    assert isinstance(facts, UnderlyingFacts)
    assert (facts.next_ex_dividend_date, facts.dividend_yield) == (None, None)
    assert facts.dividend_per_share == D("0.15") and facts.sources["next_ex_dividend_date"][0] == "questrade"

    _symbol(env.qt, ex_date=SESSION)  # today still counts
    today = await env.refresh()
    assert isinstance(today, UnderlyingFacts) and today.next_ex_dividend_date == SESSION
