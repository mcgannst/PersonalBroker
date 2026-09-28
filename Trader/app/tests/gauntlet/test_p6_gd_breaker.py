"""P6-GD gauntlet, attempt 1 (Breaker): the decision log (P6-T10 recorder, P6-T12 API and CSV).

Targets, in order of importance:
- LOGGING-ONLY: every table except `decision_log` byte-identical around every recorder pass of a simulated
  live day, and across two fresh databases (live day and golden replay) with and without the recorder; a
  failing recorder (DB error, bad data, an exception in `explain_orb`) never touches trading, never raises
  into the replay and never writes a notification; no database work on the event loop.
- CORRECTNESS: `explain_orb` against the real `OrbSip` over generated scans; planned-versus-fill signs for
  buys and sells of every order type; exit categories; summary counts equal the rows.
- FREEZE and RACES, PRUNE boundaries, ISOLATION of replay rows, API error bodies, the CSV formula guard, and
  a big day's volume and streaming.
"""

import asyncio
import csv
import io
import json
import random
import re
import threading
import traceback
from collections import Counter
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, cast

import pytest
from alembic import command
from sqlalchemy import create_engine, func, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from tests.api.conftest import make_client
from tests.conftest import alembic_config
from tests.decisions.test_read import add_decision
from tests.decisions.test_recorder import (
    CAL as REC_CAL,
)
from tests.decisions.test_recorder import (
    OPEN as REC_OPEN,
)
from tests.decisions.test_recorder import (
    SCAN_AT,
    World,
    deps,
    make_world,
    rows,
    scan_world,
)
from tests.decisions.test_recorder import (
    D as REC_D,
)
from tests.decisions.test_recorder import (
    et as rec_et,
)
from tests.factories import add_run, add_strategy_config, add_symbol
from tests.fakes_api import make_services, test_core
from tests.integration.test_simulated_day import CAL, DAY, QT, Day, et, opening, setup_day
from tests.replay.test_decisions_replay import FIELDS, _core
from tests.replay.test_golden import (
    EXPECTED_PATH,
    WALL,
    create_golden_replay,
    golden_part,
    load_data,
    normalized,
    seed_from_data,
)
from tests.strategies.fakes import (
    FakeCatalyst,
    FakeCatalysts,
    FakeData,
    bar,
    make_ctx,
    position,
    working_entry,
)
from trader.api.routers import decisions as decisions_router
from trader.api.routers.performance import close_when_done
from trader.db import models as m
from trader.db.models import SCHEMA
from trader.db.session import make_engine, make_session_factory, session_scope
from trader.decisions import recorder
from trader.decisions.export import DECISION_CSV_COLUMNS, decisions_csv
from trader.decisions.orb_explain import explain_orb, first_failure
from trader.decisions.prune import prune
from trader.decisions.recorder import LiveScanData, record_day
from trader.decisions.types import RecorderDeps
from trader.jobs.nightly import NightlyDeps, run_nightly
from trader.jobs.premarket import PremarketDeps, run_premarket
from trader.market.clock import FixedClock
from trader.replay import runner
from trader.reports.export import FORMULA_STARTS
from trader.settings_store import RuntimeSettings, SettingsStore
from trader.strategies.orb_sip import ORB_EVENT, OrbSip, OrbSipParams

pytestmark = pytest.mark.db


# ====================================================================================================
# Helpers: a byte-level dump of every table but the journal, and two fresh databases
# ====================================================================================================
JOURNAL = "decision_log"


def dump(factory: sessionmaker[Session], *, skip: tuple[str, ...] = (JOURNAL,)) -> dict[str, list[str]]:
    """Every row of every table in the schema (as `row_to_json` text, sorted), except `skip`, plus the
    last value of every identity sequence except the journal's."""
    out: dict[str, list[str]] = {}
    with factory() as s:
        tables = s.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname = :s ORDER BY tablename"), {"s": SCHEMA}
        ).scalars()
        for t in list(tables):
            if t in skip:
                continue
            out[t] = sorted(s.execute(text(f'SELECT row_to_json(x)::text FROM {SCHEMA}."{t}" x')).scalars())
        seqs = s.execute(
            text("SELECT sequencename, last_value FROM pg_sequences WHERE schemaname = :s"), {"s": SCHEMA}
        ).all()
    out["__sequences__"] = sorted(f"{n}={v}" for n, v in seqs if not str(n).startswith(JOURNAL))
    return out


def dump_diff(a: dict[str, list[str]], b: dict[str, list[str]]) -> list[str]:
    diffs: list[str] = []
    for t in sorted(set(a) | set(b)):
        x, y = a.get(t, []), b.get(t, [])
        if x != y:
            only_a = sorted(set(x) - set(y))[:2]
            only_b = sorted(set(y) - set(x))[:2]
            diffs.append(f"{t}: {len(x)} vs {len(y)} rows; only first {only_a}; only second {only_b}")
    return diffs


@pytest.fixture
def twin_factories(pg_url: str) -> Iterator[tuple[sessionmaker[Session], sessionmaker[Session]]]:
    """Two fresh, migrated databases in the test container (identical starting state)."""
    names = ("p6gd_twin_a", "p6gd_twin_b")
    admin = create_engine(pg_url, isolation_level="AUTOCOMMIT")
    engines = []
    try:
        for name in names:
            with admin.connect() as conn:
                conn.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
                conn.execute(text(f"CREATE DATABASE {name}"))
            url = make_url(pg_url).set(database=name).render_as_string(hide_password=False)
            command.upgrade(alembic_config(url), "head")
            engines.append(make_engine(url))
        yield make_session_factory(engines[0]), make_session_factory(engines[1])
    finally:
        for e in engines:
            e.dispose()
        with admin.connect() as conn:
            for name in names:
                conn.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
        admin.dispose()


# ====================================================================================================
# LOGGING-ONLY 1: a simulated live day
# ====================================================================================================
Step = Callable[[Day, str], Awaitable[None]]


