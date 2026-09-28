"""P5-T18 acceptance tests 1-2: the replay golden test (SPEC §15.1 promotion criterion 3, §16).

Scenario `orb_week`: sessions 2026-11-23 to 2026-11-27 through the REAL offline composition
(`open_replay_deps(core, data_mode="offline")`: `ReplayData` over the database, `ReplayCatalysts` over stored
rows, the real `orb_sip` and `spy_overlay` with their default params, `SimBroker` + `CandleFillModel`), with
committed fixture data (`golden/orb_week_data.json`) and the committed expected result
(`golden/orb_week_expected.json`):

- Mon 23: BBB breaks out and the SAME 1-minute bar touches its stop: entered, then stopped out at
  `stop - slip - hs` (the worst case, never the favourable order).
- Tue 24: CCC breaks out, then a later bar opens below the stop (a gap): filled at the open less costs.
- Wed 25: no universe snapshot (a biased day: the names of the newest snapshot, numbers from the daily bars);
  AAA breaks out and holds; SPY is below its prior close at 15:30, so the overlay exits it.
- Thu 26: Thanksgiving, skipped.
- Fri 27: the 13:00 early close; DDD breaks out cleanly, SPY is up at 12:30 (hold) and the position is
  flattened at close - 10m = 12:50.
Each day also ranks rejected candidates (lower rank, catalyst missing / low quality / bearish, a bearish
candle, a doji, a name below rvol_min that is not ranked at all).

The replay runs twice in the same database and once in a fresh database; the three normalized outputs and
their progress JSON are identical, and equal the expected file. `UPDATE_GOLDEN=1` rewrites both files (the
data from `build_orb_week_data()`, the expected result from the first run); it is never set in the gate.
"""

import json
import os
from collections.abc import Iterator
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from alembic import command
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from tests.conftest import alembic_config
from tests.fakes_replay import candle, seed_replay_world
from trader.db import models as m
from trader.db.session import make_engine, make_session_factory, session_scope
from trader.market.calendar import SessionCalendar
from trader.market.clock import ET, FixedClock
from trader.market.types import Candle
from trader.replay import runner
from trader.replay.types import ReplayRequest
from trader.reports.metrics import compute_metrics
from trader.settings_store import RuntimeSettings, SettingsStore
from trader.strategies.registry import StrategyRegistry

pytestmark = pytest.mark.db

SCENARIO = "orb_week"
GOLDEN = Path(__file__).resolve().parent / "golden"
DATA_PATH = GOLDEN / f"{SCENARIO}_data.json"
EXPECTED_PATH = GOLDEN / f"{SCENARIO}_expected.json"
UPDATE_GOLDEN = os.environ.get("UPDATE_GOLDEN") == "1"

CAL = SessionCalendar()
MON, TUE, WED, THU, FRI = (date(2026, 11, d) for d in (23, 24, 25, 26, 27))
WALL = datetime(2026, 11, 28, 12, 0, tzinfo=ET)  # the Saturday after: outside the offline window
ONE_MINUTE = timedelta(minutes=1)
Q4 = Decimal("0.0001")
BPS = Decimal("0.0001")
TICKERS = ("SPY", "AAA", "BBB", "CCC", "DDD")
NAMES = ("AAA", "BBB", "CCC", "DDD")
AVG_OPEN_VOL = 10_000  # every name's 14-session opening-bar average (open_bar_stats)
ATR = "1.00"  # every name's ATR (open_bar_stats): stop = entry - 0.10


# ====================================================================================================
# The fixture data (committed as golden/orb_week_data.json; rebuilt only with UPDATE_GOLDEN=1)
# ====================================================================================================
Ohlcv = tuple[str, str, str, str, int]


def _flat(p: str, v: int = 5000, band: str = "0.05") -> Ohlcv:
    d, b = Decimal(p), Decimal(band)
    return (p, str(d + b), str(d - b), p, v)


