"""OPTSIM T3: the option market service (contract master, chain cache, quotes, marks, calendar maths)."""

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from tests.options.factories import EXPIRY, T0, add_underlying
from tests.options.fakes import FakeQtOptions
from trader.db import models as m
from trader.market.calendar import SessionCalendar
from trader.market.clock import ET, FixedClock
from trader.market.types import Candle
from trader.options.market import OptionMarketService, monthly_expiry
from trader.options.protocols import UnknownContract, UnknownUnderlying
from trader.options.settings import OptionSettings
from trader.options.types import ChainStrike, ContractKey, ExpiryInfo

D = Decimal
CAL = SessionCalendar()
WEEKLY = date(2026, 11, 13)
F_QID = 1001
# (strike, Questrade call id, Questrade put id) of the November monthly
MONTHLY_STRIKES = [("14", 2001, 2002), ("14.5", 2003, 2004), ("15", 2005, 2006)]


def service(factory: Any, clock: FixedClock, qt: FakeQtOptions) -> OptionMarketService:
    return OptionMarketService(factory, clock, CAL, qt, lambda: OptionSettings())


def setup(
    factory: sessionmaker[Session], now: datetime = T0
) -> tuple[OptionMarketService, FakeQtOptions, FixedClock, int]:
    """Ford in `symbols` and at Questrade, with a weekly (one strike) and the monthly (three strikes)."""
    clock = FixedClock(now)
    qt = FakeQtOptions(clock)
    qt.add_symbol("F", F_QID)
    qt.add_expiry(F_QID, WEEKLY, [("14.5", 2011, 2012)])
    qt.add_expiry(F_QID, EXPIRY, MONTHLY_STRIKES)
    with factory() as s:
        symbol_id = add_underlying(s, "F", questrade_id=F_QID)
        s.commit()
    return service(factory, clock, qt), qt, clock, symbol_id


def calls(qt: FakeQtOptions, method: str) -> list[tuple[Any, ...]]:
    return [args for name, args in qt.calls if name == method]


async def put_id(svc: OptionMarketService, strike: str = "14.5") -> int:
    found = await svc.find_contract(ContractKey("F", EXPIRY, D(strike), "put"))
    assert found is not None
    return found.id


@pytest.mark.db
async def test_chain_is_cached_and_refetched_after_ttl(db_factory: sessionmaker[Session]) -> None:
    svc, qt, clock, _ = setup(db_factory)
    want = [ExpiryInfo(WEEKLY, 38, False, 1), ExpiryInfo(EXPIRY, 45, True, 3)]
    assert await svc.expiries("F") == want
    assert await svc.expiries("f") == want and len(calls(qt, "option_chain")) == 1
    clock.advance(timedelta(hours=13))  # 23:00 ET, the same session date
    assert await service(db_factory, clock, qt).expiries("F") == want  # another process reads the same cache
    assert await svc.refresh_chain("F") == 0 and len(calls(qt, "option_chain")) == 1
    clock.advance(timedelta(hours=11))  # 24 h old: no longer younger than options.chain_cache_hours
    assert len(await svc.expiries("F")) == 2 and len(calls(qt, "option_chain")) == 2
    assert await svc.refresh_chain("F", force=True) == 8 and len(calls(qt, "option_chain")) == 3
    qt.errors["option_chain"] = RuntimeError("down")
    clock.advance(timedelta(hours=30))
    assert len(await svc.expiries("F")) == 2  # a failed fetch serves the old chain
    with pytest.raises(RuntimeError):
        await svc.refresh_chain("F", force=True)