async def simulated_day(factory: sessionmaker[Session], step: Step) -> Day:
    """The P5 simulated day (nightly, pre-market, the 9:35 scan with auto approval, the entry fill, the
    protective stop, the overlay, the flatten, the end of session); `step` runs after each of them."""
    d = setup_day(factory)
    await run_nightly(NightlyDeps(d.factory, d.clock, CAL, d.finviz, d.fq, d.store.load()), DAY)
    await step(d, "nightly")
    d.clock.set(et(8, 0))
    d.fq.set_quote(QT["AAA"], "20.99", "21.01", "21.00", d.clock.now())
    d.fq.set_quote(QT["BBB"], "20.09", "20.11", "20.10", d.clock.now())
    await run_premarket(PremarketDeps(d.factory, d.clock, d.finviz, d.data, d.catalysts, d.store.load()), DAY)
    await step(d, "premarket")
    d.clock.set(et(9, 35, 5))
    d.fq.add_bars(QT["AAA"], "FiveMinutes", [opening(DAY, "21.00", "21.50", "20.90", "21.40", 5000)])
    d.fq.add_bars(QT["BBB"], "FiveMinutes", [opening(DAY, "20.00", "20.40", "19.95", "20.30", 3000)])
    await d.engine.run_event("orb_open", DAY)
    await step(d, "orb_open")
    d.clock.set(et(9, 36))
    d.fq.set_quote(QT["AAA"], "21.52", "21.55", "21.53", d.clock.now())
    await d.engine.poll_quotes()
    await step(d, "entry fill")
    await d.engine.tick(d.clock.now())
    await step(d, "tick")
    d.clock.set(et(15, 30))
    d.fq.set_quote(QT["SPY"], "501.99", "502.01", "502.00", d.clock.now())
    d.fq.set_quote(QT["AAA"], "21.70", "21.72", "21.71", d.clock.now())
    await d.engine.run_event("overlay_decision", DAY)
    await step(d, "overlay")
    d.clock.set(et(15, 50))
    await d.engine.run_event("flatten", DAY)
    await step(d, "flatten")
    d.clock.set(et(15, 50, 2))
    d.fq.set_quote(QT["AAA"], "21.90", "21.92", "21.91", d.clock.now())
    await d.engine.poll_quotes()
    await step(d, "exit fill")
    d.clock.set(et(16, 0))
    await d.engine.end_of_session(DAY)
    await step(d, "end of session")
    return d


async def test_logging_only_live_day_every_table_byte_identical_per_pass_and_across_databases(
    twin_factories: tuple[sessionmaker[Session], sessionmaker[Session]],
) -> None:
    with_rec, without = twin_factories
    passes: list[str] = []

    async def record_and_check(d: Day, label: str) -> None:
        deps_ = RecorderDeps(d.factory, d.clock, CAL, d.store.load, LiveScanData(d.factory, CAL))
        final = label == "end of session"
        for kwargs in ({"final": final}, {"rebuild": True, "final": final}, {"final": final}):
            before = dump(d.factory)
            await record_day(deps_, d.run_id, DAY, **kwargs)
            after = dump(d.factory)
            assert after == before, (label, kwargs, dump_diff(before, after))
            passes.append(label)

    async def nothing(d: Day, label: str) -> None:
        return None

    recorded = await simulated_day(with_rec, record_and_check)
    plain = await simulated_day(without, nothing)
    assert len(passes) == 27 and recorded.run_id == plain.run_id

    a, b = dump(with_rec), dump(without)
    assert a == b, dump_diff(a, b)  # every trading table, byte for byte, ids and times included
    assert a["fills"] and a["trades"] and a["candidates"] and a["cash_ledger"] and a["event_log"]
    with with_rec() as s:
        journal = s.execute(select(func.count()).select_from(m.DecisionLog)).scalar_one()
        assert (
            journal > 0
            and s.execute(
                select(func.bool_and(m.DecisionLog.final)).where(m.DecisionLog.run_id == recorded.run_id)
            ).scalar_one()
        )
    with without() as s:
        assert s.execute(select(func.count()).select_from(m.DecisionLog)).scalar_one() == 0


# ====================================================================================================
# LOGGING-ONLY 2: the golden replay in two databases, the hook on and off
# ====================================================================================================
async def _golden_replay(factory: sessionmaker[Session], *, hook: bool) -> int:
    seed_from_data(factory, load_data())
    wall = FixedClock(WALL)
    run_id = create_golden_replay(factory, wall, "twin")
    async with runner.open_replay_deps(_core(factory, wall), data_mode="offline") as deps_:
        use = deps_ if hook else runner.ReplayDeps(*[getattr(deps_, f) for f in FIELDS], None)
        final = await runner.run_replay(use, run_id)
    assert final.status == "completed", final.error
    return run_id


async def test_logging_only_replay_every_table_byte_identical_with_and_without_the_hook(
    twin_factories: tuple[sessionmaker[Session], sessionmaker[Session]],
) -> None:
    on, off = twin_factories
    on_id = await _golden_replay(on, hook=True)
    off_id = await _golden_replay(off, hook=False)
    assert on_id == off_id
    a, b = dump(on), dump(off)
    assert a == b, dump_diff(a, b)  # runs.progress/result, orders, fills, ledger, event_log: all equal
    with on() as s:
        runs = set(s.execute(select(m.DecisionLog.run_id).distinct()).scalars())
    assert runs == {on_id}


# ====================================================================================================
# LOGGING-ONLY 3: a failing recorder inside the replay never touches trading or Telegram
# ====================================================================================================
def _explain_boom(*args: Any, **kwargs: Any) -> Any:
    raise ValueError("explain failed Authorization: Bearer abcdefghijklmnop1234")


def _db_boom(*args: Any, **kwargs: Any) -> Any:
    raise OperationalError("INSERT INTO decision_log", {}, Exception("server closed the connection"))


def _bad_data(*args: Any, **kwargs: Any) -> Any:
    raise InvalidOperation([Decimal])  # what a Decimal over a corrupt stored value raises


