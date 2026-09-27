"""P5-RC gauntlet (Breaker, attempt 1): the Phase 5 replay core (P5-T3 clock and candle fill model,
P5-T4 engine hooks, P5-T5 replay data, P5-T6 runner) driven together through the REAL offline composition
(`open_replay_deps(core, data_mode="offline")`: `ReplayData` over the testcontainers DB, `ReplayCatalysts`,
`build_replay_engine` with the real orb_sip and spy_overlay plug-ins, `SimBroker` + `CandleFillModel`).

Targets (Review Focus 1-3 of the Phase 5 plan):
- determinism: the same replay twice, in a fresh process with another PYTHONHASHSEED, with shifted database id
  allocation and the candle archive re-inserted in reverse order, compared row by row (normalized);
- look-ahead: every bar, quote, opening bar and prior close a strategy is handed is complete at the replay
  clock; the 09:35:05 event can't see the 09:35 minute bar; synthetic quotes come from the last complete bar;
- fills: the same-bar worst case, a gap through a stop, limits, bars before the order, hours and the entry
  cutoff on the bar's start, zero-volume bars, the forced close without a bar;
- isolation: only the documented rows change, the live registry and live proposals are untouched;
- runner: cancel between sessions, a crashed process, the lock, the offline window, bounded memory.

No real services: the DB is the testcontainers PostgreSQL, Questrade is `FakeQuestrade`, no Claude, no
Telegram.
"""

import asyncio
import dataclasses
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from tests.factories import add_strategy_config, add_symbol
from tests.fakes_questrade import FakeQuestrade
from tests.fakes_replay import ReplayWorld, candle, seed_replay_world
from trader.adapters.questrade.models import QtQuote
from trader.broker.fill_model import FillParams
from trader.broker.ledger import Ledger
from trader.broker.sim_broker import ENTRY_CUTOFF, SimBroker
from trader.broker.types import FillDecision, NoFill, OrderSpec
from trader.db import models as m
from trader.db.session import make_engine, make_session_factory, session_scope
from trader.engine.runs import get_live_run
from trader.market.calendar import SessionCalendar
from trader.market.clock import ET, Clock, FixedClock
from trader.market.types import Candle, Interval, OpeningBars
from trader.replay import runner
from trader.replay.candle_fill_model import CandleFillModel
from trader.replay.clock import ReplayClock
from trader.replay.data import ReplayData
from trader.replay.types import (
    ReplayBusy,
    ReplayEngine,
    ReplayMarket,
    ReplayRequest,
    ReplayRun,
    StrategyOverride,
    load_replay_run,
)
from trader.settings_store import RuntimeSettings, SettingsStore
from trader.strategies.registry import StrategyRegistry

pytestmark = pytest.mark.db

APP_DIR = Path(__file__).resolve().parents[2]
CAL = SessionCalendar()
FRI_20 = date(2026, 11, 20)
MON = date(2026, 11, 23)
TUE = date(2026, 11, 24)
SAT = datetime(2026, 11, 28, 12, 0, tzinfo=ET)  # a Saturday: not in the offline window
ONE_MINUTE = timedelta(minutes=1)
TICKERS = ("SPY", "AAA", "BBB")
ID_KEY = re.compile(r"(^id$|_id$|_ids$)")
# ids inside text: "order 17", "sim_account:2", "fill:40", "signal 9", "(config 3)", "position 5", "replay 7"
ID_TEXT = re.compile(
    r"\b(sim_account|fill|orders?|positions?|proposal|signal|trade|run|replay|config)([: ])(\d+)\b"
)


def et(d: date, hh: int, mm: int, ss: int = 0) -> datetime:
    return datetime.combine(d, time(hh, mm, ss), tzinfo=ET).astimezone(UTC)


# --- the market: bars that exercise the fill rules ----------------------------------------------------------
# AAA opening bar 10.00/10.50/9.95/10.40 (rvol 5, ATR 1.00) -> buy stop 10.51, stop loss 10.41.
# BBB opening bar 20.00/20.50/19.95/20.40 (rvol 3, ATR 1.00) -> buy stop 20.51, stop loss 20.41.
AAA_ENTRY, AAA_STOP = Decimal("10.51"), Decimal("10.41")
BBB_ENTRY, BBB_STOP = Decimal("20.51"), Decimal("20.41")
Bar = tuple[str, str, str, str, int]


def _flat(p: str, v: int = 5000) -> Bar:
    d = Decimal(p)
    return (p, str(d + Decimal("0.05")), str(d - Decimal("0.05")), p, v)


def aaa_bar(day: date, i: int, *, tail: str) -> Bar | None:
    """AAA's minute `i` after the open. Both days: no trigger at 09:35, the entry at 09:36 (low above the
    stop). Monday: a gap down through the stop at 10:30 (open 10.30 < 10.41). Tuesday: flat into the close;
    `tail` shapes 15:40-16:00 (`normal`, `none` = no bars, `zero` = zero-volume bars)."""
    if i < 5:
        return _flat("10.20")
    if i == 5:
        return ("10.45", "10.49", "10.40", "10.48", 5000)
    if i == 6:
        return ("10.48", "10.60", "10.47", "10.58", 5000)
    if day == MON:
        if i < 60:
            return _flat("10.60")
        if i == 60:
            return ("10.30", "10.35", "10.20", "10.25", 5000)
        return _flat("10.25")
    if i >= 370 and tail == "none":
        return None
    if i >= 370 and tail == "zero":
        return ("10.60", "10.65", "10.55", "10.60", 0)
    return _flat("10.60")


def bbb_bar(day: date, i: int) -> Bar:
    """BBB: Monday's 09:40 bar triggers the entry (high 20.60) AND the stop (low 20.30) in one bar."""
    if day == MON and i == 10:
        return ("20.45", "20.60", "20.30", "20.35", 5000)
    if day == MON and i > 10:
        return _flat("20.35")
    return _flat("20.20")


def minute_bars(day: date, shape: Callable[[int], Bar | None]) -> list[Candle]:
    open_ = CAL.session_open(day)
    n = int((CAL.session_close(day) - open_) / ONE_MINUTE)
    out: list[Candle] = []
    for i in range(n):
        b = shape(i)
        if b is not None:
            out.append(candle(open_ + i * ONE_MINUTE, *b))
    return out


def day_bar(d: date, close: str) -> Candle:
    start = datetime.combine(d, time(0), tzinfo=ET)
    c = Decimal(close)
    return Candle(start, start + timedelta(days=1), c, c + 1, c - 1, c, 1_000_000, None)


def _aaa_shape(day: date, tail: str) -> Callable[[int], Bar | None]:
    return lambda i: aaa_bar(day, i, tail=tail)


def _bbb_shape(day: date) -> Callable[[int], Bar | None]:
    return lambda i: bbb_bar(day, i)