@pytest.mark.db
async def test_contracts_upsert_is_idempotent(db_factory: sessionmaker[Session]) -> None:
    svc, _, clock, _ = setup(db_factory)
    assert await svc.refresh_chain("F") == 8
    first = await svc.strikes("F", EXPIRY)
    assert [k.strike for k in first] == [D("14"), D("14.5"), D("15")]
    assert None not in {i for k in first for i in (k.call_id, k.put_id)}
    assert len({i for k in first for i in (k.call_id, k.put_id)}) == 6
    clock.advance(timedelta(hours=1))
    assert await svc.refresh_chain("F", force=True) == 8
    assert await svc.strikes("F", EXPIRY) == first  # the same contract ids
    assert await svc.strikes("F", date(2026, 12, 18)) == []
    with db_factory() as s:
        assert s.execute(select(func.count()).select_from(m.OptionContract)).scalar_one() == 8
        assert s.execute(select(func.min(m.OptionContract.first_seen_at))).scalar_one() == T0
        assert s.execute(select(func.max(m.OptionContract.first_seen_at))).scalar_one() == T0
        cache = s.execute(select(m.OptionChainCache)).scalar_one()
        assert (cache.expiries, cache.fetched_at) == (2, clock.now())
        assert cache.chain[1] == {
            "expiry": "2026-11-20",
            "root": "F",
            "multiplier": 100,
            "strikes": [{"strike": k, "call_id": call, "put_id": put} for k, call, put in MONTHLY_STRIKES],
        }


@pytest.mark.db
async def test_adjusted_contract_is_flagged(db_factory: sessionmaker[Session]) -> None:
    svc, qt, _, _ = setup(db_factory)
    qt.add_expiry(F_QID, date(2026, 12, 18), [("14.5", 2021, 2022)], root="F1")
    qt.add_expiry(F_QID, date(2027, 1, 15), [("14.5", 2031, 2032)], multiplier=50)
    rows: dict[date, ChainStrike] = {}
    for expiry in (EXPIRY, date(2026, 12, 18), date(2027, 1, 15)):
        rows[expiry] = (await svc.strikes("F", expiry))[-1]
    got = {e: await svc.contract(k.put_id or 0) for e, k in rows.items()}
    assert [(c.adjusted, c.root, c.multiplier, c.is_monthly) for c in got.values()] == [
        (False, "F", 100, True),
        (True, "F1", 100, True),
        (True, "F", 50, True),
    ]
    weekly = await svc.contract((await svc.strikes("F", WEEKLY))[0].call_id or 0)
    assert (weekly.is_monthly, weekly.right, weekly.qt_symbol_id) == (False, "call", 2011)


@pytest.mark.parametrize(
    ("expiry", "monthly"),
    [
        (date(2026, 11, 20), True),  # a normal month: the third Friday
        (date(2025, 4, 17), True),  # Good Friday is the third Friday: the Thursday before
        (date(2025, 4, 18), False),  # Good Friday itself
        (date(2026, 6, 18), True),  # Juneteenth on the third Friday
        (date(2026, 11, 13), False),  # a weekly
        (date(2028, 1, 21), True),  # a LEAPS January
        (date(2032, 1, 16), True),  # beyond the calendar: the third Friday is assumed
    ],
)
def test_monthly_expiry(expiry: date, monthly: bool) -> None:
    assert (monthly_expiry(expiry.year, expiry.month, CAL) == expiry) is monthly
    clock = FixedClock(T0)
    assert service(None, clock, FakeQtOptions(clock)).is_monthly(expiry) is monthly


@pytest.mark.db
async def test_quotes_map_iv_and_keep_signed_delta(db_factory: sessionmaker[Session]) -> None:
    svc, qt, clock, _ = setup(db_factory)
    put, bare = await put_id(svc), await put_id(svc, "15")
    qt.set_option_quote(2004, "0.40", "0.45", iv_pct="35.2", delta="-0.31", theta="-0.012", delay=None)
    qt.set_option_quote(2006, "0.60", None)
    assert await svc.quotes([]) == {} and calls(qt, "option_quotes") == []
    clock.advance(timedelta(seconds=30))
    got = await svc.quotes([put, bare, 999_999, await put_id(svc, "14")])
    assert set(got) == {put, bare}  # an unknown id and a contract with no quote are left out
    q = got[put]
    assert (q.contract_id, q.bid, q.ask, q.iv, q.delta, q.theta) == (
        put,
        D("0.40"),
        D("0.45"),
        D("0.352"),
        D("-0.31"),
        D("-0.012"),
    )
    assert (q.delay, q.is_halted, q.fetched_at) == (None, False, clock.now())
    assert (got[bare].iv, got[bare].delta, got[bare].ask) == (None, None, None)
    assert calls(qt, "option_quotes") == [((2004, 2006, 2002),)]


