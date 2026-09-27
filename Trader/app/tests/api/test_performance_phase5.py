"""P5-T2 acceptance test 7: `/api/metrics` through `trader.reports.metrics` (the P4-T7 tests in
`tests/api/test_performance.py` keep passing), and the kill switch's expectancy agrees with it."""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from tests.api.conftest import make_client
from tests.factories import add_run, add_symbol
from tests.fakes_api import make_services, test_core
from tests.reports import add_trade
from trader.api.routers import performance
from trader.api.schemas import MetricsOut
from trader.broker.types import AccountState
from trader.db import models as m
from trader.engine.killswitch import KillSwitches
from trader.engine.runs import get_live_run
from trader.market.clock import FixedClock
from trader.reports import metrics
from trader.settings_store import RuntimeSettings

NOW = datetime(2026, 10, 9, 21, 0, tzinfo=UTC)  # Friday 17:00 ET
D1, D2, D3 = date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 7)


def _client(factory: sessionmaker[Session]) -> TestClient:
    return make_client(make_services(test_core(factory, FixedClock(NOW))), performance.router)


def _snapshot(run_id: int, ts: datetime, equity: str, peak: str, dd: str) -> m.EquitySnapshot:
    return m.EquitySnapshot(
        run_id=run_id,
        ts=ts,
        equity=Decimal(equity),
        cash=Decimal(equity),
        settled_cash=Decimal(equity),
        peak_equity=Decimal(peak),
        drawdown_pct=Decimal(dd),
    )


@pytest.mark.db
def test_replay_metrics_in_a_range_with_the_new_fields(db_factory: sessionmaker[Session]) -> None:
    live = get_live_run(db_factory, FixedClock(NOW), RuntimeSettings()).id
    with db_factory() as s:
        replay = add_run(s, mode="replay", status="completed")
        sym = add_symbol(s, "AAA")
        add_trade(s, replay, sym, D1, "50.0000", "5.0000", qty=10, slippage="0.1000", fees="1.0000")
        add_trade(s, replay, sym, D2, "20.0000", "2.0000", qty=10, slippage="0.3000", fees="1.0000")
        add_trade(s, replay, sym, D2, "-10.0000", "-1.0000", qty=30, slippage="0.1000", fees="0.5000")
        add_trade(s, replay, sym, D3, "-4.0000", None, qty=10, slippage="0.0000", fees="0.2500")
        add_trade(s, live, sym, D2, "-99.0000", "-9.0000")
        s.add(_snapshot(replay, datetime(2026, 10, 5, 20, 0, tzinfo=UTC), "12000", "12000", "0"))
        s.add(_snapshot(replay, datetime(2026, 10, 6, 20, 0, tzinfo=UTC), "10000", "12000", "0.1667"))
        s.add(_snapshot(replay, datetime(2026, 10, 7, 20, 0, tzinfo=UTC), "9500", "12000", "0.2083"))
        s.add(m.Journal(run_id=replay, session_date=D2, rules_followed=True))
        s.add(m.Journal(run_id=replay, session_date=D3, rules_followed=False))
        s.commit()
    body = _client(db_factory).get(
        "/api/metrics", params={"run": str(replay), "from": "2026-10-06", "to": "2026-10-07"}
    )
    assert body.status_code == 200, body.text
    out = body.json()
    assert (out["run_id"], out["from_date"], out["to_date"]) == (replay, "2026-10-06", "2026-10-07")
    assert (out["trades"], out["wins"], out["losses"], out["trades_without_r"]) == (3, 1, 2, 1)
    assert out["win_rate"] == "0.3333"
    assert out["expectancy_r"] == "0.5000"
    assert out["profit_factor"] == "1.4286"  # 20 / 14
    assert out["total_pnl"] == "6.0000"
    assert out["total_fees"] == "1.7500"
    assert out["avg_slippage"] == "0.1333"
    assert out["avg_slippage_per_share"] == "0.0080"  # 0.40 / 50
    assert out["max_drawdown_pct"] == "0.0500"  # the peak starts at 10000 inside the range
    assert out["adherence_pct"] == "0.5000"
    assert sum(b["count"] for b in out["r_histogram"]) == 2 and len(out["r_histogram"]) == 18
    again = MetricsOut.model_validate(out)
    assert again.total_fees == Decimal("1.75")


@pytest.mark.db
def test_route_helper_is_the_metrics_module(db_factory: sessionmaker[Session]) -> None:
    live = get_live_run(db_factory, FixedClock(NOW), RuntimeSettings()).id
    with db_factory() as s:
        sym = add_symbol(s, "AAA")
        add_trade(s, live, sym, D1, "20.0000", "2.0000")
        add_trade(s, live, sym, D2, "-10.0000", "-1.0000")
        s.commit()
    from trader.api.views import metrics_out

    expected = metrics_out(metrics.compute_metrics(db_factory, live, D1, D2))
    assert performance.compute_metrics(db_factory, live, D1, D2) == expected
    assert _client(db_factory).get(
        "/api/metrics", params={"from": "2026-10-05", "to": "2026-10-06"}
    ).json() == (expected.model_dump(mode="json"))
    for gone in ("_VIEW_SQL", "_RANGED_SQL", "_HISTOGRAM_SQL"):
        assert not hasattr(performance, gone), gone


@pytest.mark.db
def test_killswitch_expectancy_equals_the_metric(db_factory: sessionmaker[Session]) -> None:
    clock = FixedClock(NOW)
    live = get_live_run(db_factory, clock, RuntimeSettings()).id
    with db_factory() as s:
        sym = add_symbol(s, "AAA")
        for day, pnl, r in (
            (D1, "20.0000", "2.0000"),
            (D1, "-10.0000", "-1.0000"),
            (D2, "3.3300", "0.3333"),
            (D2, "-6.6700", "-0.6667"),
            (D3, "-3.0000", None),
            (D3, "0.0100", "0.0001"),
        ):
            add_trade(s, live, sym, day, pnl, r)
        s.commit()
    equity = Decimal("10000")
    account = AccountState(
        total_cash=equity, settled_cash=equity, buying_power=equity, positions_value=Decimal(0), equity=equity
    )
    inputs = KillSwitches(db_factory, clock).inputs(
        live, D3, account, datetime(2026, 10, 7, 13, 30, tzinfo=UTC)
    )
    mt = metrics.compute_metrics(db_factory, live)
    assert inputs.expectancy_r == mt.expectancy_r == Decimal("0.1333")
    assert inputs.closed_trades == mt.trades - mt.trades_without_r == 5