def archive(tail: str = "normal") -> dict[tuple[str, str], list[Candle]]:
    out: dict[tuple[str, str], list[Candle]] = {}
    for day in (MON, TUE):
        o = CAL.session_open(day)
        out.setdefault(("AAA", "5m"), []).append(
            candle(o, "10.00", "10.50", "9.95", "10.40", 50_000, minutes=5)
        )
        out.setdefault(("BBB", "5m"), []).append(
            candle(o, "20.00", "20.50", "19.95", "20.40", 30_000, minutes=5)
        )
        # a second 5-minute bar (09:35-09:40) that must stay invisible to the 09:35:05 event
        out[("AAA", "5m")].append(
            candle(o + 5 * ONE_MINUTE, "10.45", "10.70", "10.40", "10.65", 9_000, minutes=5)
        )
        out.setdefault(("AAA", "1m"), []).extend(minute_bars(day, _aaa_shape(day, tail)))
        out.setdefault(("BBB", "1m"), []).extend(minute_bars(day, _bbb_shape(day)))
        out.setdefault(("SPY", "1m"), []).extend(minute_bars(day, lambda i: _flat("500.00", 100_000)))
    return out


def seed_stack(factory: sessionmaker[Session], *, tail: str = "normal") -> ReplayWorld:
    stats = {(t, d): (10_000, "1.00") for t in ("AAA", "BBB") for d in (MON, TUE)}
    return seed_replay_world(
        factory,
        tickers=TICKERS,
        universe_days=(MON, TUE),
        universe_tickers=("AAA", "BBB"),
        stats=stats,
        archive=archive(tail),
        daily={"SPY": [(FRI_20, day_bar(FRI_20, "490.00")), (MON, day_bar(MON, "495.00"))]},
    )


def replay_request(date_to: date = TUE, **kw: Any) -> ReplayRequest:
    return ReplayRequest(
        MON,
        date_to,
        overrides={"starting_cash": "100000", "risk_pct": "0.001"},
        strategies={"orb_sip": StrategyOverride(params={"require_catalyst": False, "max_positions": 2})},
        offline=True,
        **kw,
    )


def create(factory: sessionmaker[Session], wall: Clock, request: ReplayRequest | None = None) -> int:
    return runner.create_replay(
        factory,
        wall,
        CAL,
        SettingsStore(factory, now=wall.now),
        StrategyRegistry(factory, wall),
        request or replay_request(),
        "test:breaker",
    )


MarketWrap = Callable[[ReplayMarket, ReplayClock], ReplayMarket]
EngineWrap = Callable[[ReplayEngine, ReplayRun], ReplayEngine]


async def run_real(
    factory: sessionmaker[Session],
    wall: Clock,
    run_id: int,
    *,
    wrap_market: MarketWrap | None = None,
    wrap_engine: EngineWrap | None = None,
) -> ReplayRun:
    """Run a created replay through the real offline composition (no Questrade client at all)."""
    core = SimpleNamespace(
        factory=factory, clock=wall, calendar=CAL, settings=SettingsStore(factory, now=wall.now), crypto=None
    )
    async with runner.open_replay_deps(cast(Any, core), data_mode="offline") as deps:
        if wrap_market is not None:
            base_market = deps.market_factory

            def market_factory(run: ReplayRun, clock: ReplayClock) -> ReplayMarket:
                return wrap_market(base_market(run, clock), clock)

            deps = dataclasses.replace(deps, market_factory=market_factory)
        if wrap_engine is not None:
            base_engine = deps.engine_factory

            def engine_factory(
                run: ReplayRun, clock: ReplayClock, market: Any, catalysts: Any
            ) -> ReplayEngine:
                return wrap_engine(base_engine(run, clock, market, catalysts), run)

            deps = dataclasses.replace(deps, engine_factory=engine_factory)
        return await runner.run_replay(deps, run_id)


# --- normalization (database ids differ between runs; everything else must not) -----------------------------
def _norm(v: Any) -> Any:
    if isinstance(v, dict):
        return {
            str(k): ("<id>" if ID_KEY.search(str(k)) and k != "symbol_id" else _norm(x)) for k, x in v.items()
        }
    if isinstance(v, list | tuple):
        return [_norm(x) for x in v]
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, datetime):
        return v.astimezone(UTC).isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, str):
        return ID_TEXT.sub(lambda mt: f"{mt.group(1)}{mt.group(2)}#", v)
    return v


def run_tables() -> list[Any]:
    return [t for t in m.Base.metadata.sorted_tables if "run_id" in t.c and t.name != "runs"]


def snapshot(factory: sessionmaker[Session], run_id: int) -> dict[str, Any]:
    """Every row the run owns (ordered by id, ids masked), its progress and its v_trade_metrics row."""
    out: dict[str, Any] = {}
    with factory() as s:
        for t in run_tables():
            order = list(t.primary_key.columns) or list(t.c)
            rows = s.execute(select(t).where(t.c.run_id == run_id).order_by(*order)).mappings().all()
            out[t.name] = [_norm({k: v for k, v in r.items() if k != "run_id"}) for r in rows]
        run = s.get(m.Run, run_id)
        assert run is not None
        out["progress"] = _norm(run.progress)
        out["status"] = run.status
        out["label_suffix"] = (run.label or "").endswith("(biased universe)")
        metrics = s.execute(text("SELECT * FROM trader.v_trade_metrics WHERE run_id = :r"), {"r": run_id})
        out["v_trade_metrics"] = [
            _norm({k: v for k, v in r.items() if k != "run_id"}) for r in metrics.mappings()
        ]
    return cast(dict[str, Any], json.loads(json.dumps(out, sort_keys=True)))


def _trades(factory: sessionmaker[Session], run_id: int) -> list[m.Trade]:
    with factory() as s:
        return list(s.execute(select(m.Trade).where(m.Trade.run_id == run_id).order_by(m.Trade.id)).scalars())


def _fills(factory: sessionmaker[Session], run_id: int) -> list[Any]:
    """(symbol_id, side, purpose, reason, ts, price) of each fill of the run, in fill order."""
    with factory() as s:
        return list(
            s.execute(
                select(
                    m.Order.symbol_id, m.Order.side, m.Order.purpose, m.Order.reason, m.Fill.ts, m.Fill.price
                )
                .join(m.Order, m.Order.id == m.Fill.order_id)
                .where(m.Fill.run_id == run_id)
                .order_by(m.Fill.id)
            ).all()
        )


def _open_positions(factory: sessionmaker[Session], run_id: int) -> list[m.Position]:
    with factory() as s:
        return list(
            s.execute(
                select(m.Position).where(m.Position.run_id == run_id, m.Position.closed_at.is_(None))
            ).scalars()
        )


def _shift_id_allocation(factory: sessionmaker[Session], bump: int) -> None:
    """Move every identity sequence forward by a different amount, so the next run gets unrelated ids."""
    with session_scope(factory) as s:
        for i, t in enumerate(m.Base.metadata.sorted_tables):
            if "id" not in t.c:
                continue
            seq = s.execute(text("SELECT pg_get_serial_sequence(:t, 'id')"), {"t": t.fullname}).scalar_one()
            if seq is None:
                continue
            top = s.execute(select(func.coalesce(func.max(t.c.id), 0))).scalar_one()
            s.execute(text("SELECT setval(:q, :v)"), {"q": seq, "v": int(top) + bump + 37 * i})