# (day, ticker) -> the opening 5-minute bar; (day, ticker) -> (catalyst type, direction, quality).
OPENING: dict[tuple[date, str], Ohlcv] = {
    # Mon: BBB wins (rvol 5); DDD bearish (4); AAA lower rank (3); CCC no catalyst (2).
    (MON, "BBB"): ("20.00", "20.50", "19.95", "20.40", 50_000),
    (MON, "DDD"): ("30.00", "30.10", "29.40", "29.50", 40_000),
    (MON, "AAA"): ("10.00", "10.50", "9.95", "10.40", 30_000),
    (MON, "CCC"): ("15.00", "15.40", "14.95", "15.30", 20_000),
    # Tue: CCC wins (5); BBB no catalyst (3); AAA doji (2); DDD below rvol_min (0.5), not ranked.
    (TUE, "CCC"): ("15.00", "15.40", "14.95", "15.30", 50_000),
    (TUE, "BBB"): ("20.00", "20.50", "19.95", "20.40", 30_000),
    (TUE, "AAA"): ("10.20", "10.50", "9.90", "10.21", 20_000),
    (TUE, "DDD"): ("30.00", "30.50", "29.95", "30.40", 5_000),
    # Wed (biased): AAA wins (5); BBB lower rank (4); CCC no catalyst (3); DDD bearish catalyst (2).
    (WED, "AAA"): ("10.00", "10.50", "9.95", "10.40", 50_000),
    (WED, "BBB"): ("20.00", "20.50", "19.95", "20.40", 40_000),
    (WED, "CCC"): ("15.00", "15.40", "14.95", "15.30", 30_000),
    (WED, "DDD"): ("30.00", "30.50", "29.95", "30.40", 20_000),
    # Fri: DDD wins (5); AAA low-quality catalyst (3); BBB no catalyst (2); CCC lower rank (1.5).
    (FRI, "DDD"): ("30.00", "30.50", "29.95", "30.40", 50_000),
    (FRI, "AAA"): ("10.00", "10.50", "9.95", "10.40", 30_000),
    (FRI, "BBB"): ("20.00", "20.50", "19.95", "20.40", 20_000),
    (FRI, "CCC"): ("15.00", "15.40", "14.95", "15.30", 15_000),
}
CATALYSTS: dict[tuple[date, str], tuple[str, str, int]] = {
    (MON, "BBB"): ("earnings_beat", "bullish", 85),
    (MON, "AAA"): ("analyst_action", "bullish", 70),
    (TUE, "CCC"): ("contract", "bullish", 80),
    (WED, "AAA"): ("guidance", "bullish", 90),
    (WED, "BBB"): ("m_and_a", "bullish", 75),
    (WED, "DDD"): ("offering", "bearish", 80),
    (FRI, "DDD"): ("earnings_beat", "bullish", 88),
    (FRI, "AAA"): ("rumour", "bullish", 30),
    (FRI, "CCC"): ("regulatory", "bullish", 60),
}
DAILY_CLOSE = {"AAA": "10.30", "BBB": "20.30", "CCC": "15.30", "DDD": "30.30"}
SPY_DAILY = {MON: "502.00", TUE: "503.00", WED: "499.00"}  # 500.00 before; Wed is a down day
SPY_MINUTE = {MON: "505.00", TUE: "504.00", WED: "498.00", FRI: "501.00"}  # around the overlay decision


def _traded_bar(day: date, i: int) -> Ohlcv:
    """The traded name's minute `i` after the open (entry at open+5m5s: bar 5 is the first it can use)."""
    if day == MON:  # BBB: entry 20.51, stop 20.41; bar 10 has high 20.60 AND low 20.30
        if i < 5:
            return _flat("20.20")
        if i < 10:
            return _flat("20.30")
        if i == 10:
            return ("20.45", "20.60", "20.30", "20.35", 5000)
        return _flat("20.35")
    if day == TUE:  # CCC: entry 15.41, stop 15.31; bar 60 (10:30) gaps down to open 15.10
        if i < 5:
            return _flat("15.20")
        if i == 5:
            return ("15.35", "15.50", "15.33", "15.45", 5000)
        if i < 60:
            return _flat("15.60")
        if i == 60:
            return ("15.10", "15.15", "15.00", "15.05", 5000)
        return _flat("15.05")
    if day == WED:  # AAA: entry 10.51, stop 10.41; holds above the stop all day
        if i < 5:
            return _flat("10.20")
        if i == 5:
            return ("10.45", "10.60", "10.44", "10.58", 5000)
        return _flat("10.70")
    # FRI, DDD: entry 30.51, stop 30.41; holds until the 12:50 flatten
    if i < 5:
        return _flat("30.20")
    if i == 5:
        return ("30.45", "30.60", "30.44", "30.58", 5000)
    return _flat("30.80")


TRADED = {MON: "BBB", TUE: "CCC", WED: "AAA", FRI: "DDD"}


