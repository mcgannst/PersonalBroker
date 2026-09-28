"""P6-T10 acceptance test 14 (integration): decisions unchanged on a live day. The P5 simulated-day scenario
(`tests/integration/test_simulated_day.py` composition: nightly, pre-market, the 9:35 scan, auto approvals,
the entry fill, the protective stop, the overlay, the flatten) runs in two fresh databases, one with a
`record_day` pass after every event and fill and one without; the trading rows are identical by natural keys
(ids and wall times ignored), and the recorded journal describes the day."""

from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from tests.integration.test_simulated_day import CAL, DAY, QT, Day, et, opening, setup_day
from tests.replay.test_golden import fresh_factory  # noqa: F401  (a fixture: a second database)
from trader.db import models as m
from trader.decisions.recorder import LiveScanData, record_day
from trader.decisions.types import RecorderDeps
from trader.jobs.nightly import NightlyDeps, run_nightly
from trader.jobs.premarket import PremarketDeps, run_premarket

pytestmark = pytest.mark.db


async def simulate(factory: sessionmaker[Session], *, record: bool) -> tuple[int, int]:
    """The full simulated day; with `record`, a recorder pass after every event and fill. Returns (run id,
    number of passes)."""
    d: Day = setup_day(factory)
    passes = 0
    recorder = RecorderDeps(factory, d.clock, CAL, d.store.load, LiveScanData(factory, CAL))

    async def after() -> None:
        nonlocal passes
        if record:
            await record_day(recorder, d.run_id, DAY)
            passes += 1

    await run_nightly(NightlyDeps(d.factory, d.clock, CAL, d.finviz, d.fq, d.store.load()), DAY)
    d.clock.set(et(8, 0))
    d.fq.set_quote(QT["AAA"], "20.99", "21.01", "21.00", d.clock.now())
    d.fq.set_quote(QT["BBB"], "20.09", "20.11", "20.10", d.clock.now())
    await run_premarket(PremarketDeps(d.factory, d.clock, d.finviz, d.data, d.catalysts, d.store.load()), DAY)
    await after()

    d.clock.set(et(9, 35, 5))
    d.fq.add_bars(QT["AAA"], "FiveMinutes", [opening(DAY, "21.00", "21.50", "20.90", "21.40", 5000)])
    d.fq.add_bars(QT["BBB"], "FiveMinutes", [opening(DAY, "20.00", "20.40", "19.95", "20.30", 3000)])
    await d.engine.run_event("orb_open", DAY)
    await after()

    d.clock.set(et(9, 36))
    d.fq.set_quote(QT["AAA"], "21.52", "21.55", "21.53", d.clock.now())
    await d.engine.poll_quotes()
    await after()
    await d.engine.tick(d.clock.now())
    await after()

    d.clock.set(et(15, 30))
    d.fq.set_quote(QT["SPY"], "501.99", "502.01", "502.00", d.clock.now())
    d.fq.set_quote(QT["AAA"], "21.70", "21.72", "21.71", d.clock.now())
    await d.engine.run_event("overlay_decision", DAY)
    await after()

    d.clock.set(et(15, 50))
    await d.engine.run_event("flatten", DAY)
    await after()
    d.clock.set(et(15, 50, 2))
    d.fq.set_quote(QT["AAA"], "21.90", "21.92", "21.91", d.clock.now())
    await d.engine.poll_quotes()
    await after()

    d.clock.set(et(16, 0))
    await d.engine.end_of_session(DAY)
    if record:
        await record_day(recorder, d.run_id, DAY, final=True)
        passes += 1
    return d.run_id, passes


def _d(v: Any) -> Any:
    return format(v, "f") if isinstance(v, Decimal) else v