def _reinsert_archive_reversed(factory: sessionmaker[Session]) -> None:
    """Same candle rows, stored in the opposite physical order (unordered reads would now differ)."""
    table = cast(Any, m.CandleArchive.__table__)
    with session_scope(factory) as s:
        rows = [dict(r) for r in s.execute(select(table).order_by(*table.primary_key.columns)).mappings()]
        s.execute(table.delete())
        s.execute(table.insert(), list(reversed(rows)))


def child_main(url: str, wall_iso: str, out_path: str) -> None:
    """Entry point of the fresh-process run (test 2): create and run the same request, dump the snapshot."""
    engine = make_engine(url)
    try:
        factory = make_session_factory(engine)
        wall = FixedClock(datetime.fromisoformat(wall_iso))
        run_id = create(factory, wall)
        final = asyncio.run(run_real(factory, wall, run_id))
        assert final.status == "completed", final.error
        Path(out_path).write_text(json.dumps(snapshot(factory, run_id), sort_keys=True))
    finally:
        engine.dispose()


def crash_main(url: str, wall_iso: str, run_id: int) -> None:
    """Entry point of the crashing process (test 15): run a replay and die hard during its second session."""
    engine = make_engine(url)
    factory = make_session_factory(engine)
    wall = FixedClock(datetime.fromisoformat(wall_iso))

    class Dying:
        def __init__(self, inner: ReplayEngine) -> None:
            self._inner = inner

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

        @property
        def broker(self) -> Any:
            return self._inner.broker

        async def run_event(self, event_key: str, session_date: date) -> Any:
            if session_date == TUE:
                os._exit(9)  # no finally, no unlock: the connection just drops
            return await self._inner.run_event(event_key, session_date)

    asyncio.run(run_real(factory, wall, run_id, wrap_engine=lambda e, _r: cast(ReplayEngine, Dying(e))))


def _child_env(seed: str) -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONHASHSEED"] = seed
    env["PYTHONPATH"] = str(APP_DIR) + os.pathsep + env.get("PYTHONPATH", "")
    return env


def _url(factory: sessionmaker[Session]) -> str:
    bind = factory.kw["bind"]
    assert isinstance(bind, Engine)
    return bind.url.render_as_string(hide_password=False)


# ====================================================================================================
# DETERMINISM
# ====================================================================================================
async def test_1_same_replay_twice_identical_rows_with_shifted_ids_and_reversed_archive(
    db_factory: sessionmaker[Session],
) -> None:
    """Two runs of the same request (different wall times, every id sequence moved, the archive re-inserted
    in reverse order) give identical orders, fills, trades, positions, signals, candidates, proposals, ledger,
    snapshots, events, progress and metrics."""
    seed_stack(db_factory)
    wall1 = FixedClock(SAT)
    first = create(db_factory, wall1)
    assert (await run_real(db_factory, wall1, first)).status == "completed"
    a = snapshot(db_factory, first)

    _shift_id_allocation(db_factory, 5_000)
    _reinsert_archive_reversed(db_factory)
    wall2 = FixedClock(SAT + timedelta(hours=5, minutes=17))
    second = create(db_factory, wall2)
    assert (await run_real(db_factory, wall2, second)).status == "completed"
    b = snapshot(db_factory, second)

    assert a["trades"], "the scenario must trade (otherwise the comparison proves nothing)"
    for key in sorted(a):
        assert a[key] == b[key], f"replay rows differ between two identical runs: {key}"


def test_2_fresh_process_with_another_hash_seed_matches(db_factory: sessionmaker[Session]) -> None:
    """The same request in a fresh interpreter (PYTHONHASHSEED 1, then 4242) matches the in-process run."""
    seed_stack(db_factory)
    wall = FixedClock(SAT)
    run_id = create(db_factory, wall)
    assert asyncio.run(run_real(db_factory, wall, run_id)).status == "completed"
    here = snapshot(db_factory, run_id)
    for seed, wall_iso in (("1", "2026-11-28T19:00:00-05:00"), ("4242", "2026-11-29T08:30:00-05:00")):
        out = APP_DIR / f".p5rc_child_{seed}.json"
        try:
            proc = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import sys\n"
                    "from tests.gauntlet.test_p5_rc_breaker import child_main\n"
                    "child_main(sys.argv[1], sys.argv[2], sys.argv[3])\n",
                    _url(db_factory),
                    wall_iso,
                    str(out),
                ],
                cwd=APP_DIR,
                env=_child_env(seed),
                capture_output=True,
                text=True,
                timeout=300,
            )
            assert proc.returncode == 0, proc.stderr[-3000:]
            there = json.loads(out.read_text())
        finally:
            out.unlink(missing_ok=True)
        for key in sorted(here):
            assert here[key] == there[key], f"PYTHONHASHSEED={seed}: {key} differs from the in-process run"


def test_3_replay_code_has_no_nondeterministic_sources() -> None:
    """Static scan of trader/replay/: no wall clock reads, randomness, uuids, floats on money, or completion-
    order assembly (asyncio.as_completed / gather feeding results)."""
    banned = {
        r"\btime\.time\(": "time.time()",
        r"datetime\.now\(|datetime\.utcnow\(|date\.today\(": "wall-clock read",
        r"^\s*(import random|from random )": "random",
        r"\buuid\b": "uuid",
        r"^\s*(import secrets|from secrets )": "secrets",
        r"as_completed\(": "completion-order assembly",
        r"\bfloat\(": "float conversion",
        r"time\.monotonic\(|perf_counter\(": "monotonic timing",
    }
    root = APP_DIR / "trader" / "replay"
    hits: list[str] = []
    for path in sorted(root.glob("*.py")):
        for n, line in enumerate(path.read_text().splitlines(), start=1):
            code = line.split("#", 1)[0]
            if code.lstrip().startswith(('"', "'")):
                continue
            for pattern, what in banned.items():
                if re.search(pattern, code):
                    hits.append(f"{path.name}:{n}: {what}: {line.strip()}")
    assert hits == []