def build_orb_week_data() -> dict[str, Any]:
    """The whole fixture, as JSON-safe values (prices as strings, times as ISO UTC)."""
    days = (MON, TUE, WED, FRI)
    daily_days = [d for d in (date(2026, 10, 12) + timedelta(n) for n in range(45)) if CAL.is_session(d)]
    daily: list[list[Any]] = []
    for ticker in TICKERS:
        for d in daily_days:
            if ticker == "SPY":
                c = SPY_DAILY.get(d, "500.00")
                o, h, lo = c, str(Decimal(c) + 2), str(Decimal(c) - 2)
                vol = 50_000_000
            else:
                c = DAILY_CLOSE[ticker]
                o, h, lo = c, str(Decimal(c) + Decimal("0.5")), str(Decimal(c) - Decimal("0.5"))
                vol = 2_000_000
            daily.append([ticker, d.isoformat(), o, h, lo, c, vol])
    five: list[list[Any]] = []
    for (d, ticker), (o, h, lo, c, v) in sorted(OPENING.items()):
        five.append([ticker, CAL.session_open(d).astimezone(UTC).isoformat(), o, h, lo, c, v])
    minute: list[dict[str, Any]] = []
    for d in days:
        open_ = CAL.session_open(d)
        n = int((CAL.session_close(d) - open_) / ONE_MINUTE)
        minute.append(
            {
                "ticker": TRADED[d],
                "start": open_.astimezone(UTC).isoformat(),
                "bars": [list(_traded_bar(d, i)) for i in range(n)],
            }
        )
        decision = CAL.session_close(d) - timedelta(minutes=30)
        minute.append(
            {
                "ticker": "SPY",
                "start": (decision - 10 * ONE_MINUTE).astimezone(UTC).isoformat(),
                "bars": [list(_flat(SPY_MINUTE[d], 100_000, "0.10")) for _ in range(16)],
            }
        )
    return {
        "scenario": SCENARIO,
        "date_from": MON.isoformat(),
        "date_to": FRI.isoformat(),
        "tickers": list(TICKERS),
        # Wed 25 has no snapshot: the biased day.
        "universe": {d.isoformat(): list(NAMES) for d in (MON, TUE, FRI)},
        "open_bar_stats": [[t, d.isoformat(), AVG_OPEN_VOL, ATR] for d in days for t in NAMES],
        "candles_5m": five,
        "candles_1m": minute,
        "daily": daily,
        "catalysts": [[t, d.isoformat(), *CATALYSTS[(d, t)]] for d, t in sorted(CATALYSTS)],
    }


def load_data() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(DATA_PATH.read_text()))


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=1, sort_keys=True) + "\n")


def _day_candle(day: str, o: str, h: str, lo: str, c: str, v: int) -> Candle:
    start = datetime.combine(date.fromisoformat(day), time(0), tzinfo=ET)
    return Candle(start, start + timedelta(days=1), Decimal(o), Decimal(h), Decimal(lo), Decimal(c), v, None)


def seed_from_data(factory: sessionmaker[Session], data: dict[str, Any]) -> dict[str, int]:
    """Seed the fixture into a database (committed); returns ticker -> symbols.id."""
    archive: dict[tuple[str, str], list[Candle]] = {}
    for ticker, start, o, h, lo, c, v in data["candles_5m"]:
        archive.setdefault((ticker, "5m"), []).append(
            candle(datetime.fromisoformat(start), o, h, lo, c, v, minutes=5)
        )
    for run in data["candles_1m"]:
        t0 = datetime.fromisoformat(run["start"])
        for i, (o, h, lo, c, v) in enumerate(run["bars"]):
            archive.setdefault((run["ticker"], "1m"), []).append(candle(t0 + i * ONE_MINUTE, o, h, lo, c, v))
    daily: dict[str, list[tuple[date, Candle]]] = {}
    for ticker, day, o, h, lo, c, v in data["daily"]:
        daily.setdefault(ticker, []).append((date.fromisoformat(day), _day_candle(day, o, h, lo, c, v)))
    universe_days = sorted(date.fromisoformat(d) for d in data["universe"])
    members = sorted({t for names in data["universe"].values() for t in names})
    world = seed_replay_world(
        factory,
        clock=FixedClock(WALL - timedelta(days=8)),
        tickers=tuple(data["tickers"]),
        universe_days=universe_days,
        universe_tickers=members,
        stats={(t, date.fromisoformat(d)): (avg, atr) for t, d, avg, atr in data["open_bar_stats"]},
        archive=archive,
        daily=daily,
    )
    at = WALL - timedelta(days=8)
    with session_scope(factory) as s:
        for ticker, day, kind, direction, quality in data["catalysts"]:
            s.add(
                m.Catalyst(
                    symbol_id=world.symbols[ticker],
                    session_date=date.fromisoformat(day),
                    headlines=[],
                    catalyst_type=kind,
                    direction=direction,
                    quality=quality,
                    confirmed=True,
                    reason="golden fixture",
                    model="claude-sonnet-5",
                    cost_usd=Decimal(0),
                    classified_at=at,
                    created_at=at,
                )
            )
    return world.symbols