def trading_rows(factory: sessionmaker[Session]) -> dict[str, Any]:
    """Every trading table by natural keys: tickers instead of ids, no wall-clock columns that differ."""
    with factory() as s:
        tick = dict(s.execute(select(m.Symbol.id, m.Symbol.ticker)).all())
        sig_key = {
            x.id: (tick.get(x.symbol_id), x.event_key, x.ts.isoformat())
            for x in s.execute(select(m.Signal)).scalars()
        }
        out: dict[str, Any] = {
            "candidates": sorted(
                (
                    tick[c.symbol_id],
                    c.rank,
                    _d(c.rvol),
                    c.passed,
                    c.reject_reason,
                    repr(sorted(c.data.items())),
                )
                for c in s.execute(select(m.Candidate)).scalars()
            ),
            "signals": sorted(
                (*sig_key[x.id], repr(sorted(x.intent.items())), repr(sorted(x.evidence.items())))
                for x in s.execute(select(m.Signal)).scalars()
            ),
            "proposals": sorted(
                (
                    sig_key[p.signal_id],
                    p.kind,
                    p.qty,
                    p.status,
                    p.decided_via,
                    repr(
                        sorted(
                            (k, v) for k, v in p.order_spec.items() if k not in ("proposal_id", "position_id")
                        )
                    ),
                )
                for p in s.execute(select(m.Proposal)).scalars()
            ),
            "orders": sorted(
                (
                    tick[o.symbol_id],
                    o.side,
                    o.order_type,
                    o.purpose,
                    o.qty,
                    _d(o.stop_price),
                    o.status,
                    o.reason,
                    o.cancel_reason,
                    o.submitted_at.isoformat(),
                )
                for o in s.execute(select(m.Order)).scalars()
            ),
            "fills": sorted(
                (f.ts.isoformat(), f.qty, _d(f.price), _d(f.slippage), repr(sorted(f.fees.items())))
                for f in s.execute(select(m.Fill)).scalars()
            ),
            "trades": sorted(
                (
                    tick[t.symbol_id],
                    _d(t.entry_price),
                    _d(t.exit_price),
                    t.qty,
                    _d(t.pnl),
                    _d(t.pnl_r),
                    t.exit_reason,
                )
                for t in s.execute(select(m.Trade)).scalars()
            ),
            "ledger": sorted(
                (r.ts.isoformat(), r.kind, _d(r.amount), r.trade_date.isoformat(), r.settle_date.isoformat())
                for r in s.execute(select(m.CashLedger)).scalars()
            ),
            "equity": sorted(
                (e.ts.isoformat(), _d(e.equity), _d(e.cash), _d(e.settled_cash))
                for e in s.execute(select(m.EquitySnapshot)).scalars()
            ),
            "kill_switches": sorted(
                (k.switch, k.session_date.isoformat()) for k in s.execute(select(m.KillSwitchEvent)).scalars()
            ),
        }
    return out


async def test_14_a_live_day_is_identical_with_and_without_the_recorder(
    db_factory: sessionmaker[Session],
    fresh_factory: sessionmaker[Session],  # noqa: F811
) -> None:
    with_id, passes = await simulate(db_factory, record=True)
    without_id, none = await simulate(fresh_factory, record=False)
    assert passes == 8 and none == 0
    recorded, plain = trading_rows(db_factory), trading_rows(fresh_factory)
    assert recorded == plain
    assert recorded["trades"] and recorded["fills"] and recorded["candidates"]

    with db_factory() as s:
        journal = list(
            s.execute(
                select(m.DecisionLog).where(m.DecisionLog.run_id == with_id).order_by(m.DecisionLog.seq)
            ).scalars()
        )
    with fresh_factory() as s:
        assert s.execute(select(m.DecisionLog)).first() is None
    assert journal and all(r.final for r in journal)
    stages = [r.stage for r in journal]
    assert stages[0] == "universe" and stages[-1] == "day"
    (exit_row,) = [r for r in journal if r.stage == "exit"]
    assert (exit_row.ticker, exit_row.rule) == ("AAA", "flatten")
    scan = {r.ticker: r for r in journal if r.stage == "scan" and r.symbol_id is not None}
    assert scan["AAA"].outcome == "passed" and scan["BBB"].rule == "catalyst_missing"
    entry_fill = next(r for r in journal if r.stage == "fill" and r.data["purpose"] == "entry")
    # a buy stop at 21.51 filled at 21.5608: 0.0508 worse than planned
    assert Decimal(entry_fill.data["planned_price"]) == Decimal("21.51")
    assert Decimal(entry_fill.data["diff_per_share"]) == Decimal("0.0508")
    assert with_id and without_id