# ====================================================================================================
# LOOK-AHEAD
# ====================================================================================================
class SpyMarket:
    """Wraps the real `ReplayData`: every strategy-facing answer is checked against the replay clock, and at
    every call a sweep asks for all bars of every symbol (1m, 5m, daily) to catch anything from the future."""

    def __init__(self, inner: ReplayMarket, clock: ReplayClock, ids: Sequence[int]) -> None:
        self._inner = inner
        self._clock = clock
        self._ids = list(ids)
        self.violations: list[str] = []
        self.quotes_seen: list[tuple[datetime, int, QtQuote]] = []
        self.opening_seen: list[tuple[datetime, OpeningBars]] = []
        self.prior_seen: list[tuple[datetime, date, int, Decimal | None]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    @property
    def biased_days(self) -> frozenset[date]:
        return self._inner.biased_days

    def _bad(self, what: str) -> None:
        self.violations.append(f"{self._clock.now().isoformat()}: {what}")

    async def _sweep(self) -> None:
        now = self._clock.now()
        day = now.astimezone(ET).date()
        lo, hi = datetime.combine(day - timedelta(days=5), time(0), tzinfo=ET), now + timedelta(days=3)
        for sid in self._ids:
            for interval in cast(tuple[Interval, ...], ("OneMinute", "FiveMinutes", "OneDay")):
                for bar in await self._inner.candles(sid, lo, hi, interval):
                    if bar.end > now:
                        self._bad(f"candles({sid}, {interval}) returned a bar ending {bar.end.isoformat()}")

    async def quotes(self, symbol_ids: Sequence[int]) -> dict[int, QtQuote]:
        out = await self._inner.quotes(symbol_ids)
        now = self._clock.now()
        for sid, q in out.items():
            self.quotes_seen.append((now, sid, q))
            if q.last_trade_time is None or q.last_trade_time > now:
                self._bad(f"quote {sid} from {q.last_trade_time}")
            if q.last != self._inner.last_close(sid, now):
                self._bad(f"quote {sid} last {q.last} is not the last complete bar's close")
        await self._sweep()
        return out

    async def opening_bars(self, session_date: date, symbol_ids: Sequence[int] | None = None) -> OpeningBars:
        out = await self._inner.opening_bars(session_date, symbol_ids)
        self.opening_seen.append((self._clock.now(), out))
        for sid, bar in out.bars.items():
            if bar.end > self._clock.now():
                self._bad(f"opening bar {sid} ends {bar.end.isoformat()}")
        await self._sweep()
        return out

    async def candles(
        self, symbol_id: int, start: datetime, end: datetime, interval: Interval
    ) -> list[Candle]:
        out = await self._inner.candles(symbol_id, start, end, interval)
        for bar in out:
            if bar.end > self._clock.now():
                self._bad(f"candles {symbol_id} {interval} bar ending {bar.end.isoformat()}")
        return out

    async def prior_close(self, symbol_id: int, session_date: date) -> Decimal | None:
        out = await self._inner.prior_close(symbol_id, session_date)
        self.prior_seen.append((self._clock.now(), session_date, symbol_id, out))
        prev = CAL.previous_session(session_date)
        if out is not None and CAL.session_close(prev) > self._clock.now():
            self._bad(f"prior_close({symbol_id}, {session_date}) before {prev}'s close")
        return out

    async def prior_closes(self, symbol_ids: Sequence[int], session_date: date) -> dict[int, Decimal]:
        out: dict[int, Decimal] = await self._inner.prior_closes(symbol_ids, session_date)  # type: ignore[attr-defined]
        prev = CAL.previous_session(session_date)
        if out and CAL.session_close(prev) > self._clock.now():
            self._bad(f"prior_closes({session_date}) before {prev}'s close")
        return out


async def test_4_strategies_never_see_a_bar_or_quote_from_the_future(
    db_factory: sessionmaker[Session],
) -> None:
    world = seed_stack(db_factory)
    ids = [world.symbols[t] for t in TICKERS]
    wall = FixedClock(SAT)
    run_id = create(db_factory, wall)
    spies: list[SpyMarket] = []

    def wrap(market: ReplayMarket, clock: ReplayClock) -> ReplayMarket:
        spy = SpyMarket(market, clock, ids)
        spies.append(spy)
        return cast(ReplayMarket, spy)

    final = await run_real(db_factory, wall, run_id, wrap_market=wrap)
    assert final.status == "completed", final.error
    (spy,) = spies
    assert spy.violations == []
    # the 09:35:05 scan saw the 09:30 opening bars, and nothing of 09:35 onwards
    (at, seen) = next((t, o) for t, o in spy.opening_seen if t == et(MON, 9, 35, 5))
    assert {sid: b.start for sid, b in seen.bars.items()} == {
        world.symbols["AAA"]: et(MON, 9, 30),
        world.symbols["BBB"]: et(MON, 9, 30),
    }
    # every synthetic quote is the last COMPLETE minute bar: its end is the last whole minute at or before now
    assert spy.quotes_seen, "the account marks and the overlay must have asked for quotes"
    for now, _sid, q in spy.quotes_seen:
        assert q.last_trade_time == now.replace(second=0, microsecond=0)
        assert q.delay == 0 and q.bid is not None and q.ask is not None and q.last is not None
        assert q.bid < q.last < q.ask
    # the overlay's prior close is the previous session's daily close, never the current day's
    assert spy.prior_seen
    for _now, day, _sid, close in spy.prior_seen:
        assert close == {MON: Decimal("490.00"), TUE: Decimal("495.00")}[day]


async def test_5_direct_probes_prior_close_opening_minute_and_five_minute_bars(
    db_factory: sessionmaker[Session],
) -> None:
    """Adversarial clock positions against the real ReplayData."""
    world = seed_stack(db_factory)
    spy_id, aaa = world.symbols["SPY"], world.symbols["AAA"]
    clock = ReplayClock(et(MON, 9, 0))
    data = ReplayData(
        db_factory,
        clock,
        FixedClock(SAT),
        CAL,
        None,
        run_id=world.live_run_id,
        date_from=MON,
        date_to=TUE,
        half_spread_bps=Decimal("5"),
        questrade_window_days=85,
        lookback_sessions=14,
    )
    await data.prepare_day(MON)
    # 09:34:59: the opening bar is not complete; no 5-minute bar at all
    clock.set(et(MON, 9, 34, 59))
    assert aaa not in (await data.opening_bars(MON, [aaa])).bars
    assert await data.candles(aaa, et(MON, 9, 30), et(MON, 10, 0), "FiveMinutes") == []
    # 09:35:05: the opening bar only; not the 09:35-09:40 bar, not the 09:35-09:36 minute bar
    clock.set(et(MON, 9, 35, 5))
    assert [b.start for b in await data.candles(aaa, et(MON, 9, 30), et(MON, 10, 0), "FiveMinutes")] == [
        et(MON, 9, 30)
    ]
    minutes = await data.candles(aaa, et(MON, 9, 30), et(MON, 16, 0), "OneMinute")
    assert minutes and max(b.end for b in minutes) == et(MON, 9, 35)
    q = (await data.quotes([aaa]))[aaa]
    assert (q.last_trade_time, q.last) == (et(MON, 9, 35), Decimal("10.20"))
    # 09:59:59 vs 10:00:00: the 09:59 bar is complete only at 10:00
    clock.set(et(MON, 9, 59, 59))
    assert (await data.quotes([aaa]))[aaa].last_trade_time == et(MON, 9, 59)
    clock.set(et(MON, 10, 0))
    assert (await data.quotes([aaa]))[aaa].last_trade_time == et(MON, 10, 0)
    # the previous session's close is unknown before that session closed ...
    clock.set(et(MON, 15, 59, 59))
    assert await data.prior_close(spy_id, TUE) is None
    assert await data.prior_closes([spy_id], TUE) == {}
    # ... and today's daily bar is never visible during today's session
    daily = await data.candles(spy_id, et(MON, 0, 0) - timedelta(days=10), et(MON, 23, 0), "OneDay")
    assert MON not in [b.start.astimezone(ET).date() for b in daily]
    clock.set(et(MON, 16, 0))
    assert await data.prior_close(spy_id, TUE) == Decimal("495.00")


# ====================================================================================================
# FILLS (Review Focus 3)
# ====================================================================================================
def _model(settings: RuntimeSettings | None = None) -> CandleFillModel:
    return CandleFillModel(FillParams.from_settings(settings or RuntimeSettings()), Decimal("5"))


async def test_6_same_bar_entry_and_stop_and_gap_through_stop_in_a_real_replay(
    db_factory: sessionmaker[Session],
) -> None:
    """Monday, through the real engine: BBB's 09:40 bar touches the entry (20.51) and the stop (20.41), so it
    is entered and stopped out in that bar at stop - slip - hs; AAA's 10:30 bar opens at 10.30, below its
    10.41 stop, so it fills at the open - slip - hs (worse than the stop)."""
    world = seed_stack(db_factory)
    wall = FixedClock(SAT)
    run_id = create(db_factory, wall, replay_request(date_to=MON))
    final = await run_real(db_factory, wall, run_id)
    assert final.status == "completed", final.error
    model = _model(final.settings)
    trades = {t.symbol_id: t for t in _trades(db_factory, run_id)}
    fills = _fills(db_factory, run_id)
    bbb, aaa = world.symbols["BBB"], world.symbols["AAA"]
    assert set(trades) == {aaa, bbb}

    # BBB: entered and stopped in the same bar, never the favourable order
    b_fills = [f for f in fills if f.symbol_id == bbb]
    assert [(f.side, f.ts) for f in b_fills] == [("buy", et(MON, 9, 41)), ("sell", et(MON, 9, 41))]
    ref_in = max(BBB_ENTRY, Decimal("20.45"))
    assert b_fills[0].price == ref_in + model.slip(ref_in) + model.half_spread(ref_in)
    assert b_fills[1].price == BBB_STOP - model.slip(BBB_STOP) - model.half_spread(BBB_STOP)
    assert trades[bbb].exit_reason == "protective_stop" and trades[bbb].pnl < 0

    # AAA: entry at 09:36, gap through the stop at 10:30 -> the open, not the stop
    a_fills = [f for f in fills if f.symbol_id == aaa]
    assert [(f.side, f.ts) for f in a_fills] == [("buy", et(MON, 9, 37)), ("sell", et(MON, 10, 31))]
    gap_open = Decimal("10.30")
    assert a_fills[1].price == gap_open - model.slip(gap_open) - model.half_spread(gap_open)
    assert a_fills[1].price < AAA_STOP
    assert trades[aaa].exit_reason == "protective_stop"


def test_7_fills_never_flatter_the_strategy_over_a_grid_of_bars() -> None:
    """Invariants over many bars: a buy never fills below max(trigger, open) (stops) or the open (market); a
    sell never above min(trigger, open) / the open; limits fill at exactly the limit, never better; a
    zero-volume bar never fills anything; a bar that doesn't reach the trigger never fills."""
    model = _model()
    now = et(MON, 10, 0)
    prices = [Decimal(p) for p in ("9.50", "9.90", "10.00", "10.10", "10.50")]
    orders = [
        OrderSpec(1, "buy", "stop", 100, stop=Decimal("10.00"), stop_loss=Decimal("9.80")),
        OrderSpec(
            1,
            "buy",
            "stop_limit",
            100,
            stop=Decimal("10.00"),
            limit=Decimal("10.05"),
            stop_loss=Decimal("9.8"),
        ),
        OrderSpec(1, "buy", "limit", 100, limit=Decimal("10.00"), stop_loss=Decimal("9.80")),
        OrderSpec(1, "buy", "market", 100, stop_loss=Decimal("9.80")),
        OrderSpec(1, "sell", "stop", 100, stop=Decimal("10.00"), purpose="stop", position_id=1),
        OrderSpec(1, "sell", "limit", 100, limit=Decimal("10.00"), purpose="exit", position_id=1),
        OrderSpec(1, "sell", "market", 100, purpose="exit", position_id=1),
    ]
    checked = 0
    for o_ in prices:
        for h in prices:
            for l_ in prices:
                if l_ > h:
                    continue
                for vol in (0, 1, 500):
                    bar = candle(et(MON, 9, 59), o_, h, l_, max(l_, min(h, o_)), vol)
                    for order in orders:
                        out = model.assess(order, bar, now)
                        checked += 1
                        if vol == 0:
                            assert isinstance(out, NoFill) and out.reason == "no_volume", (order, bar)
                            continue
                        if isinstance(out, NoFill):
                            continue
                        assert isinstance(out, FillDecision)
                        p = out.price
                        if order.order_type in ("stop", "stop_limit") and order.side == "buy":
                            assert order.stop is not None and bar.high >= order.stop
                            assert p >= max(order.stop, bar.open) + model.half_spread(
                                max(order.stop, bar.open)
                            )
                            if order.order_type == "stop_limit":
                                assert order.limit is not None and p <= order.limit
                        elif order.order_type == "stop":
                            assert order.stop is not None and bar.low <= order.stop
                            assert p <= min(order.stop, bar.open) - model.half_spread(
                                min(order.stop, bar.open)
                            )
                        elif order.order_type == "limit":
                            assert p == order.limit and out.slippage == 0, (order, bar, p)
                        elif order.side == "buy":
                            assert p > bar.open
                        else:
                            assert p < bar.open
    assert checked > 1000


@dataclasses.dataclass
class BrokerEnv:
    broker: SimBroker
    clock: FixedClock
    run_id: int
    aaa: int
    cfg: int
    factory: sessionmaker[Session]


@pytest.fixture
def benv(db_factory: sessionmaker[Session]) -> BrokerEnv:
    clock = FixedClock(et(MON, 9, 35, 5))
    settings = RuntimeSettings.model_validate({"starting_cash": "100000"})
    run = get_live_run(db_factory, clock, settings)
    with db_factory() as s:
        aaa = add_symbol(s, "AAA", questrade_id=11)
        cfg = add_strategy_config(s)
        s.commit()
    broker = SimBroker(
        db_factory, clock, Ledger(CAL), _model(settings), run.id, calendar=CAL, settings=lambda: settings
    )
    return BrokerEnv(broker, clock, run.id, aaa, cfg, db_factory)


def _entry(env: BrokerEnv, stop: str = "10.00") -> int:
    return env.broker.submit(
        OrderSpec(
            env.aaa,
            "buy",
            "stop",
            10,
            stop=Decimal(stop),
            stop_loss=Decimal("9.50"),
            strategy_config_id=env.cfg,
            reason="orb_breakout",
        )
    )


def _order(env: BrokerEnv, order_id: int) -> m.Order:
    with env.factory() as s:
        o = s.get(m.Order, order_id)
        assert o is not None
        return o


def test_8_bars_before_the_order_zero_volume_bars_and_limits_through_the_broker(benv: BrokerEnv) -> None:
    """Real SimBroker + CandleFillModel. Submitted at 09:36:00: the 09:34 bar and the 09:35 bar (ending
    exactly at submission) never fill it, even handed over directly and trading far through the stop, and a
    zero-volume 09:36 bar through the stop doesn't either; the 09:37 bar does. Then a sell limit exit met by a
    bar opening far above it fills at exactly the limit, never the better open."""
    benv.clock.set(et(MON, 9, 36))
    oid = _entry(benv)
    through = ("10.00", "11.00", "9.90", "10.90")
    assert benv.broker.on_candles({benv.aaa: candle(et(MON, 9, 34), *through)}, et(MON, 9, 36)) == []
    assert benv.broker.on_candles({benv.aaa: candle(et(MON, 9, 35), *through)}, et(MON, 9, 36)) == []
    assert _order(benv, oid).status == "working"
    assert (
        benv.broker.on_candles(
            {benv.aaa: candle(et(MON, 9, 36), "10.50", "11.00", "10.40", "10.90", 0)}, et(MON, 9, 37)
        )
        == []
    )
    assert _order(benv, oid).status == "working"
    (entry,) = benv.broker.on_candles(
        {benv.aaa: candle(et(MON, 9, 37), "10.00", "10.10", "9.95", "10.05")}, et(MON, 9, 38)
    )
    assert (entry.order_id, entry.ts) == (oid, et(MON, 9, 38))
    benv.clock.set(et(MON, 9, 38))
    limit_id = benv.broker.submit(
        OrderSpec(
            benv.aaa,
            "sell",
            "limit",
            10,
            limit=Decimal("10.50"),
            purpose="exit",
            position_id=entry.position_id,
            reason="target",
        )
    )
    (ev,) = benv.broker.on_candles(
        {benv.aaa: candle(et(MON, 9, 38), "11.00", "11.20", "10.90", "11.10")}, et(MON, 9, 39)
    )
    assert (ev.order_id, ev.price) == (limit_id, Decimal("10.50"))


def test_9_entry_cutoff_and_regular_hours_are_checked_on_the_bar_start(benv: BrokerEnv) -> None:
    """Cutoff 15:30 (close - 30 min): the 15:29-15:30 bar still fills an entry (at 15:30); the 15:30 bar
    cancels another (`entry cutoff`) although it trades through its stop. A pre-market bar (09:29-09:30 the
    next day) never fills a working exit, which keeps working."""
    benv.clock.set(et(MON, 15, 0))
    early = _entry(benv, stop="10.00")
    late = _entry(benv, stop="10.30")
    (ev,) = benv.broker.on_candles(
        {benv.aaa: candle(et(MON, 15, 29), "10.00", "10.20", "9.95", "10.10")}, et(MON, 15, 30)
    )
    assert (ev.order_id, ev.ts) == (early, et(MON, 15, 30))
    assert (
        benv.broker.on_candles(
            {benv.aaa: candle(et(MON, 15, 30), "10.20", "10.60", "10.15", "10.50")}, et(MON, 15, 31)
        )
        == []
    )
    o = _order(benv, late)
    assert o.status == "cancelled" and ENTRY_CUTOFF in (o.cancel_reason or ""), (o.status, o.cancel_reason)
    # pre-market bar: an exit submitted at 09:28 the next day stays working through the 09:29 bar
    benv.clock.set(et(TUE, 9, 28))
    exit_id = benv.broker.submit(
        OrderSpec(
            benv.aaa, "sell", "market", 10, purpose="exit", position_id=ev.position_id, reason="flatten_close"
        )
    )
    assert (
        benv.broker.on_candles(
            {benv.aaa: candle(et(TUE, 9, 29), "10.00", "10.20", "9.95", "10.10")}, et(TUE, 9, 30)
        )
        == []
    )
    assert _order(benv, exit_id).status == "working"


async def test_10_forced_close_without_a_bar_after_1540(db_factory: sessionmaker[Session]) -> None:
    """Tuesday AAA has no bar after 15:40: the 15:50 flatten can't fill, so at 15:59 the runner cancels it and
    submits `replay_forced_close`, filled at 16:00 by a synthetic bar at the last close (10.60) - slip - hs;
    nothing is open at the end of the session."""
    world = seed_stack(db_factory, tail="none")
    wall = FixedClock(SAT)
    run_id = create(db_factory, wall)
    final = await run_real(db_factory, wall, run_id)
    assert final.status == "completed", final.error
    aaa = world.symbols["AAA"]
    tue = [t for t in _trades(db_factory, run_id) if t.session_date == TUE]
    assert [(t.symbol_id, t.exit_reason) for t in tue] == [(aaa, "replay_forced_close")]
    exit_fill = [f for f in _fills(db_factory, run_id) if f.symbol_id == aaa and f.side == "sell"][-1]
    model = _model(final.settings)
    last = Decimal("10.60")
    assert (exit_fill.ts, exit_fill.price) == (
        et(TUE, 16, 0),
        last - model.slip(last) - model.half_spread(last),
    )
    assert final.progress.forced_closes == 1
    assert _open_positions(db_factory, run_id) == []


async def test_11_forced_close_with_zero_volume_bars_to_the_close(db_factory: sessionmaker[Session]) -> None:
    """Tuesday AAA trades with ZERO volume from 15:40 to the close (a halt, a data gap written as zeros): the
    flatten and the forced exit can't fill on those bars. The forced close exists so a replay never carries a
    position into the next day (plan decision "Forced close"), so the position must still be closed by the end
    of the session (by the synthetic bar), never left open."""
    seed_stack(db_factory, tail="zero")
    wall = FixedClock(SAT)
    run_id = create(db_factory, wall)
    final = await run_real(db_factory, wall, run_id)
    assert final.status == "completed", final.error
    assert final.progress.forced_closes == 1
    assert _open_positions(db_factory, run_id) == [], "a replay position was carried past the session's close"


# ====================================================================================================
# ISOLATION
# ====================================================================================================
ALLOWED_SHARED_CHANGES = {"runs", "audit_log", "strategy_configs"}


def _table_contents(factory: sessionmaker[Session]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    with factory() as s:
        for t in m.Base.metadata.sorted_tables:
            order = list(t.primary_key.columns) or list(t.c)
            rows = s.execute(select(t).order_by(*order)).mappings().all()
            out[t.name] = [json.dumps(_norm(dict(r)), sort_keys=True, default=str) for r in rows]
    return out


async def test_12_a_replay_writes_only_its_own_rows_and_nothing_live(
    db_factory: sessionmaker[Session],
) -> None:
    world = seed_stack(db_factory)
    wall = FixedClock(SAT)
    live = StrategyRegistry(db_factory, wall)
    live_views = {k: live.current(k) for k in live.keys()}
    live_enabled = [(s.key, c.id) for s, c in live.enabled()] if hasattr(live, "enabled") else []
    before = _table_contents(db_factory)

    run_id = create(db_factory, wall)
    final = await run_real(db_factory, wall, run_id)
    assert final.status == "completed", final.error
    after = _table_contents(db_factory)

    changed = sorted(n for n in before if before[n] != after[n])
    run_owned = {t.name for t in run_tables()}
    illegal = [n for n in changed if n not in run_owned and n not in ALLOWED_SHARED_CHANGES]
    assert illegal == [], f"a replay changed shared tables: {illegal}"
    # every new row of a run-owned table carries the replay's run id (never the live run's, never NULL)
    with db_factory() as s:
        for t in run_tables():
            foreign = s.execute(
                select(func.count()).select_from(t).where(t.c.run_id.is_distinct_from(run_id))
            ).scalar_one()
            before_foreign = len(before[t.name])
            assert foreign == before_foreign, f"{t.name}: rows written outside the replay's run id"
        audits = list(s.execute(select(m.AuditLog.action)).scalars())
        cfgs = list(s.execute(select(m.StrategyConfig).order_by(m.StrategyConfig.id)).scalars())
    assert audits == ["replay.start"]
    new_cfgs = [c for c in cfgs if c.id not in {v.id for v in live_views.values()}]
    assert [(c.scope, c.created_by) for c in new_cfgs] == [("replay", f"replay:{run_id}")]
    # the live registry is exactly as before
    live2 = StrategyRegistry(db_factory, wall)
    assert {k: live2.current(k) for k in live2.keys()} == live_views
    if live_enabled:
        assert [(s.key, c.id) for s, c in live2.enabled()] == live_enabled
    assert SettingsStore(db_factory, now=wall.now).load().approval_mode == "manual"
    with db_factory() as s:
        live_rows = s.execute(
            select(func.count()).select_from(m.EventLog).where(m.EventLog.run_id == world.live_run_id)
        ).scalar_one()
    assert live_rows == sum(1 for r in before["event_log"] if f'"run_id": {world.live_run_id}' in r)


async def test_13_replay_alongside_live_worker_leaves_live_proposals_alone(
    db_factory: sessionmaker[Session],
) -> None:
    """A live pending entry proposal whose expiry falls INSIDE the replay's simulated window, while the live
    worker keeps ticking its own ProposalService: the replay (auto approvals, its own ticks at Monday times)
    neither approves, expires nor audits the live proposal, and writes no live order."""
    from trader.engine.proposals import ProposalService

    world = seed_stack(db_factory)
    wall = FixedClock(SAT)
    with session_scope(db_factory) as s:
        sig = m.Signal(
            run_id=world.live_run_id,
            strategy_config_id=world.configs["orb_sip"],
            symbol_id=world.symbols["AAA"],
            session_date=MON,
            event_key="orb_open",
            ts=et(MON, 9, 35, 5),
            intent={"kind": "enter_long"},
            evidence={},
        )
        s.add(sig)
        s.flush()
        prop = m.Proposal(
            run_id=world.live_run_id,
            signal_id=sig.id,
            kind="entry",
            order_spec={},
            qty=10,
            status="pending",
            created_at=et(MON, 9, 35, 5),
            expires_at=et(MON, 9, 40),  # inside the replay's simulated Monday
            escalations=0,
        )
        s.add(prop)
        s.flush()
        live_prop_id = prop.id

    def live_state() -> tuple[Any, ...]:
        with db_factory() as s:
            p = s.get(m.Proposal, live_prop_id)
            assert p is not None
            orders = s.execute(
                select(func.count()).select_from(m.Order).where(m.Order.run_id == world.live_run_id)
            ).scalar_one()
            return (p.status, p.decided_at, p.decided_via, p.expired_at, orders)

    before = live_state()
    run_id = create(db_factory, wall)
    live_service = ProposalService(
        db_factory,
        FixedClock(et(MON, 9, 36)),  # the live worker's own clock: before the expiry
        SettingsStore(db_factory, now=wall.now),
        cast(Any, SimpleNamespace()),
        world.live_run_id,
    )
    done = asyncio.Event()

    async def live_worker() -> int:
        steps = 0
        while not done.is_set():
            live_service.expire_due(et(MON, 9, 36))
            steps += 1
            await asyncio.sleep(0)
        return steps

    async def replay() -> ReplayRun:
        try:
            return await run_real(db_factory, wall, run_id)
        finally:
            done.set()

    final, steps = await asyncio.gather(replay(), live_worker())
    assert final.status == "completed", final.error and steps > 0
    assert live_state() == before
    with db_factory() as s:
        actions = list(s.execute(select(m.AuditLog.action)).scalars())
    assert actions == ["replay.start"]


# ====================================================================================================
# RUNNER
# ====================================================================================================
async def test_14_cancel_between_sessions_stops_before_the_next_day(
    db_factory: sessionmaker[Session],
) -> None:
    """A cancel requested during Monday's end of session: Tuesday never starts (no Tuesday signal, order or
    event), the run is `cancelled` with one session done, and nothing is left open or working."""
    seed_stack(db_factory)
    wall = FixedClock(SAT)
    run_id = create(db_factory, wall)

    class CancelAtEod:
        def __init__(self, inner: ReplayEngine) -> None:
            self._inner = inner

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

        @property
        def broker(self) -> Any:
            return self._inner.broker

        async def end_of_session(self, session_date: date) -> Any:
            out = await self._inner.end_of_session(session_date)
            if session_date == MON:
                assert runner.request_cancel(db_factory, wall, run_id, "test")
            return out

    final = await run_real(
        db_factory, wall, run_id, wrap_engine=lambda e, _r: cast(ReplayEngine, CancelAtEod(e))
    )
    assert final.status == "cancelled" and final.finished_at is not None
    assert (final.progress.sessions_done, final.progress.current_date) == (1, MON)
    with db_factory() as s:
        tue_signals = s.execute(
            select(func.count())
            .select_from(m.Signal)
            .where(m.Signal.run_id == run_id, m.Signal.session_date == TUE)
        ).scalar_one()
        working = s.execute(
            select(func.count())
            .select_from(m.Order)
            .where(m.Order.run_id == run_id, m.Order.status == "working")
        ).scalar_one()
        late_events = s.execute(
            select(func.count())
            .select_from(m.EventLog)
            .where(m.EventLog.run_id == run_id, m.EventLog.ts >= et(TUE, 0, 0))
        ).scalar_one()
    assert (tue_signals, working, late_events) == (0, 0, 0)
    assert _open_positions(db_factory, run_id) == []


def test_15_crashed_process_is_settled_failed_on_the_next_start(db_factory: sessionmaker[Session]) -> None:
    """A replay process killed hard during its second session leaves its row `running` with the lock gone;
    the next `create_replay` settles it `failed` ("abandoned") and the new run starts and completes."""
    seed_stack(db_factory)
    wall = FixedClock(SAT)
    run_id = create(db_factory, wall)
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys\n"
            "from tests.gauntlet.test_p5_rc_breaker import crash_main\n"
            "crash_main(sys.argv[1], sys.argv[2], int(sys.argv[3]))\n",
            _url(db_factory),
            SAT.isoformat(),
            str(run_id),
        ],
        cwd=APP_DIR,
        env=_child_env("0"),
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 9, proc.stderr[-3000:]
    crashed = load_replay_run(db_factory, run_id)
    assert crashed.status == "running" and crashed.progress.sessions_done == 1
    later = FixedClock(SAT + timedelta(minutes=10))
    new_id = create(db_factory, later)
    crashed = load_replay_run(db_factory, run_id)
    assert (crashed.status, crashed.error, crashed.finished_at) == ("failed", "abandoned", later.now())
    assert asyncio.run(run_real(db_factory, later, new_id)).status == "completed"


async def test_16_lock_held_twice_is_busy_and_a_running_replay_is_never_reconciled(
    db_factory: sessionmaker[Session],
) -> None:
    """While one replay runs (inside its engine call), a second `run_replay` of another queued row raises
    ReplayBusy, `create_replay` raises ReplayBusy, and `reconcile_abandoned` (called by the list route) leaves
    the running row alone. The first run then completes."""
    seed_stack(db_factory)
    wall = FixedClock(SAT)
    first = create(db_factory, wall, replay_request(date_to=MON))
    with session_scope(db_factory) as s:  # a second queued row, written behind create's back
        row = s.get(m.Run, first)
        assert row is not None
        other = m.Run(
            mode="replay",
            started_at=SAT,
            updated_at=SAT,
            params=dict(row.params),
            status="queued",
            label="second",
            progress=row.progress,
            cancel_requested=False,
        )
        s.add(other)
        s.flush()
        second = other.id
    seen: dict[str, Any] = {}

    class Probe:
        def __init__(self, inner: ReplayEngine) -> None:
            self._inner = inner

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

        @property
        def broker(self) -> Any:
            return self._inner.broker

        async def run_event(self, event_key: str, session_date: date) -> Any:
            if "busy" not in seen:
                with pytest.raises(ReplayBusy):
                    await runner.run_replay(
                        runner.ReplayDeps(db_factory, wall, CAL, *cast(Any, (None, None, None))), second
                    )
                with pytest.raises(ReplayBusy):
                    create(db_factory, wall, replay_request(date_to=MON))
                seen["busy"] = True
                seen["reconciled"] = runner.reconcile_abandoned(
                    db_factory, FixedClock(SAT + timedelta(hours=1))
                )
            return await self._inner.run_event(event_key, session_date)

    final = await run_real(db_factory, wall, first, wrap_engine=lambda e, _r: cast(ReplayEngine, Probe(e)))
    assert seen.get("busy") is True and seen["reconciled"] == []
    assert final.status == "completed", final.error
    assert load_replay_run(db_factory, second).status == "queued"


@pytest.mark.parametrize(
    ("wall", "mode"),
    [
        (datetime(2026, 11, 30, 9, 14, 59, tzinfo=ET), "full"),
        (datetime(2026, 11, 30, 9, 15, tzinfo=ET), "offline"),
        (datetime(2026, 11, 30, 16, 29, 59, tzinfo=ET), "offline"),
        (datetime(2026, 11, 30, 16, 30, tzinfo=ET), "full"),
        (datetime(2026, 11, 27, 14, 0, tzinfo=ET), "offline"),  # early-close day, after its 13:00 close
        (datetime(2026, 11, 26, 10, 0, tzinfo=ET), "full"),  # Thanksgiving: not a session
        # DST ended on Sunday 2026-11-01: 13:15 UTC on Monday Nov 2 is 08:15 EST (it would be 09:15 in EDT)
        (datetime(2026, 11, 2, 13, 15, tzinfo=UTC), "full"),
        (datetime(2026, 11, 2, 14, 15, tzinfo=UTC), "offline"),
        # DST starts Sunday 2026-03-08: 13:15 UTC on Monday Mar 9 is 09:15 EDT (08:15 in EST)
        (datetime(2026, 3, 9, 13, 15, tzinfo=UTC), "offline"),
    ],
)
def test_17_offline_window_is_0915_to_1630_et_on_session_days(
    db_factory: sessionmaker[Session], wall: datetime, mode: str
) -> None:
    seed_stack(db_factory)
    clock = FixedClock(wall)
    today = wall.astimezone(ET).date()
    req = ReplayRequest(
        today - timedelta(days=14),
        today - timedelta(days=7),
        strategies={"orb_sip": StrategyOverride(params={"require_catalyst": False})},
    )
    run_id = create(db_factory, clock, req)
    assert load_replay_run(db_factory, run_id).data_mode == mode


async def test_18_130_sessions_hold_a_bounded_number_of_bars(db_factory: sessionmaker[Session]) -> None:
    """130 sessions in `full` mode through FakeQuestrade (78 five-minute and 390 one-minute bars per session),
    with the universe rotating daily: at every step the 1-minute bars held are only today's, the opening bars
    held are one per (symbol, session) of the span, and the daily bars stay within the range."""
    tickers = [f"S{i:02d}" for i in range(12)]
    world = seed_replay_world(db_factory, tickers=("SPY", *tickers), strategies=False)
    sessions: list[date] = []
    d = date(2026, 5, 1)
    while len(sessions) < 130:
        if CAL.is_session(d):
            sessions.append(d)
        d += timedelta(days=1)
    wall = FixedClock(datetime.combine(sessions[-1] + timedelta(days=3), time(20), tzinfo=ET))
    lookback = CAL.sessions_before(sessions[0], 14)
    qt = FakeQuestrade()
    for i in range(1 + len(tickers)):  # Questrade ids 1000 + position (seed_replay_world)
        qid = 1000 + i
        five: list[Candle] = []
        one: list[Candle] = []
        for day in [*lookback, *sessions]:
            o = CAL.session_open(day)
            five += [
                candle(o + k * 5 * ONE_MINUTE, "10", "10.1", "9.9", "10", 1000, minutes=5) for k in range(78)
            ]
            if day in sessions:
                one += [candle(o + k * ONE_MINUTE, "10", "10.1", "9.9", "10", 100) for k in range(390)]
        qt.add_bars(qid, "FiveMinutes", five)
        qt.add_bars(qid, "OneMinute", one)
        qt.add_bars(qid, "OneDay", [day_bar(x, "10.00") for x in [*lookback, *sessions]])
    with session_scope(db_factory) as s:  # the universe rotates: 4 names a day out of 12
        for n, day in enumerate(sessions):
            for k in range(4):
                t = tickers[(n + k) % len(tickers)]
                s.add(
                    m.UniverseSnapshot(
                        session_date=day,
                        symbol_id=world.symbols[t],
                        price=Decimal("20"),
                        avg_volume=2_000_000,
                        atr14=Decimal("1.0"),
                        source="finviz",
                    )
                )
    clock = ReplayClock(CAL.session_open(sessions[0]) - ONE_MINUTE)
    data = ReplayData(
        db_factory,
        clock,
        wall,
        CAL,
        cast(Any, qt),
        run_id=world.live_run_id,
        date_from=sessions[0],
        date_to=sessions[-1],
        half_spread_bps=Decimal("5"),
        questrade_window_days=400,
        lookback_sessions=14,
    )
    span = len(lookback) + len(sessions)
    peak = {"opening_bars": 0, "daily_bars": 0, "minute_bars": 0}
    for day in sessions:
        clock.set(CAL.session_open(day))
        await data.prepare_day(day)
        ids = sorted({u.symbol_id for u in await data.universe(day)})
        await data.load_minute_bars(ids, day)
        clock.set(CAL.session_close(day))
        held = data.held_counts()
        for name in peak:
            peak[name] = max(peak[name], held[name])
        # 1-minute bars: today's only (the day's names plus SPY)
        assert held["minute_bars"] <= 390 * (len(ids) + 1), (day, held)
        # opening bars: at most one per symbol and session of the span, never a whole 78-bar response
        assert held["opening_bars"] <= (len(tickers) + 1) * span, (day, held)
    # at the end only the look-back window (plus the last day) should remain per symbol
    assert data.held_counts()["opening_bars"] <= (len(tickers) + 1) * (14 + 1)
    assert peak["minute_bars"] <= 390 * 5