@pytest.mark.db
async def test_quotes_for_expiry_uses_strike_bounds(db_factory: sessionmaker[Session]) -> None:
    svc, qt, _, _ = setup(db_factory)
    for option_id in (2002, 2004, 2006, 2003):
        qt.set_option_quote(option_id, "0.40", "0.45", iv_pct="30")
    got = await svc.quotes_for_expiry("F", EXPIRY, "put", D("14.5"))
    assert [(c.strike, c.right, c.qt_symbol_id, q.contract_id == c.id) for c, q in got] == [
        (D("14.5"), "put", 2004, True),
        (D("15"), "put", 2006, True),
    ]
    assert got[0][1].iv == D("0.3")
    assert len(await svc.quotes_for_expiry("F", EXPIRY, "put", None, D("14.5"))) == 2
    assert calls(qt, "option_quotes_filter") == [
        (F_QID, EXPIRY, "put", D("14.5"), None),
        (F_QID, EXPIRY, "put", None, D("14.5")),
    ]


@pytest.mark.db
async def test_unknown_or_optionless_underlying_raises(db_factory: sessionmaker[Session]) -> None:
    svc, qt, _, symbol_id = setup(db_factory)
    qt.add_symbol("BOND", 1002, has_options=False)
    qt.add_symbol("GM", 1003)
    with db_factory() as s:
        add_underlying(s, "OLD")  # a symbols row without a Questrade id
        s.commit()
    for ticker in ("NOPE", "BOND", "OLD", ""):
        with pytest.raises(UnknownUnderlying):
            await svc.resolve_underlying(ticker)
        with pytest.raises(UnknownUnderlying):
            await svc.expiries(ticker)
    assert await svc.resolve_underlying("F") == symbol_id
    gm = await svc.resolve_underlying("gm")  # looked up at Questrade and stored
    assert await svc.resolve_underlying("GM") == gm and len(calls(qt, "symbols_by_names")) == 5
    with db_factory() as s:
        assert s.execute(select(m.Symbol.ticker, m.Symbol.questrade_id).order_by(m.Symbol.id)).all() == [
            ("F", F_QID),
            ("OLD", None),
            ("GM", 1003),
        ]


@pytest.mark.db
async def test_find_contract_by_key(db_factory: sessionmaker[Session]) -> None:
    svc, _, _, symbol_id = setup(db_factory)
    found = await svc.find_contract(ContractKey("F", EXPIRY, D("14.50"), "put"))  # loads the chain itself
    assert found is not None and await svc.contract(found.id) == found
    assert (found.underlying, found.underlying_symbol_id, found.qt_symbol_id, found.strike) == (
        "F",
        symbol_id,
        2004,
        D("14.5"),
    )
    assert await svc.find_contract(ContractKey("F", EXPIRY, D("99"), "put")) is None
    assert await svc.find_contract(ContractKey("NOPE", EXPIRY, D("14.5"), "put")) is None
    with pytest.raises(UnknownContract):
        await svc.contract(999_999)


@pytest.mark.db
async def test_record_marks_upserts_latest(db_factory: sessionmaker[Session]) -> None:
    svc, qt, clock, symbol_id = setup(db_factory)
    put = await put_id(svc)
    qt.set_option_quote(2004, "0.40", "0.45", iv_pct="35.2", delta="-0.31", open_interest=1200)
    qt.set_share_quote(F_QID, "14.80")
    share = await svc.underlying_quote("F")
    assert share is not None and (share.symbol_id, share.last) == (symbol_id, D("14.80"))
    assert await svc.record_marks([put, await put_id(svc, "15")]) == 1  # the other contract has no quote
    clock.advance(timedelta(minutes=1))
    qt.set_option_quote(2004, "0.50", "0.55", iv_pct="36", delta="-0.35")
    qt.set_share_quote(F_QID, "14.60")
    assert await svc.record_marks([put]) == 1
    with db_factory() as s:
        row = s.execute(select(m.OptionQuoteMark)).scalar_one()  # one row per contract: the latest
        assert (row.contract_id, row.bid, row.ask, row.iv, row.delta) == (
            put,
            D("0.50"),
            D("0.55"),
            D("0.36"),
            D("-0.35"),
        )
        assert (row.underlying_price, row.fetched_at, row.delay) == (D("14.60"), clock.now(), 0)
    mark = (await svc.marks([put, 999_999]))[put]  # the stored mark keeps its fetch time
    assert (mark.bid, mark.iv, mark.fetched_at) == (D("0.50"), D("0.36"), clock.now())
    assert await svc.record_marks([]) == 0


