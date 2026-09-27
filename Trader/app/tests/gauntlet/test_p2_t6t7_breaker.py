"""P2-B2 gauntlet breaker tests for P2-T6 (strategy framework) and P2-T7 (market data service).

Fakes and the testcontainers database only; never real Questrade. Several tests collect every problem they
find before asserting, so one failure report names all the broken cases at once.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from importlib.metadata import EntryPoint
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from tests.factories import add_symbol
from tests.fakes_questrade import FakeQuestrade
from tests.strategies.demo_plugin import DemoStrategy
from trader.adapters.questrade.client import QuestradeApiError
from trader.db import models as m
from trader.market import repository as repo
from trader.market.calendar import SessionCalendar
from trader.market.clock import FixedClock
from trader.market.data_service import MarketDataService
from trader.market.types import Candle, UniverseStatus
from trader.strategies import registry as reg
from trader.strategies.base import EnterLong, Exit, SessionOffset
from trader.strategies.registry import PluginError, StrategyRegistry

CAL = SessionCalendar()
CLOCK = FixedClock(datetime(2026, 10, 6, 12, 0, tzinfo=UTC))


# --- helpers ------------------------------------------------------------------------------------------------
class ShadowDemo(DemoStrategy):
    """A second plug-in that claims the key "demo" (e.g. a stray package shadowing a real strategy)."""

    version = "9.9.9"


class DemoV2(DemoStrategy):
    version = "0.2.0"


class NotAModel:
    pass


class BadParamsModel(DemoStrategy):
    key = "bad_model"
    params_model = NotAModel  # type: ignore[assignment]


def not_a_plugin() -> None:
    """An entry point that loads a function, not a class."""


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


def daily(d: date, close: str) -> Candle:
    start = datetime.combine(d, datetime.min.time(), tzinfo=UTC) + timedelta(hours=5)  # 00:00 EST
    return Candle(
        start, start + timedelta(days=1), Decimal("1"), Decimal("99"), Decimal("1"), Decimal(close), 1, None
    )


def _patch_entry_points(monkeypatch: pytest.MonkeyPatch, *pairs: tuple[str, str]) -> None:
    eps = [EntryPoint(name=n, value=v, group=reg.ENTRY_POINT_GROUP) for n, v in pairs]
    monkeypatch.setattr(reg, "entry_points", lambda group: [e for e in eps if e.group == group])


# --- 1. SessionOffset.parse boundaries --------------------------------------------------------------------
def test_session_offset_zero_long_and_malformed_strings() -> None:
    zero_open = SessionOffset.parse("open+0m")
    zero_close = SessionOffset.parse("close-0m")
    assert (zero_open.anchor, zero_open.seconds, str(zero_open)) == ("open", 0, "open")
    assert (zero_close.anchor, zero_close.seconds, str(zero_close)) == ("close", 0, "close")
    assert zero_close == SessionOffset.parse("close")  # canonical forms compare equal
    full = SessionOffset.parse("open+390m")  # a whole regular session
    assert full.seconds == 390 * 60 and SessionOffset.parse(str(full)) == full

    accepted: list[str] = []
    for bad in [
        "open+5m\n",  # a trailing newline from a form or a YAML block scalar
        "close-30m\n",
        "OPEN+5m",
        "open+-5m",
        "open+5m5s5s",
        "open+5m 5s",
        " open+5m",
        "open+1000m",
        "open+5s",
        "close-",
    ]:
        try:
            SessionOffset.parse(bad)
        except ValueError:
            continue
        accepted.append(repr(bad))
    assert accepted == [], f"SessionOffset.parse accepted malformed offsets: {accepted}"


# --- 2. SessionOffset.resolve across DST, early closes and holidays --------------------------------------
def test_session_offset_resolves_across_dst_early_closes_and_holidays() -> None:
    open_, close = SessionOffset.parse("open"), SessionOffset.parse("close-0m")
    # DST ends Sun 2026-11-01: the 09:30 ET open moves from 13:30Z to 14:30Z.
    assert open_.resolve(CAL, date(2026, 10, 30)) == datetime(2026, 10, 30, 13, 30, tzinfo=UTC)
    assert open_.resolve(CAL, date(2026, 11, 2)) == datetime(2026, 11, 2, 14, 30, tzinfo=UTC)
    # DST starts Sun 2026-03-08: the open moves from 14:30Z to 13:30Z.
    assert open_.resolve(CAL, date(2026, 3, 6)) == datetime(2026, 3, 6, 14, 30, tzinfo=UTC)
    assert open_.resolve(CAL, date(2026, 3, 9)) == datetime(2026, 3, 9, 13, 30, tzinfo=UTC)
    assert SessionOffset.parse("close-10m").resolve(CAL, date(2026, 3, 9)) == datetime(
        2026, 3, 9, 19, 50, tzinfo=UTC
    )
    # Early closes (13:00 ET, EST = 18:00Z): the close anchor follows the real close.
    for early in (date(2026, 11, 27), date(2026, 12, 24)):
        at = close.resolve(CAL, early)
        assert at == datetime(early.year, early.month, early.day, 18, 0, tzinfo=UTC), early
        assert at.utcoffset() == timedelta(0)
    # Holidays and a weekend are not sessions.
    for holiday in (
        date(2026, 11, 26),
        date(2026, 12, 25),
        date(2026, 7, 3),
        date(2026, 4, 3),
        date(2026, 10, 10),
    ):
        with pytest.raises(ValueError):
            close.resolve(CAL, holiday)


# --- 3. Intents: prices must be Decimal ------------------------------------------------------------------
def test_intents_refuse_float_prices() -> None:
    """Global Constraints: never float for money. The intent is where a plug-in's prices enter the engine,
    so a float there flows into sizing, risk and the broker (0.1 + 0.2 style drift in stops and R)."""
    accepted: list[str] = []
    cases: list[tuple[str, Any]] = [
        ("EnterLong.stop_loss", lambda: EnterLong(1, "stop", Decimal("20.01"), None, 19.91, "orb")),  # type: ignore[arg-type]
        ("EnterLong.stop", lambda: EnterLong(1, "stop", 20.01, None, Decimal("19.91"), "orb")),  # type: ignore[arg-type]
        ("EnterLong.limit", lambda: EnterLong(1, "limit", None, 20.0, Decimal("19.91"), "orb")),  # type: ignore[arg-type]
        ("Exit.stop", lambda: Exit(1, "stop", 19.5, "trail")),  # type: ignore[arg-type]
    ]
    for name, make in cases:
        try:
            make()
        except (TypeError, ValueError):
            continue
        accepted.append(name)
    assert accepted == [], f"intents accepted float prices in {accepted}"


# --- 4. Registry: duplicate, broken and malformed entry points -------------------------------------------
def test_registry_refuses_duplicate_broken_and_malformed_entry_points(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    here = "tests.gauntlet.test_p2_t6t7_breaker"
    problems: list[str] = []

    _patch_entry_points(
        monkeypatch,
        ("demo", "tests.strategies.demo_plugin:DemoStrategy"),
        ("demo", f"{here}:ShadowDemo"),
    )
    try:
        got = reg.load_plugin("demo")
        problems.append(f"two entry points named 'demo' silently resolved to {got.__name__}")
    except PluginError:
        pass

    _patch_entry_points(
        monkeypatch,
        ("demo", "tests.strategies.demo_plugin:DemoStrategy"),
        ("broken", "tests.gauntlet.no_such_module_p2b2:Nope"),
        ("func", f"{here}:not_a_plugin"),
        ("bad_model", f"{here}:BadParamsModel"),
    )
    for name in ("broken", "func", "bad_model"):
        try:
            reg.load_plugin(name)
            problems.append(f"{name}: loaded without error")
        except PluginError:
            pass
        except Exception as exc:  # noqa: BLE001 - the point is which type escapes
            problems.append(f"{name}: raised {type(exc).__name__} instead of PluginError")
    try:
        reg.load_all()
        problems.append("load_all() succeeded with a broken plug-in declared")
    except PluginError:
        pass
    except Exception as exc:  # noqa: BLE001
        problems.append(f"load_all(): raised {type(exc).__name__} instead of PluginError")
    assert problems == [], problems


# --- 5. Registry: every change is exactly one new revision -----------------------------------------------
@pytest.mark.db
def test_registry_revision_bumps_on_every_change_and_only_then(db_factory: sessionmaker[Session]) -> None:
    r = StrategyRegistry(db_factory, CLOCK, plugins={"demo": DemoStrategy})
    r.ensure_defaults()
    assert r.current("demo").revision == 1
    assert r.update("demo", params={"threshold": 7}, actor="t").revision == 2
    assert r.update("demo", params={"threshold": "7"}, actor="t").revision == 2  # coerces to the same value
    assert r.update("demo", params={}, actor="t").revision == 2
    assert r.update("demo", enabled=True, actor="t").revision == 2
    assert r.update("demo", enabled=False, actor="t").revision == 3
    assert r.update("demo", enabled=True, actor="t").revision == 4
    for bad in ({"threshold": 0}, {"threshold": 7.5}, {"at": None}, {"nope": 1}):
        with pytest.raises(ValidationError):
            r.update("demo", params=bad, actor="t")
    assert r.current("demo").revision == 4

    # A new plug-in version is a new revision that keeps the params and the enabled flag.
    r2 = StrategyRegistry(db_factory, CLOCK, plugins={"demo": DemoV2})
    r2.ensure_defaults()
    r2.ensure_defaults()
    v = r2.current("demo")
    assert (v.revision, v.version, v.params["threshold"], v.enabled) == (5, "0.2.0", 7, True)

    # Two concurrent changes both land, each as its own revision (the advisory lock serialises them).
    with ThreadPoolExecutor(max_workers=2) as pool:
        views = list(pool.map(lambda t: r2.update("demo", params={"threshold": t}, actor="t"), [2, 3]))
    assert sorted(x.revision for x in views) == [6, 7]

    with db_factory() as s:
        revs = (
            s.execute(select(m.StrategyConfig.revision).order_by(m.StrategyConfig.revision)).scalars().all()
        )
        audits = s.execute(select(func.count()).select_from(m.AuditLog)).scalar_one()
    assert revs == [1, 2, 3, 4, 5, 6, 7]
    assert audits == 5  # update() revisions 2, 3, 4, 6 and 7; failed validations write nothing


# --- 6. opening_bars: every missing symbol is reported with its reason (Review Focus 4) -------------------
@pytest.mark.db
async def test_opening_bars_report_each_missing_symbol_on_an_early_close_day(
    db_factory: sessionmaker[Session],
) -> None:
    day = date(2026, 11, 27)  # early close, EST: open 14:30Z
    open_ = CAL.session_open(day)
    with db_factory() as s:
        ids = {
            t: add_symbol(s, t, questrade_id=qid)
            for t, qid in [("CACH", 201), ("ERR", 203), ("EMPT", 204), ("LATE", 205), ("GOOD", 206)]
        }
        ids["NOQT"] = add_symbol(s, "NOQT")
        repo.upsert_intraday_candles(s, ids["CACH"], "5m", [c5(open_)])
        s.commit()
    qt = FakeQuestrade()
    qt.errors[203] = 429
    qt.add_bars(205, "FiveMinutes", [c5(open_ + timedelta(minutes=5))])  # only the 09:35 bar
    qt.add_bars(206, "FiveMinutes", [c5(open_, volume=8000)])
    order = [ids[t] for t in ("CACH", "NOQT", "ERR", "EMPT", "LATE", "GOOD", "GOOD", "CACH")]

    svc = MarketDataService(db_factory, FixedClock(open_ + timedelta(minutes=5, seconds=5)), CAL, qt)
    got = await svc.opening_bars(day, order)
    assert set(got.bars) == {ids["CACH"], ids["GOOD"]}
    assert got.bars[ids["GOOD"]].volume == 8000
    assert got.missing == {
        ids["NOQT"]: "no_questrade_id",
        ids["ERR"]: "questrade_error: HTTP 429",
        ids["EMPT"]: "no_bar_at_open",
        ids["LATE"]: "no_bar_at_open",
    }
    assert set(got.bars).isdisjoint(got.missing)
    assert qt.calls == [("candles_many", 4)]  # one batch, duplicates collapsed, the cached bar not refetched

    # Before the bar completes, a fetched bar is reported, not used; an empty request is a no-op.
    qt2 = FakeQuestrade()
    qt2.add_bars(206, "FiveMinutes", [c5(open_)])
    early = MarketDataService(db_factory, FixedClock(open_ + timedelta(minutes=4)), CAL, qt2)
    with db_factory() as s:
        s.execute(m.IntradayCandle.__table__.delete().where(m.IntradayCandle.symbol_id == ids["GOOD"]))
        s.commit()
    got2 = await early.opening_bars(day, [ids["GOOD"]])
    assert got2.bars == {} and got2.missing == {ids["GOOD"]: "bar_not_complete"}
    empty = await early.opening_bars(day, [])
    assert empty.bars == {} and empty.missing == {}


# --- 7. candles(): partial cache, a still-forming bar, naive datetimes, Questrade errors ---------------
@pytest.mark.db
async def test_candles_partial_cache_forming_bar_naive_times_and_errors(
    db_factory: sessionmaker[Session],
) -> None:
    day = date(2026, 10, 6)
    open_ = CAL.session_open(day)  # 13:30Z
    t = [open_ + timedelta(minutes=5 * i) for i in range(3)]
    with db_factory() as s:
        part, form, naive_id, err = (
            add_symbol(s, "PART", questrade_id=301),
            add_symbol(s, "FORM", questrade_id=302),
            add_symbol(s, "NAIV", questrade_id=303),
            add_symbol(s, "ERRS", questrade_id=304),
        )
        repo.upsert_intraday_candles(s, part, "5m", [c5(t[0]), c5(t[2])])  # 13:35 is missing
        repo.upsert_intraday_candles(s, naive_id, "5m", [c5(x) for x in t])
        repo.upsert_intraday_candles(s, err, "5m", [c5(t[0])])
        s.commit()
    qt = FakeQuestrade()
    qt.add_bars(301, "FiveMinutes", [c5(x, volume=6000) for x in t])
    end = open_ + timedelta(minutes=15)

    # (a) A partial cache is refilled from Questrade, and then served from the cache.
    later = MarketDataService(db_factory, FixedClock(open_ + timedelta(minutes=30)), CAL, qt)
    got = await later.candles(part, open_, end, "FiveMinutes")
    assert [c.start for c in got] == t
    assert await later.candles(part, open_, end, "FiveMinutes") == got
    assert qt.calls == [("candles", 1)]

    problems: list[str] = []
    # (b) A bar still forming when first fetched must not be frozen in the cache as if final.
    qt.add_bars(302, "FiveMinutes", [c5(t[0]), c5(t[1]), c5(t[2], volume=100)])
    mid = MarketDataService(db_factory, FixedClock(open_ + timedelta(minutes=12)), CAL, qt)
    await mid.candles(form, open_, end, "FiveMinutes")
    qt.bars[(302, "FiveMinutes")][-1] = c5(t[2], volume=9000)  # the bar completed at 13:45
    final = await MarketDataService(db_factory, FixedClock(open_ + timedelta(minutes=20)), CAL, qt).candles(
        form, open_, end, "FiveMinutes"
    )
    if not final or final[-1].volume != 9000:
        problems.append(
            f"stale cache: the 13:40 bar fetched while forming is served later with volume "
            f"{final[-1].volume if final else None} instead of the final 9000"
        )

    # (c) Naive datetimes are ambiguous against timestamptz: refuse them instead of guessing a zone.
    naive_start, naive_end = open_.replace(tzinfo=None), end.replace(tzinfo=None)
    try:
        await later.candles(naive_id, naive_start, naive_end, "FiveMinutes")
        problems.append("naive start/end accepted (interpreted in the DB session's time zone)")
    except ValueError:
        pass
    except Exception as exc:  # noqa: BLE001
        problems.append(f"naive start/end raised {type(exc).__name__}, not ValueError")

    # (d) A Questrade error with only part of the window cached must not crash the caller (Review Focus 4).
    qt.errors[304] = 503
    try:
        await later.candles(err, open_, end, "FiveMinutes")
    except QuestradeApiError as exc:
        problems.append(f"candles() raised QuestradeApiError HTTP {exc.status} at a decision point")
    assert problems == [], problems


# --- 8. quotes keyed by DB id, a stale fallback universe, prior_close across holidays ---------------------
@pytest.mark.db
async def test_quote_ids_fallback_stale_status_and_prior_close_across_holidays(
    db_factory: sessionmaker[Session],
) -> None:
    after_thanksgiving = date(2026, 11, 27)
    with db_factory() as s:
        a, b, c = add_symbol(s, "AAA"), add_symbol(s, "BBB"), add_symbol(s, "CCC")
        # Questrade IDs that collide numerically with the other rows' DB ids, crosswise.
        s.get(m.Symbol, a).questrade_id = b  # type: ignore[union-attr]
        s.get(m.Symbol, b).questrade_id = a  # type: ignore[union-attr]
        s.get(m.Symbol, c).questrade_id = 999_001  # type: ignore[union-attr]
        s.commit()
    with db_factory() as s, pytest.raises(IntegrityError):  # two rows can never share a Questrade id
        add_symbol(s, "DUP", questrade_id=999_001)
    now = datetime(2026, 11, 27, 15, 0, tzinfo=UTC)
    qt = FakeQuestrade()
    qt.add_symbol("AAA", b)
    qt.add_symbol("BBB", a)
    qt.set_quote(b, "10.00", "10.02", "10.01", now)
    qt.set_quote(a, "50.00", "50.05", "50.02", now)
    svc = MarketDataService(db_factory, FixedClock(now), CAL, qt)
    quotes = await svc.quotes([a, b, a])
    assert set(quotes) == {a, b}
    assert (quotes[a].symbol, quotes[a].symbol_id, quotes[a].ask) == ("AAA", a, Decimal("10.02"))
    assert (quotes[b].symbol, quotes[b].symbol_id, quotes[b].ask) == ("BBB", b, Decimal("50.05"))
    assert qt.calls == [("quotes", 2)]

    # A fallback universe the nightly job judged stale stays stale; a later failed re-run doesn't clear it.
    with db_factory() as s:
        s.add(
            m.UniverseSnapshot(
                session_date=after_thanksgiving,
                symbol_id=a,
                price=None,
                avg_volume=None,
                atr14=None,
                source="fallback",
            )
        )
        verdict = {
            "source": "fallback",
            "fallback_from": "2026-11-17",
            "fallback_stale": True,
            "fallback_age_sessions": 7,
        }
        s.add(
            m.JobRun(
                job="nightly",
                session_date=after_thanksgiving,
                started_at=now,
                finished_at=now,
                status="succeeded",
                error=None,
                detail=verdict,
            )
        )
        s.add(
            m.JobRun(
                job="nightly",
                session_date=after_thanksgiving,
                started_at=now,
                finished_at=now,
                status="failed",
                error="boom",
                detail=None,
            )
        )
        s.add(
            m.JobRun(
                job="nightly",
                session_date=date(2026, 11, 25),
                started_at=now,
                finished_at=now,
                status="succeeded",
                error=None,
                detail={"source": "finviz"},
            )
        )
        s.commit()
    assert await svc.universe_status(after_thanksgiving) == UniverseStatus(
        "fallback", date(2026, 11, 17), True, 7
    )

    # prior_close skips Thanksgiving (to Wed 11-25) and Good Friday (Mon 04-06 -> Thu 04-02).
    with db_factory() as s:
        repo.upsert_daily_candles(s, a, [daily(date(2026, 11, 25), "20.25"), daily(date(2026, 11, 26), "99")])
        repo.upsert_daily_candles(s, a, [daily(date(2026, 4, 2), "18.50"), daily(date(2026, 4, 3), "77")])
        s.commit()
    assert await svc.prior_close(a, after_thanksgiving) == Decimal("20.25")
    assert await svc.prior_close(a, date(2026, 4, 6)) == Decimal("18.50")
    assert await svc.prior_closes([a], after_thanksgiving) == {a: Decimal("20.25")}
    qt.add_bars(
        a, "OneDay", [daily(date(2026, 11, 24), "30.00"), daily(date(2026, 11, 25), "31.00")]
    )  # BBB's qid
    assert await svc.prior_close(b, after_thanksgiving) == Decimal("31.00")  # Questrade fallback, 11-25 bar
    assert await svc.prior_closes([a, b, c], after_thanksgiving) == {a: Decimal("20.25"), b: Decimal("31.00")}