@pytest.mark.parametrize(
    ("target", "boom"),
    [("explain_orb", _explain_boom), ("write_locked", _db_boom), ("summarize", _bad_data)],
    ids=["explain_orb raises", "database error", "bad data"],
)
async def test_a_failing_recorder_in_the_replay_is_one_warning_per_day_and_nothing_else(
    db_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch, target: str, boom: Any
) -> None:
    monkeypatch.setattr(recorder, target, boom)
    run_id = await _golden_replay(db_factory, hook=True)
    assert golden_part(normalized(db_factory, run_id)) == json.loads(EXPECTED_PATH.read_text())
    with db_factory() as s:
        assert s.execute(select(func.count()).select_from(m.DecisionLog)).scalar_one() == 0
        assert s.execute(select(func.count()).select_from(m.Notification)).scalar_one() == 0
        events = list(
            s.execute(
                select(m.EventLog).where(m.EventLog.source == "decisions").order_by(m.EventLog.id)
            ).scalars()
        )
        run = s.get(m.Run, run_id)
    assert run is not None and run.status == "completed"
    assert len(events) == 4 and {e.level for e in events} == {"warning"}  # never relayed (error/critical)
    assert {e.run_id for e in events} == {run_id}
    assert all("abcdefghijklmnop1234" not in e.message + json.dumps(e.data) for e in events)