@pytest.mark.parametrize(
    ("now", "is_open"),
    [
        (datetime(2026, 10, 6, 13, 29, 59, tzinfo=UTC), False),  # before the open
        (datetime(2026, 10, 6, 13, 30, tzinfo=UTC), True),  # at the open
        (datetime(2026, 10, 6, 19, 59, 59, tzinfo=UTC), True),
        (datetime(2026, 10, 6, 20, 0, tzinfo=UTC), False),  # at the close
        (datetime(2026, 11, 27, 17, 59, tzinfo=UTC), True),  # the day after Thanksgiving closes at 13:00 ET
        (datetime(2026, 11, 27, 18, 0, tzinfo=UTC), False),
        (datetime(2026, 12, 25, 16, 0, tzinfo=UTC), False),  # a holiday
        (datetime(2026, 10, 10, 16, 0, tzinfo=UTC), False),  # a Saturday
    ],
)
def test_is_open(now: datetime, is_open: bool) -> None:
    clock = FixedClock(T0)
    assert service(None, clock, FakeQtOptions(clock)).is_open(now) is is_open


def day_bar(d: date, close: str) -> Candle:
    start = datetime.combine(d, time(0), tzinfo=ET).astimezone(UTC)
    price = D(close)
    return Candle(start, start + timedelta(days=1), price, price, price, price, 1000, None)


@pytest.mark.db
async def test_daily_bars_fill_gaps_once(db_factory: sessionmaker[Session]) -> None:
    svc, qt, clock, symbol_id = setup(db_factory)  # Tuesday 2026-10-06, 10:00 ET
    days = [date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 5), date(2026, 10, 6)]
    qt.set_candles(F_QID, [day_bar(d, f"14.{i}") for i, d in enumerate(days)])
    with db_factory() as s:
        s.add(
            m.DailyCandle(
                symbol_id=symbol_id,
                date=days[0],
                volume=5,
                **dict.fromkeys(("open", "high", "low", "close"), D("13.9")),
            )
        )
        s.commit()
    got = await svc.daily_bars("F", days[0], days[3])
    # the stored day is kept, the two missing sessions are fetched, today's unfinished session is not
    assert [(c.start.astimezone(ET).date(), c.close) for c in got] == [
        (days[0], D("13.9")),
        (days[1], D("14.1")),
        (days[2], D("14.2")),
    ]
    assert got[1] == day_bar(days[1], "14.1")
    asked = [(args[1].astimezone(ET).date(), args[2].astimezone(ET).date()) for args in calls(qt, "candles")]
    assert asked == [(days[1], days[3])]
    assert await svc.daily_bars("F", days[0], days[3]) == got and len(calls(qt, "candles")) == 1
    clock.set(datetime(2026, 10, 6, 20, 0, tzinfo=UTC))  # the close: today's bar is now due
    assert len(await svc.daily_bars("F", days[0], days[3])) == 4 and len(calls(qt, "candles")) == 2
    with pytest.raises(UnknownUnderlying):
        await svc.daily_bars("NOPE", days[0], days[3])


@pytest.mark.db
async def test_dte_uses_the_et_date(db_factory: sessionmaker[Session]) -> None:
    # 02:00 UTC on the 7th is still the 6th in New York
    svc, _, clock, _ = setup(db_factory, datetime(2026, 10, 7, 2, 0, tzinfo=UTC))
    assert [(e.expiry, e.dte) for e in await svc.expiries("F")] == [(WEEKLY, 38), (EXPIRY, 45)]
    clock.set(datetime(2026, 11, 14, 14, 0, tzinfo=UTC))  # the weekly has expired; the cache still lists it
    assert [(e.expiry, e.dte) for e in await svc.expiries("F")] == [(EXPIRY, 6)]