# ====================================================================================================
# Running and normalizing
# ====================================================================================================
def create_golden_replay(factory: sessionmaker[Session], wall: FixedClock, label: str) -> int:
    return runner.create_replay(
        factory,
        wall,
        CAL,
        SettingsStore(factory, now=wall.now),
        StrategyRegistry(factory, wall),
        ReplayRequest(MON, FRI, label=label, offline=True),
        "test:golden",
    )


async def run_golden(factory: sessionmaker[Session], wall: FixedClock, label: str) -> int:
    """Create and run the golden replay through the real offline composition; returns its run id."""
    run_id = create_golden_replay(factory, wall, label)
    core = SimpleNamespace(
        factory=factory, clock=wall, calendar=CAL, settings=SettingsStore(factory, now=wall.now), crypto=None
    )
    async with runner.open_replay_deps(cast(Any, core), data_mode="offline") as deps:
        final = await runner.run_replay(deps, run_id)
    assert final.status == "completed", final.error
    return run_id


def _j(v: Any) -> Any:
    if isinstance(v, Decimal):
        return format(v, "f")
    if isinstance(v, datetime):
        return v.astimezone(UTC).isoformat()
    if isinstance(v, date):
        return v.isoformat()
    return v


def _metrics(factory: sessionmaker[Session], run_id: int) -> dict[str, Any]:
    mt = compute_metrics(factory, run_id)
    out: dict[str, Any] = {}
    for key in mt.__dataclass_fields__:
        if key == "run_id":
            continue
        value = getattr(mt, key)
        if key == "r_histogram":
            out[key] = [[_j(b.lo), _j(b.hi), b.count] for b in value]
        else:
            out[key] = _j(value)
    return out


def normalized(factory: sessionmaker[Session], run_id: int) -> dict[str, Any]:
    """The run's results without database ids: trades, fills, per-day rankings, whole-run metrics, the
    progress JSON (all in the golden file), plus orders and ledger rows (compared between runs)."""
    with factory() as s:
        tickers = dict(s.execute(select(m.Symbol.id, m.Symbol.ticker)).all())
        trades = [
            {
                "session_date": _j(t.session_date),
                "ticker": tickers[t.symbol_id],
                "qty": t.qty,
                "entry_price": _j(t.entry_price),
                "exit_price": _j(t.exit_price),
                "pnl": _j(t.pnl),
                "pnl_r": _j(t.pnl_r),
                "exit_reason": t.exit_reason,
                "opened_at": _j(t.opened_at),
                "closed_at": _j(t.closed_at),
            }
            for t in s.execute(
                select(m.Trade).where(m.Trade.run_id == run_id).order_by(m.Trade.opened_at, m.Trade.id)
            ).scalars()
        ]
        fills = [
            {
                "ticker": tickers[o.symbol_id],
                "side": o.side,
                "order_type": o.order_type,
                "purpose": o.purpose,
                "reason": o.reason,
                "ts": _j(f.ts),
                "qty": f.qty,
                "price": _j(f.price),
                "slippage": _j(f.slippage),
            }
            for f, o in s.execute(
                select(m.Fill, m.Order)
                .join(m.Order, m.Order.id == m.Fill.order_id)
                .where(m.Fill.run_id == run_id)
                .order_by(m.Fill.ts, m.Fill.id)
            ).all()
        ]
        rankings: dict[str, list[dict[str, Any]]] = {}
        for c in s.execute(
            select(m.Candidate)
            .where(m.Candidate.run_id == run_id)
            .order_by(m.Candidate.session_date, m.Candidate.strategy_key, m.Candidate.rank, m.Candidate.id)
        ).scalars():
            rankings.setdefault(c.session_date.isoformat(), []).append(
                {
                    "ticker": tickers[c.symbol_id],
                    "rank": c.rank,
                    "rvol": _j(c.rvol),
                    "passed": c.passed,
                    "reject_reason": c.reject_reason,
                }
            )
        orders = [
            [
                tickers[o.symbol_id],
                o.side,
                o.order_type,
                o.purpose,
                o.qty,
                _j(o.stop_price),
                o.status,
                o.reason,
                o.cancel_reason,
                _j(o.submitted_at),
                _j(o.closed_at),
            ]
            for o in s.execute(
                select(m.Order).where(m.Order.run_id == run_id).order_by(m.Order.submitted_at, m.Order.id)
            ).scalars()
        ]
        ledger = [
            [_j(r.ts), _j(r.trade_date), _j(r.settle_date), r.kind, _j(r.amount)]
            for r in s.execute(
                select(m.CashLedger)
                .where(m.CashLedger.run_id == run_id)
                .order_by(m.CashLedger.ts, m.CashLedger.id)
            ).scalars()
        ]
        run = s.get(m.Run, run_id)
        assert run is not None
        progress, label = run.progress, run.label
    return {
        "trades": trades,
        "fills": fills,
        "rankings": rankings,
        "metrics": _metrics(factory, run_id),
        "progress": progress,
        "biased_label": (label or "").endswith("(biased universe)"),
        "orders": orders,
        "ledger": ledger,
    }