async def test_a_failing_live_pass_rolls_back_and_leaves_the_last_journal_and_every_table(
    db_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    w = scan_world(db_factory)
    d = deps(db_factory)
    assert (await record_day(d, w.run_id, REC_D)).skipped is None
    journal = _journal(db_factory, w.run_id)
    buy = w.order("R01", side="buy", order_type="stop", purpose="entry", status="filled", stop="21.51")
    w.fill(buy, "21.53", at=rec_et(9, 36))  # the fingerprint changes, so the next pass rebuilds
    before = dump(db_factory)

    calls = {"n": 0}
    real_cap = recorder.cap_data

    def cap_then_fail(data: Any) -> Any:  # fails after the day's rows were deleted in the transaction
        calls["n"] += 1
        if calls["n"] > 5:
            raise TypeError("Object of type bytes is not JSON serializable")
        return real_cap(data)

    for target, boom in (("cap_data", cap_then_fail), ("explain_orb", _explain_boom)):
        with monkeypatch.context() as mp:
            mp.setattr(recorder, target, boom)
            with pytest.raises((TypeError, ValueError)):
                await record_day(d, w.run_id, REC_D)
        assert _journal(db_factory, w.run_id) == journal, target  # not half-deleted
        after = dump(db_factory)
        assert after == before, dump_diff(before, after)
    rebuilt = await record_day(d, w.run_id, REC_D)
    assert rebuilt.skipped is None and rebuilt.stages["fill"] == 1


def _journal(factory: sessionmaker[Session], run_id: int) -> list[tuple[Any, ...]]:
    with factory() as s:
        return [
            tuple(r)
            for r in s.execute(
                select(m.DecisionLog.__table__)
                .where(m.DecisionLog.run_id == run_id)
                .order_by(m.DecisionLog.session_date, m.DecisionLog.seq)
            ).all()
        ]


# ====================================================================================================
# LOGGING-ONLY 4: no database work on the event loop
# ====================================================================================================
class _LoopGuard:
    """A session factory that records every session opened on the event loop's thread."""

    def __init__(self, inner: sessionmaker[Session], loop_thread: int) -> None:
        self.inner = inner
        self.kw = inner.kw
        self.loop_thread = loop_thread
        self.hits: list[str] = []

    def __call__(self, *args: Any, **kwargs: Any) -> Session:
        if threading.get_ident() == self.loop_thread:
            self.hits.append("".join(traceback.format_stack(limit=8)[:-1]))
        return self.inner(*args, **kwargs)


async def test_record_day_never_touches_the_database_on_the_event_loop(
    db_factory: sessionmaker[Session],
) -> None:
    """The plan (T10 Passes, T11 verifier fix): database work never blocks the worker's event loop. The
    live wiring reads the settings from the database (`quiet_settings` -> `SettingsStore.load`), so the
    settings callable must be called off the loop too."""
    w = scan_world(db_factory)
    guard = _LoopGuard(db_factory, threading.get_ident())
    factory = cast(sessionmaker[Session], guard)
    clock = FixedClock(rec_et(10, 0))
    rd = RecorderDeps(
        factory, clock, REC_CAL, SettingsStore(factory, now=clock.now).load, LiveScanData(factory, REC_CAL)
    )
    await record_day(rd, w.run_id, REC_D)
    await record_day(rd, w.run_id, REC_D)  # the cheap "unchanged" pass the worker loop makes every minute
    await record_day(rd, w.run_id, REC_D, final=True)
    assert guard.hits == [], f"{len(guard.hits)} session(s) opened on the event loop, first:\n{guard.hits[0]}"


# ====================================================================================================
# CORRECTNESS 1: explain_orb agrees with the real OrbSip over generated scans
# ====================================================================================================
@dataclass
class _Gen:
    params: OrbSipParams
    data: FakeData
    cats: dict[int, FakeCatalyst]
    held: list[int]
    working: list[int]
    entries_today: int


def _generate(rng: random.Random) -> _Gen:
    p = OrbSipParams(
        price_min=Decimal(rng.choice(["5", "10"])),
        price_max=Decimal(rng.choice(["25", "50"])),
        min_avg_volume=rng.choice([0, 1_000_000, 1_500_000]),
        min_atr=Decimal(rng.choice(["0", "0.50", "1.00"])),
        rvol_min=Decimal(rng.choice(["0", "1.00", "1.50", "2.00"])),
        top_n=rng.randint(1, 12),
        max_positions=rng.randint(1, 3),
        require_catalyst=rng.random() < 0.75,
        catalyst_min_quality=rng.choice([0, 50, 80, 100]),
        stop_atr_fraction=Decimal(rng.choice(["0.10", "0.50", "1"])),
        entry_offset=Decimal(rng.choice(["0", "0.01", "0.25"])),
        doji_body_pct_max=Decimal(rng.choice(["0", "0.10", "0.50"])),
    )
    data = FakeData()
    cats: dict[int, FakeCatalyst] = {}
    n = rng.randint(1, 18)
    for sid in range(1, n + 1):
        close = rng.choice(
            [p.price_min, p.price_max, p.price_min - Decimal("0.01"), p.price_max + Decimal("0.01")]
            + [Decimal(rng.randint(100, 8000)) / 100] * 3
        )
        shape = rng.choice(["bull", "bull", "bull", "bear", "doji", "flat", "malformed"])
        clean = rng.random() < 0.35  # a name that passes every screen (so slots run out: lower_rank)
        if clean:
            close, shape = (p.price_min + p.price_max) / 2, "bull"
        body = {"bull": Decimal("0.40"), "bear": Decimal("-0.40"), "doji": Decimal("0.01")}.get(
            shape, Decimal(0)
        )
        o = close - body
        hi, lo = max(o, close) + Decimal("0.10"), min(o, close) - Decimal("0.10")
        if shape == "flat":
            o = hi = lo = close
        if shape == "malformed":
            hi, lo = lo, hi
        avg_open = rng.choice([None, "1000", "1000", "1000", "0"])
        target = rng.choice(
            [p.rvol_min, p.rvol_min + Decimal("0.50"), p.rvol_min - Decimal("0.01"), Decimal("3")]
        )
        volume = max(0, int(target * 1000))
        atr_choice = rng.choice(
            [None, "0.40", "2", "60", str(p.min_atr), str(max(Decimal(0), p.min_atr - Decimal("0.01")))]
        )
        avg_volume = rng.choice([None, p.min_avg_volume, max(0, p.min_avg_volume - 1), 5_000_000])
        if clean:
            avg_open, volume, atr_choice, avg_volume = "1000", 3000, "2", 5_000_000
        data.add(
            sid,
            f"T{sid:02d}",
            bar(str(o), str(hi), str(lo), str(close), volume),
            avg_open_vol=avg_open,
            atr=atr_choice,
            avg_volume=avg_volume,
        )
        kind = rng.choice(
            ["none", "ok", "ok", "unclassified", "no_type", "bearish", "q_low", "q_none", "q_edge"]
        )
        cat = {
            "none": None,
            "ok": FakeCatalyst(quality=90),
            "unclassified": FakeCatalyst(classified=False),
            "no_type": FakeCatalyst(catalyst_type=rng.choice(["none", "unknown"])),
            "bearish": FakeCatalyst(direction="bearish"),
            "q_low": FakeCatalyst(quality=max(0, p.catalyst_min_quality - 1)),
            "q_none": FakeCatalyst(quality=None),
            "q_edge": FakeCatalyst(quality=p.catalyst_min_quality),
        }[kind]
        if clean:
            cat = FakeCatalyst(quality=100)
        if cat is not None:
            cats[sid] = cat
    ids = list(range(1, n + 1))
    held = [x for x in ids if rng.random() < 0.08]
    working = [x for x in ids if rng.random() < 0.08]
    return _Gen(p, data, cats, held, working, rng.choice([0, 0, 1]))


def _table_view(cat: FakeCatalyst | None) -> dict[str, Any] | None:
    """The recorder's live `_catalyst_view` of D's catalysts row."""
    if cat is None:
        return None
    return {
        "type": cat.catalyst_type,
        "direction": cat.direction,
        "quality": cat.quality,
        "classified": cat.classified,
    }


async def test_explain_orb_first_failure_equals_the_real_strategy_over_500_generated_scans() -> None:
    rng = random.Random(20260928)
    seen: Counter[str | None] = Counter()
    checked = 0
    for i in range(500):
        g = _generate(rng)
        strategy = OrbSip(g.params)
        ctx = make_ctx(
            g.data,
            g.params,
            FakeCatalysts(g.cats),
            positions=[position(100 + k, sid) for k, sid in enumerate(g.held)],
            orders=[working_entry(200 + k, sid) for k, sid in enumerate(g.working)],
            entries_today=g.entries_today,
        )
        event = next(e for e in strategy.schedule(ctx.calendar) if e.key == ORB_EVENT)
        await strategy.on_event(ctx, event)
        for rec in ctx.candidates:
            stored = rec.data.get("catalyst")
            catalyst = stored if isinstance(stored, dict) else _table_view(g.cats.get(rec.symbol_id))
            # the recorder reads the rows back from JSON: round-trip data and candle the same way
            data = json.loads(json.dumps(rec.data, default=str))
            candle = json.loads(json.dumps(rec.candle, default=str))
            checks = explain_orb(data, candle, g.params, catalyst=catalyst, reject_reason=rec.reject_reason)
            got = first_failure(checks)
            assert got == rec.reject_reason, (
                i,
                g.params,
                data,
                candle,
                catalyst,
                [c for c in checks if c.passed is False],
            )
            if rec.passed:
                assert not [c for c in checks if c.passed is False], (i, data)
            by = {c.name: c for c in checks}
            assert by["rvol"].value is not None and Decimal(by["rvol"].value) == rec.rvol
            seen[rec.reject_reason] += 1
            checked += 1
    assert checked > 500
    every = {
        None,
        "already_held",
        "entry_working",
        "bearish_candle",
        "doji",
        "malformed_bar",
        "price_out_of_range",
        "atr_missing",
        "atr_below_min",
        "avg_volume_below_min",
        "stop_invalid",
        "catalyst_missing",
        "catalyst_bearish",
        "catalyst_low_quality",
        "lower_rank",
    }
    assert every <= set(seen), every - set(seen)


# ====================================================================================================
# CORRECTNESS 2: planned versus fill, both sides, every order type; exit categories; summary counts
# ====================================================================================================
async def test_fill_differences_are_signed_worse_positive_for_every_side_and_type_and_the_summary_counts_them(
    db_factory: sessionmaker[Session],
) -> None:
    w = make_world(db_factory)
    # (ticker, side, type, purpose, stop, limit, fill, snapshot, expected diff, planned source)
    cases: list[
        tuple[str, str, str, str, str | None, str | None, str, dict[str, Any] | None, str | None, str]
    ] = [
        ("BST", "buy", "stop", "entry", "10.25", None, "10.27", None, "0.02", "stop"),
        ("BLM", "buy", "limit", "entry", None, "10.30", "10.28", None, "-0.02", "limit"),
        (
            "BMK",
            "buy",
            "market",
            "entry",
            None,
            None,
            "10.31",
            {"bid": "10.28", "ask": "10.30"},
            "0.01",
            "quote_ask",
        ),
        ("SST", "sell", "stop", "stop", "9.80", None, "9.70", None, "0.10", "stop"),
        ("SLM", "sell", "limit", "exit", None, "10.50", "10.52", None, "-0.02", "limit"),
        (
            "SMK",
            "sell",
            "market",
            "exit",
            None,
            None,
            "10.39",
            {"bid": "10.40", "ask": "10.42"},
            "0.01",
            "quote_bid",
        ),
        (
            "SRP",
            "sell",
            "market",
            "exit",
            None,
            None,
            "9.99",
            {"source": "candle_1m", "open": "10.00"},
            "0.01",
            "candle_open",
        ),
        ("SNQ", "sell", "market", "exit", None, None, "9.99", {"ask": "10.00"}, None, "quote_bid"),
    ]
    orders: dict[int, tuple[str | None, str]] = {}
    for i, (t, side, typ, purpose, stop, limit, price, snap, diff, source) in enumerate(cases):
        oid = w.order(t, side=side, order_type=typ, purpose=purpose, status="filled", stop=stop, limit=limit)
        w.fill(oid, price, qty=7, snapshot=snap, at=rec_et(10, i))
        orders[oid] = (diff, source)
    reasons = {
        "protective_stop": "stop",
        "flatten_close": "flatten",
        "overlay_negative": "overlay",
        "replay_forced_close": "replay_close",
        "kill_switch_daily_loss": "kill_switch",
        "a_reason_nobody_mapped": "other",
    }
    for k, reason in enumerate(reasons):
        w.trade(
            f"X{k}",
            exit_reason=reason,
            pnl="-1" if k % 2 else "2",
            pnl_r=None,
            opened=rec_et(10, 0),
            closed=rec_et(11, k),
        )
    await record_day(deps(db_factory), w.run_id, REC_D, final=True)

    fills = rows(db_factory, w.run_id, "fill")
    assert len(fills) == len(cases)
    for r in fills:
        diff, source = orders[r.ref["order_id"]]
        assert r.data["planned_source"] == source, r.ticker
        if diff is None:
            assert r.data["diff_per_share"] is None and r.data["diff_total"] is None, r.ticker
        else:
            assert Decimal(r.data["diff_per_share"]) == Decimal(diff), r.ticker
            assert Decimal(r.data["diff_total"]) == Decimal(diff) * 7, r.ticker
    exits = {r.reason: r.rule for r in rows(db_factory, w.run_id, "exit")}
    assert exits == reasons

    (day,) = rows(db_factory, w.run_id, "day")
    s = day.data
    assert s["fills"] == len(fills) and s["trades"] == len(reasons)
    assert (s["wins"], s["losses"]) == (3, 3) and Decimal(s["pnl"]) == Decimal(3)
    assert dict(s["exits_by_reason"]) == Counter(reasons.values())
    diffs = [Decimal(d) for d, _ in orders.values() if d is not None]
    assert Decimal(s["avg_fill_diff_per_share"]) == (sum(diffs) / len(diffs)).quantize(Decimal("0.0001"))


async def test_summary_counts_equal_the_scan_rows_when_spy_is_ranked(
    db_factory: sessionmaker[Session],
) -> None:
    """SPY is always in the universe and the strategy ranks it like any name (a busy macro morning gives it
    a high rvol). The strategy then stores a SPY candidate (price_out_of_range). The day's counts must
    agree with the rows it wrote, and the plan says the overlay symbol never appears in the scan."""
    w = make_world(db_factory)
    w.member("SPY", bar=("500", "501", "499", "500.5", 6000), avg_open="1000", price="500")
    w.candidate("SPY", 1, "6.0000", reject="price_out_of_range", bar=("500", "501", "499", "500.5", 6000))
    for i, t in enumerate(("R01", "R02", "R03")):
        vol = 5000 - i * 100
        w.member(t, bar=("21.00", "21.50", "20.90", "21.40", vol))
        w.candidate(t, i + 2, f"{Decimal(vol) / 1000:.4f}", reject=None if i == 0 else "lower_rank")
    w.member("LOW", bar=("21.00", "21.50", "20.90", "21.40", 800))
    w.event("strategy.orb_sip", "orb: ranked", {"ranked": 4, "selected": ["R01"]}, SCAN_AT)
    w.job("event:orb_open", {}, SCAN_AT - timedelta(seconds=1))
    await record_day(deps(db_factory), w.run_id, REC_D)

    scan = [r for r in rows(db_factory, w.run_id, "scan") if r.symbol_id is not None]
    (head,) = [r for r in rows(db_factory, w.run_id, "scan") if r.symbol_id is None]
    counts = head.data["counts"]
    by_rule = Counter(r.rule for r in scan if r.outcome == "rejected")
    assert counts["passed"] == sum(1 for r in scan if r.outcome == "passed")
    assert counts["rejects_by_rule"] == dict(by_rule)
    assert counts["scanned"] == len(scan), (counts, sorted(r.ticker or "" for r in scan))
    assert "SPY" not in {r.ticker for r in scan}


# ====================================================================================================
# FREEZE and RACES
# ====================================================================================================
async def test_in_place_changes_rebuild_unrelated_rows_do_not_and_a_final_day_survives_racing_passes(
    db_factory: sessionmaker[Session],
) -> None:
    w = scan_world(db_factory)
    d = deps(db_factory)
    sig = w.signal("R01", evidence={"rvol": "5.0"})
    prop = w.proposal(sig, "R01", status="pending")
    cat = w.catalyst("R01", classified=False, reason="not classified (over cap)")
    with session_scope(db_factory) as s:
        ks = m.KillSwitchEvent(
            run_id=w.run_id,
            switch="daily_loss_pct",
            session_date=REC_D,
            tripped_at=rec_et(10, 0),
            value=Decimal("-3.1"),
            threshold=Decimal("-3"),
        )
        s.add(ks)
        s.flush()
        ks_id = ks.id
    assert (await record_day(d, w.run_id, REC_D)).skipped is None

    def changed(fn: Callable[[Session], None]) -> Callable[[], Awaitable[str | None]]:
        async def run() -> str | None:
            with session_scope(db_factory) as s:
                fn(s)
            return (await record_day(d, w.run_id, REC_D)).skipped

        return run

    # rows updated in place are seen
    in_place = {
        "proposal approved": changed(
            lambda s: s.execute(
                update(m.Proposal)
                .where(m.Proposal.id == prop)
                .values(
                    status="approved",
                    decided_via="web",
                    decided_by="web:stephen",
                    decided_at=rec_et(9, 36),
                    decision_latency_ms=55_000,
                )
            )
        ),
        "catalyst classified": changed(
            lambda s: s.execute(
                update(m.Catalyst)
                .where(m.Catalyst.id == cat)
                .values(
                    classified_at=rec_et(9, 0),
                    catalyst_type="earnings_beat",
                    direction="bullish",
                    quality=70,
                    reason="Beat.",
                )
            )
        ),
        "kill switch reset": changed(
            lambda s: s.execute(
                update(m.KillSwitchEvent)
                .where(m.KillSwitchEvent.id == ks_id)
                .values(reset_at=rec_et(12, 0), reset_by="web:stephen", reset_reason="ok")
            )
        ),
    }
    for label, run in in_place.items():
        assert await run() is None, label
    assert [r.outcome for r in rows(db_factory, w.run_id, "approval")] == ["approved"]
    assert {r.outcome for r in rows(db_factory, w.run_id, "kill_switch")} == {"tripped", "reset"}

    # rows that aren't the day's sources change nothing
    other = await asyncio.to_thread(_other_run_activity, db_factory, w)
    for label, fn in {
        "a new strategy revision": lambda s: s.add(
            m.StrategyConfig(
                strategy_key="orb_sip",
                version="1.0.0",
                revision=2,
                params={"rvol_min": "3"},
                enabled=True,
                created_at=rec_et(12, 0),
                created_by="t",
            )
        ),
        "a decisions warning": lambda s: s.add(
            m.EventLog(
                ts=rec_et(12, 0), level="warning", source="decisions", run_id=w.run_id, message="x", data={}
            )
        ),
        "a worker heartbeat": lambda s: s.add(
            m.WorkerHeartbeat(
                process="worker",
                pid=1,
                host="h",
                started_at=rec_et(7, 0),
                beat_at=rec_et(12, 0),
                session_date=REC_D,
                phase="session",
                detail={},
            )
        ),
        "another run's journal": lambda s: add_decision(s, other, REC_D, 1, "day", "info"),
    }.items():
        assert await changed(fn)() == "unchanged", label

    # racing passes, one of them final: one consistent, final set
    results = await asyncio.gather(
        *(record_day(d, w.run_id, REC_D, final=f) for f in (False, True, False, True, False, False))
    )
    assert any(r.final for r in results)
    got = rows(db_factory, w.run_id)
    assert [r.seq for r in got] == list(range(1, len(got) + 1))
    assert [r.stage for r in got].count("day") == 1 and all(r.final for r in got)

    frozen = _journal(db_factory, w.run_id)
    buy = w.order("R01", side="buy", order_type="stop", purpose="entry", status="filled", stop="21.51")
    w.fill(buy, "21.53", at=rec_et(13, 0))
    with session_scope(db_factory) as s:  # a later config edit and a scan-detail change
        add_strategy_config(s, "orb_sip", revision=3, params={"rvol_min": "9"}, created_at=rec_et(14, 0))
    ranked = RuntimeSettings.model_validate({"reports.decisions_scan_detail": "ranked"})
    for kw in ({}, {"final": True}):
        assert (await record_day(d, w.run_id, REC_D, **kw)).skipped == "final"
        assert (await record_day(deps(db_factory, settings=ranked), w.run_id, REC_D, **kw)).skipped == "final"
    assert _journal(db_factory, w.run_id) == frozen


def _other_run_activity(factory: sessionmaker[Session], w: World) -> int:
    """Another (replay) run's candidates, orders and fills on the same day: not this run's sources."""
    with session_scope(factory) as s:
        other = add_run(s, mode="replay", status="running")
        cfg = w.orb_config
        sid = w.sym["R01"]
        s.add(
            m.Candidate(
                run_id=other,
                session_date=REC_D,
                strategy_key="orb_sip",
                symbol_id=sid,
                rvol=Decimal("5"),
                rank=1,
                candle={},
                passed=True,
                reject_reason=None,
                data={},
                created_at=SCAN_AT,
            )
        )
        o = m.Order(
            run_id=other,
            proposal_id=None,
            position_id=None,
            strategy_config_id=cfg,
            symbol_id=sid,
            side="buy",
            order_type="stop",
            purpose="entry",
            qty=1,
            stop_price=Decimal("21"),
            limit_price=None,
            stop_loss=None,
            tif="day",
            status="filled",
            reason="x",
            session_date=REC_D,
            submitted_at=SCAN_AT,
            closed_at=SCAN_AT,
            cancel_reason=None,
            stale_since=None,
            stale_alerted=False,
        )
        s.add(o)
        s.flush()
        s.add(
            m.Fill(
                run_id=other,
                order_id=o.id,
                ts=SCAN_AT,
                qty=1,
                price=Decimal("21"),
                fees={},
                quote_snapshot={},
                slippage=Decimal(0),
            )
        )
        s.add(
            m.EventLog(
                ts=SCAN_AT,
                level="info",
                source="strategy.orb_sip",
                run_id=other,
                message="orb: ranked",
                data={},
            )
        )
    return other


# ====================================================================================================
# PRUNE
# ====================================================================================================
def test_prune_uses_the_et_day_keeps_every_row_inside_retention_and_never_crosses_runs(
    db_factory: sessionmaker[Session],
) -> None:
    now = datetime(2027, 11, 11, 3, 30, tzinfo=UTC)  # 22:30 ET on 2027-11-10 (UTC is already the 11th)
    today = date(2027, 11, 10)
    cutoff = today - timedelta(days=400)
    with session_scope(db_factory) as s:
        live = add_run(s)
        ended_live = add_run(s, status="ended")
        old_replay = add_run(s, mode="replay", status="completed")
        fresh_replay = add_run(s, mode="replay", status="completed")
        running_replay = add_run(s, mode="replay", status="running")
        s.get(m.Run, old_replay).finished_at = now - timedelta(days=30, minutes=1)  # type: ignore[union-attr]
        s.get(m.Run, fresh_replay).finished_at = now - timedelta(days=30) + timedelta(minutes=1)  # type: ignore[union-attr]
        s.flush()
        add_decision(s, live, cutoff, 1, "day", "info")  # exactly 400 days: kept
        add_decision(s, live, cutoff - timedelta(days=1), 1, "day", "info")  # 401: deleted
        add_decision(s, live, today, 1, "day", "info")
        add_decision(s, ended_live, cutoff + timedelta(days=1), 1, "day", "info")  # an older live run: kept
        add_decision(s, old_replay, today, 1, "day", "info")  # recent sessions, old replay: deleted
        add_decision(
            s, fresh_replay, date(2019, 1, 2), 1, "day", "info"
        )  # ancient sessions, fresh replay: kept
        add_decision(s, running_replay, date(2019, 1, 3), 1, "day", "info")  # unfinished: kept
    before = dump(db_factory)
    result = prune(db_factory, FixedClock(now), RuntimeSettings())
    assert (result.live_deleted, result.replay_deleted) == (1, 1)
    with db_factory() as s:
        left = sorted((r.run_id, r.session_date) for r in s.execute(select(m.DecisionLog)).scalars())
    assert left == sorted(
        [
            (live, cutoff),
            (live, today),
            (ended_live, cutoff + timedelta(days=1)),
            (fresh_replay, date(2019, 1, 2)),
            (running_replay, date(2019, 1, 3)),
        ]
    )
    after = dump(db_factory)
    assert after == before, dump_diff(before, after)  # prune deletes only decision_log rows


# ====================================================================================================
# ISOLATION, API error bodies and the CSV formula guard
# ====================================================================================================
NOW_API = datetime(2026, 10, 7, 14, 0, tzinfo=UTC)
D1, D2 = date(2026, 10, 5), date(2026, 10, 6)
HOSTILE = ("=cmd|'/C calc'!A0", "+1+1", "-1+cmd", "@SUM(1+1)", "\t=1+1", "\r=1+1")
_PLAIN_NUMBER = re.compile(r"^-?\d+(\.\d+)?$")


def _api(factory: sessionmaker[Session]) -> Any:
    return make_client(
        make_services(test_core(factory, FixedClock(NOW_API))),
        decisions_router.router,
        raise_server_exceptions=False,
    )


def test_replay_rows_are_never_served_without_their_run_id_and_error_bodies_echo_nothing(
    db_factory: sessionmaker[Session],
) -> None:
    client = _api(db_factory)
    with session_scope(db_factory) as s:
        replay = add_run(s, mode="replay", status="running")
        for day in (D1, D2):
            add_decision(s, replay, day, 1, "scan", "passed", ticker="RPLY", data={"rvol": "9"})
            add_decision(s, replay, day, 2, "day", "info", data={"text": "replay day", "scanned": 1})
    # no live run at all: nothing is served by default
    assert client.get("/api/decisions", params={"date": D2.isoformat()}).status_code == 404
    assert client.get("/api/decisions/days").json() == {"days": []}
    assert client.get("/api/export/decisions.csv", params={"date": D2.isoformat()}).status_code == 404

    with session_scope(db_factory) as s:
        live = add_run(s)
        add_decision(s, live, D1, 1, "day", "info", data={"text": "live day", "scanned": 0})
    assert client.get("/api/decisions", params={"date": D2.isoformat()}).status_code == 404
    days = client.get("/api/decisions/days").json()["days"]
    assert [(x["run_id"], x["session_date"]) for x in days] == [(live, D1.isoformat())]
    body = client.get("/api/decisions", params={"date": D1.isoformat()}).json()
    assert (body["run_id"], body["run_mode"]) == (live, "live") and "RPLY" not in json.dumps(body)
    csv_text = client.get("/api/export/decisions.csv", params={"date": D1.isoformat()}).text
    assert "RPLY" not in csv_text
    explicit = client.get("/api/decisions", params={"date": D2.isoformat(), "run_id": replay}).json()
    assert (explicit["run_id"], explicit["run_mode"]) == (replay, "replay")
    assert {r["ticker"] for r in explicit["rows"]} == {"RPLY", None}

    probes: list[tuple[str, dict[str, Any], int]] = [
        ("/api/decisions", {"date": D1.isoformat(), "stage": "<script>x9q</script>"}, 422),
        ("/api/decisions", {"date": D1.isoformat(), "outcome": "=HYPERLINK(x9q)"}, 422),
        ("/api/decisions", {"date": D1.isoformat(), "ticker": "x9q" * 10}, 422),
        ("/api/decisions", {"date": "2026-13-45x9q"}, 422),
        ("/api/decisions", {"date": D1.isoformat(), "run_id": "x9q'--"}, 422),
        ("/api/decisions", {"date": D1.isoformat(), "limit": 5001}, 422),
        ("/api/decisions", {"date": D1.isoformat(), "offset": -1}, 422),
        ("/api/decisions/days", {"limit": 401}, 422),
        ("/api/export/decisions.csv", {"date": "x9q"}, 422),
        ("/api/decisions", {"date": D1.isoformat(), "run_id": 987654321}, 404),
        ("/api/decisions/days", {"run_id": 987654321}, 404),
        ("/api/export/decisions.csv", {"date": D1.isoformat(), "run_id": 987654321}, 404),
        ("/api/decisions", {"date": "2031-01-02"}, 404),
        # an offset past bigint: refused like run_id (le=2**63-1), not a database error (500)
        ("/api/decisions", {"date": D1.isoformat(), "offset": 10**19}, 422),
    ]
    for path, params, status in probes:
        r = client.get(path, params=params)
        assert r.status_code == status, (path, params, r.status_code, r.text)
        assert "x9q" not in r.text and "987654321" not in r.text and "2031" not in r.text, (path, r.text)


def test_every_text_cell_of_the_csv_is_formula_guarded(db_factory: sessionmaker[Session]) -> None:
    with session_scope(db_factory) as s:
        live = add_run(s)
        sid = add_symbol(s, "GUARD")
        a, b, c, d, e, f = HOSTILE
        add_decision(
            s,
            live,
            D2,
            1,
            "scan",
            "rejected",
            ticker=a[:20],
            symbol_id=sid,
            strategy_key=b,
            rule=c,
            reason=d,
            data={
                "checks": [{"name": e, "value": f, "op": ">=", "threshold": a, "passed": False}],
                "catalyst": {"type": b, "quality": c, "reason": d},
                "rvol": "=2+3",
                "rank": "@1",
                "or_high": "-1+1",
                "entry": "+5",
                "qty": "\t7",
            },
        )
        add_decision(
            s, live, D2, 2, "premarket", "classified", ticker=e, reason=f, data={"type": a, "quality": b}
        )
        add_decision(
            s, live, D2, 3, "approval", "approved", rule=d, data={"decided_via": c, "decision_latency_ms": e}
        )
        add_decision(
            s,
            live,
            D2,
            4,
            "fill",
            "filled",
            data={"planned_price": f, "fill_price": "-10.5", "diff_per_share": "-0.0200"},
        )
        add_decision(s, live, D2, 5, "exit", "exited", rule=b, reason=a, data={"pnl": "=1", "pnl_r": "-0.5"})
        add_decision(s, live, D2, 6, "scan", "rejected", data={"catalyst_type": e, "catalyst_reason": a})
    body = _api(db_factory).get("/api/export/decisions.csv", params={"date": D2.isoformat()}).text
    table = list(csv.reader(io.StringIO(body)))
    assert tuple(table[0]) == DECISION_CSV_COLUMNS and len(table) == 7
    for line in table[1:]:
        for col, cell in zip(DECISION_CSV_COLUMNS, line, strict=True):
            if cell.startswith(FORMULA_STARTS):
                assert _PLAIN_NUMBER.match(cell), (col, cell)
    fill = dict(zip(DECISION_CSV_COLUMNS, table[4], strict=True))
    assert (fill["fill_price"], fill["diff_per_share"]) == ("-10.5", "-0.0200")  # numbers stay numbers


# ====================================================================================================
# VOLUME and STREAMING: an ~850-name day recorded by the recorder
# ====================================================================================================
async def test_a_full_universe_day_is_about_a_megabyte_and_streams_through_a_server_side_cursor(
    db_factory: sessionmaker[Session],
) -> None:
    w = make_world(db_factory)
    with session_scope(db_factory) as s:
        syms = [
            m.Symbol(ticker=f"U{i:04d}", exchange="NYSE", questrade_id=None, currency="USD", name=None)
            for i in range(850)
        ]
        s.add_all(syms)
        s.flush()
        for i, sym in enumerate(syms):
            s.add(
                m.UniverseSnapshot(
                    session_date=REC_D,
                    symbol_id=sym.id,
                    price=Decimal("20"),
                    avg_volume=2_000_000,
                    atr14=Decimal("1"),
                    source="finviz",
                )
            )
            s.add(
                m.OpenBarStat(
                    symbol_id=sym.id, session_date=REC_D, avg_open_vol_14d=Decimal("1000"), atr14=Decimal("1")
                )
            )
            if i % 12:
                s.add(
                    m.IntradayCandle(
                        symbol_id=sym.id,
                        interval="5m",
                        ts=REC_OPEN,
                        open=Decimal("21"),
                        high=Decimal("21.5"),
                        low=Decimal("20.9"),
                        close=Decimal("21.4"),
                        volume=100 + i * 3,
                        vwap=None,
                    )
                )
            if i < 50:  # a busy pre-market: 50 flagged names with headlines and Claude's reasons
                s.add(
                    m.Catalyst(
                        symbol_id=sym.id,
                        session_date=REC_D,
                        headlines=[{"title": "h" * 200}] * 5,
                        gap_pct=Decimal("6.5"),
                        catalyst_type="earnings_beat",
                        direction="bullish",
                        quality=70,
                        confirmed=True,
                        reason="r" * 500,
                        model="claude",
                        cost_usd=Decimal("0.002"),
                        classified_at=rec_et(8, 1),
                        created_at=rec_et(8, 1),
                    )
                )
        w.sym.update({sym.ticker: sym.id for sym in syms})
    for rank, i in enumerate(range(849, 829, -1), start=1):
        w.candidate(f"U{i:04d}", rank, "3.0", reject=None if rank == 1 else "lower_rank")
    w.event("strategy.orb_sip", "orb: ranked", {"ranked": 20}, SCAN_AT)
    res = await record_day(deps(db_factory), w.run_id, REC_D, final=True)
    assert res.skipped is None and res.stages["scan"] == 851
    with db_factory() as s:
        n, size = s.execute(
            text(f"SELECT count(*), sum(pg_column_size(t.*)) FROM {SCHEMA}.decision_log t WHERE run_id = :r"),
            {"r": w.run_id},
        ).one()
    assert n > 900 and size <= 1_000_000, (n, size)  # the plan: <= ~1 MB per day

    engine = db_factory.kw["bind"]
    opened: list[Session] = []

    def factory() -> Session:
        sess = db_factory()
        opened.append(sess)
        return sess

    stream = close_when_done(decisions_csv(cast(sessionmaker[Session], factory), w.run_id, REC_D))
    header = await stream.__anext__()
    first = await stream.__anext__()
    assert header.startswith("run_id,") and first.startswith(f"{w.run_id},live,")
    (sess,) = opened
    cursors = sess.connection().exec_driver_sql("SELECT count(*) FROM pg_cursors").scalar_one()
    assert cursors >= 1, "the export must stream through a server-side cursor, not load the day"
    assert engine.pool.checkedout() == 1
    await stream.aclose()  # the client went away
    assert engine.pool.checkedout() == 0