GOLDEN_KEYS = ("trades", "fills", "rankings", "metrics", "progress", "biased_label")


def golden_part(result: dict[str, Any]) -> dict[str, Any]:
    return {k: result[k] for k in GOLDEN_KEYS}


# ====================================================================================================
# A fresh database in the same PostgreSQL container
# ====================================================================================================
@pytest.fixture
def fresh_factory(pg_url: str) -> Iterator[sessionmaker[Session]]:
    name = "golden_fresh"
    admin = create_engine(pg_url, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS {name}"))
            conn.execute(text(f"CREATE DATABASE {name}"))
        url = make_url(pg_url).set(database=name).render_as_string(hide_password=False)
        command.upgrade(alembic_config(url), "head")
        engine = make_engine(url)
        try:
            yield make_session_factory(engine)
        finally:
            engine.dispose()
        with admin.connect() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
    finally:
        admin.dispose()


# ====================================================================================================
# Hand checks (independent of the implementation: SPEC §7.4 and the decision "Candle fill model")
# ====================================================================================================
def _q(x: Decimal) -> Decimal:
    return x.quantize(Q4, ROUND_HALF_UP)


def slip(p: Decimal) -> Decimal:
    s = RuntimeSettings()
    return _q(max(s.slippage_min, s.slippage_bps * BPS * p))


def hs(p: Decimal) -> Decimal:
    return _q(RuntimeSettings().replay_half_spread_bps * BPS * p)


def sell_at(ref: str) -> str:
    p = Decimal(ref)
    return format(p - slip(p) - hs(p), "f")


def buy_at(ref: str) -> str:
    p = Decimal(ref)
    return format(p + slip(p) + hs(p), "f")


def et_iso(d: date, hh: int, mm: int) -> str:
    return datetime.combine(d, time(hh, mm), tzinfo=ET).astimezone(UTC).isoformat()


def assert_hand_checks(result: dict[str, Any]) -> None:
    trades = {t["session_date"]: t for t in result["trades"]}
    assert sorted(trades) == [d.isoformat() for d in (MON, TUE, WED, FRI)]  # Thanksgiving skipped
    assert all(t["session_date"] != THU.isoformat() for t in result["trades"])

    mon = trades[MON.isoformat()]  # the same-bar entry and stop: entered, then stopped in bar 09:40
    assert (mon["ticker"], mon["entry_price"], mon["exit_price"]) == (
        "BBB",
        buy_at("20.51"),
        sell_at("20.41"),
    )
    assert mon["opened_at"] == mon["closed_at"] == et_iso(MON, 9, 41)
    assert Decimal(mon["pnl"]) < 0

    tue = trades[TUE.isoformat()]  # the gap through the stop: filled at the 10:30 open, less costs
    assert (tue["ticker"], tue["entry_price"], tue["exit_price"]) == (
        "CCC",
        buy_at("15.41"),
        sell_at("15.10"),
    )
    assert tue["closed_at"] == et_iso(TUE, 10, 31)
    assert Decimal(tue["exit_price"]) < Decimal("15.31") - slip(Decimal("15.31")) - hs(Decimal("15.31"))

    wed = trades[WED.isoformat()]  # the overlay exit on a down day for SPY, at 15:30
    assert (wed["ticker"], wed["exit_reason"]) == ("AAA", "overlay_negative")
    assert (wed["entry_price"], wed["exit_price"]) == (buy_at("10.51"), sell_at("10.70"))
    assert wed["closed_at"] == et_iso(WED, 15, 31)

    fri = trades[FRI.isoformat()]  # the clean breakout flattened at close - 10m on the 13:00 early close
    assert (fri["ticker"], fri["exit_reason"]) == ("DDD", "flatten_close")
    assert (fri["entry_price"], fri["exit_price"]) == (buy_at("30.51"), sell_at("30.80"))
    assert fri["closed_at"] == et_iso(FRI, 12, 51)
    assert Decimal(fri["pnl"]) > 0

    ranks = {
        d: [(c["ticker"], c["rank"], c["reject_reason"]) for c in cs] for d, cs in result["rankings"].items()
    }
    assert ranks[MON.isoformat()] == [
        ("BBB", 1, None),
        ("DDD", 2, "bearish_candle"),
        ("AAA", 3, "lower_rank"),
        ("CCC", 4, "catalyst_missing"),
    ]
    assert ranks[TUE.isoformat()] == [("CCC", 1, None), ("BBB", 2, "catalyst_missing"), ("AAA", 3, "doji")]
    assert ranks[WED.isoformat()] == [
        ("AAA", 1, None),
        ("BBB", 2, "lower_rank"),
        ("CCC", 3, "catalyst_missing"),
        ("DDD", 4, "catalyst_bearish"),
    ]
    assert ranks[FRI.isoformat()] == [
        ("DDD", 1, None),
        ("AAA", 2, "catalyst_low_quality"),
        ("BBB", 3, "catalyst_missing"),
        ("CCC", 4, "lower_rank"),
    ]
    assert result["progress"]["biased_days"] == [WED.isoformat()]
    assert result["progress"]["sessions_total"] == result["progress"]["sessions_done"] == 4
    assert result["progress"]["forced_closes"] == 0
    assert result["biased_label"] is True
    metrics = result["metrics"]
    assert metrics["trades"] == 4 and metrics["wins"] + metrics["losses"] == 4
    total = sum((Decimal(t["pnl"]) for t in result["trades"]), Decimal(0))
    assert Decimal(metrics["total_pnl"]) == total


# ====================================================================================================
# The tests
# ====================================================================================================
def test_the_committed_data_is_the_generator_output() -> None:
    """The fixture file is exactly what `build_orb_week_data()` describes (rewritten with UPDATE_GOLDEN=1),
    so the scenario comments above stay true."""
    if UPDATE_GOLDEN:
        _write_json(DATA_PATH, build_orb_week_data())
    assert load_data() == json.loads(json.dumps(build_orb_week_data()))


async def test_1_2_golden_orb_week_is_reproduced_three_times(
    db_factory: sessionmaker[Session], fresh_factory: sessionmaker[Session]
) -> None:
    """Test 1 (golden) and test 2 (determinism): the same replay twice in one database (at other wall times)
    and once in a fresh database gives identical normalized outputs and progress, equal to the expected
    file, including the hand-checked prices of the same-bar stop-out, the gap fill, the overlay exit and the
    early-close flatten."""
    data = load_data()
    seed_from_data(db_factory, data)
    first_id = await run_golden(db_factory, FixedClock(WALL), "golden 1")
    first = normalized(db_factory, first_id)
    second_id = await run_golden(db_factory, FixedClock(WALL + timedelta(hours=7, minutes=13)), "golden 2")
    second = normalized(db_factory, second_id)

    seed_from_data(fresh_factory, data)
    with session_scope(fresh_factory) as s:  # different id allocation in the fresh database
        s.execute(text("SELECT setval(pg_get_serial_sequence('trader.runs', 'id'), 500)"))
    third_id = await run_golden(fresh_factory, FixedClock(WALL + timedelta(days=1)), "golden 3")
    third = normalized(fresh_factory, third_id)

    assert first_id != second_id and third_id > 500
    assert second == first
    assert third == first
    assert first["progress"] == second["progress"] == third["progress"]

    if UPDATE_GOLDEN:
        _write_json(EXPECTED_PATH, golden_part(first))
    expected = json.loads(EXPECTED_PATH.read_text())
    assert golden_part(first) == expected
    assert_hand_checks(first)

    # Nothing about the live run changed: it has no trades, fills or candidates.
    with db_factory() as s:
        live_trades = s.execute(
            select(func.count())
            .select_from(m.Trade)
            .join(m.Run, m.Run.id == m.Trade.run_id)
            .where(m.Run.mode != "replay")
        ).scalar_one()
    assert live_trades == 0
