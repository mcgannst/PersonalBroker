"""P4-T7 acceptance tests 1-5 and 8: `GET /api/metrics`, `GET /api/equity`, `GET /api/export/trades.csv`."""

import inspect
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert, text
from sqlalchemy.orm import Session, sessionmaker

from tests.api.conftest import make_client
from tests.factories import add_run, add_strategy_config, add_symbol
from tests.fakes_api import make_services, test_core
from tests.reports import add_trade
from trader.api.routers import performance
from trader.api.schemas import HistogramBinOut
from trader.db import models as m
from trader.engine.runs import get_live_run
from trader.market.clock import FixedClock
from trader.reports.export import TRADE_CSV_COLUMNS, trades_csv
from trader.settings_store import RuntimeSettings

NOW = datetime(2026, 10, 9, 21, 0, tzinfo=UTC)  # Friday 17:00 ET
D1, D2, D3, D4 = date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 7), date(2026, 10, 8)


def _client(factory: sessionmaker[Session]) -> TestClient:
    core = test_core(factory, FixedClock(NOW))
    return make_client(make_services(core), performance.router)


def _live(factory: sessionmaker[Session]) -> int:
    return get_live_run(factory, FixedClock(NOW), RuntimeSettings()).id


def _seed_four(factory: sessionmaker[Session], run_id: int) -> None:
    """+2R, -1R, +0.5R, -1R with P&L 20, -10, 5, -10 on four sessions."""
    with factory() as s:
        sym = add_symbol(s, "AAA")
        cfg = add_strategy_config(s)
        add_trade(s, run_id, sym, D1, "20.0000", "2.0000", config_id=cfg, slippage="0.0100")
        add_trade(s, run_id, sym, D2, "-10.0000", "-1.0000", config_id=cfg, slippage="0.0300")
        add_trade(s, run_id, sym, D3, "5.0000", "0.5000", config_id=cfg, slippage="0.0200")
        add_trade(s, run_id, sym, D4, "-10.0000", "-1.0000", config_id=cfg, slippage="0.0200")
        s.commit()


def _dec(body: dict[str, Any], key: str) -> Decimal | None:
    value = body[key]
    return None if value is None else Decimal(value)


def _bin_counts(body: dict[str, Any]) -> dict[tuple[str, str], int]:
    return {(b["lo"], b["hi"]): b["count"] for b in body["r_histogram"]}


# --- metrics -----------------------------------------------------------------------------------------------


@pytest.mark.db
def test_metrics_of_four_trades_match_the_view(db_factory: sessionmaker[Session]) -> None:
    run_id = _live(db_factory)
    _seed_four(db_factory, run_id)
    body = _client(db_factory).get("/api/metrics").json()
    assert body["run_id"] == run_id and body["from_date"] is None and body["to_date"] is None
    assert (body["trades"], body["wins"]) == (4, 2)
    assert _dec(body, "win_rate") == Decimal("0.5")
    assert _dec(body, "expectancy_r") == Decimal("0.125")
    assert _dec(body, "profit_factor") == Decimal("1.25")
    assert _dec(body, "total_pnl") == Decimal("5")
    assert _dec(body, "avg_win_r") == Decimal("1.25") and _dec(body, "avg_loss_r") == Decimal("-1")
    assert _dec(body, "avg_slippage") == Decimal("0.02")
    with db_factory() as s:
        view = s.execute(text("SELECT * FROM trader.v_trade_metrics WHERE run_id = :r"), {"r": run_id}).one()
    for key in ("win_rate", "avg_win_r", "avg_loss_r", "expectancy_r", "profit_factor", "avg_slippage"):
        assert _dec(body, key) == getattr(view, key), key
    assert body["trades"] == view.trades and body["wins"] == view.wins


@pytest.mark.db
def test_metrics_range_and_empty_range(db_factory: sessionmaker[Session]) -> None:
    run_id = _live(db_factory)
    _seed_four(db_factory, run_id)
    with db_factory() as s:
        s.add(m.Journal(run_id=run_id, session_date=D1, rules_followed=False, answered_via="telegram"))
        s.add(m.Journal(run_id=run_id, session_date=D2, rules_followed=True, answered_via="telegram"))
        s.add(m.Journal(run_id=run_id, session_date=D3, rules_followed=None))
        # P5-T2: the ranged drawdown is computed from the equity in range (running peak from the first
        # snapshot in range), so the snapshots carry consistent equity: in range 10000 then 9700 = 0.03, while
        # the whole run (peak 12000 on D1, outside the range) would give 0.1917.
        for day, equity, dd in ((D1, "12000", "0"), (D2, "10000", "0.1667"), (D4, "9700", "0.1917")):
            ts = datetime(day.year, day.month, day.day, 20, 30, tzinfo=UTC)
            s.add(_snapshot(run_id, ts, equity, dd))
        s.commit()
    client = _client(db_factory)
    body = client.get("/api/metrics", params={"from": "2026-10-06", "to": "2026-10-08"}).json()
    assert (body["from_date"], body["to_date"]) == ("2026-10-06", "2026-10-08")
    assert (body["trades"], body["wins"]) == (3, 1)
    assert _dec(body, "win_rate") == Decimal("0.3333")
    assert _dec(body, "expectancy_r") == Decimal("-0.5")
    assert _dec(body, "profit_factor") == Decimal("0.25")
    assert _dec(body, "total_pnl") == Decimal("-15")
    assert _dec(body, "adherence_pct") == Decimal("1")  # D2 followed; D3 unanswered; D1 outside
    assert _dec(body, "max_drawdown_pct") == Decimal("0.03")
    only_from = client.get("/api/metrics", params={"from": "2026-10-07"}).json()
    assert only_from["trades"] == 2 and only_from["to_date"] is None

    empty = client.get("/api/metrics", params={"from": "2026-11-02", "to": "2026-11-06"}).json()
    assert (empty["trades"], empty["wins"]) == (0, 0)
    for key in ("win_rate", "avg_win_r", "avg_loss_r", "expectancy_r", "profit_factor", "avg_slippage"):
        assert empty[key] is None, key
    assert empty["max_drawdown_pct"] is None and empty["adherence_pct"] is None
    assert _dec(empty, "total_pnl") == Decimal("0")
    assert all(b["count"] == 0 for b in empty["r_histogram"])


@pytest.mark.db
def test_metrics_reject_an_inverted_range(db_factory: sessionmaker[Session]) -> None:
    _live(db_factory)
    resp = _client(db_factory).get("/api/metrics", params={"from": "2026-10-08", "to": "2026-10-01"})
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "validation"


@pytest.mark.db
def test_metrics_r_histogram_bins(db_factory: sessionmaker[Session]) -> None:
    run_id = _live(db_factory)
    _seed_four(db_factory, run_id)
    with db_factory() as s:
        sym = add_symbol(s, "BBB")
        add_trade(s, run_id, sym, D4, "-70.0000", "-7.0000")
        add_trade(s, run_id, sym, D4, "50.0000", "5.0000")  # exactly +5R: the open-ended last bin
        add_trade(s, run_id, sym, D4, "-30.0000", "-3.0000")  # exactly -3R: [-3.0, -2.5)
        add_trade(s, run_id, sym, D4, "1.0000", None)  # no R: not counted
        s.commit()
    body = _client(db_factory).get("/api/metrics").json()
    bins = body["r_histogram"]
    assert len(bins) == 18
    assert (bins[0]["lo"], bins[0]["hi"]) == ("-Infinity", "-3.0")
    assert (bins[-1]["lo"], bins[-1]["hi"]) == ("5.0", "Infinity")
    for left, right in zip(bins, bins[1:], strict=False):
        assert left["hi"] == right["lo"]  # contiguous
    counts = _bin_counts(body)
    assert counts[("2.0", "2.5")] == 1
    assert counts[("-1.0", "-0.5")] == 2
    assert counts[("0.5", "1.0")] == 1
    assert counts[("-Infinity", "-3.0")] == 1
    assert counts[("5.0", "Infinity")] == 1
    assert counts[("-3.0", "-2.5")] == 1
    assert sum(counts.values()) == 7


def test_histogram_open_bins_wire_format() -> None:
    """The open-ended first and last bins carry the Decimal sentinels `-Infinity` / `Infinity`, which
    serialise as the strings the web's `histogramLabel` reads as `< hi` / `≥ lo` (non-finite bounds)."""
    bins = performance.histogram_bins({0: 1, 1: 2, 17: 3})
    assert bins[0].lo == Decimal("-Infinity") and bins[0].hi == Decimal("-3.0") and bins[0].count == 1
    assert bins[-1].lo == Decimal("5.0") and bins[-1].hi == Decimal("Infinity") and bins[-1].count == 3
    assert bins[1].model_dump(mode="json") == {"lo": "-3.0", "hi": "-2.5", "count": 2}
    assert bins[0].model_dump(mode="json") == {"lo": "-Infinity", "hi": "-3.0", "count": 1}
    assert bins[-1].model_dump(mode="json") == {"lo": "5.0", "hi": "Infinity", "count": 3}
    assert all(isinstance(b, HistogramBinOut) for b in bins)


@pytest.mark.db
def test_metrics_for_a_replay_run_and_unknown_run(db_factory: sessionmaker[Session]) -> None:
    live = _live(db_factory)
    with db_factory() as s:
        replay = add_run(s, mode="replay", status="completed")
        sym = add_symbol(s, "CCC")
        add_trade(s, replay, sym, D1, "30.0000", "3.0000")
        add_trade(s, live, sym, D1, "-10.0000", "-1.0000")
        s.commit()
    client = _client(db_factory)
    body = client.get("/api/metrics", params={"run": str(replay)}).json()
    assert body["run_id"] == replay and body["trades"] == 1 and _dec(body, "total_pnl") == Decimal("30")
    resp = client.get("/api/metrics", params={"run": "999999"})
    assert resp.status_code == 404 and resp.json()["error"]["code"] == "not_found"
    assert client.get("/api/equity", params={"run": "nope"}).status_code == 404
    assert client.get("/api/export/trades.csv", params={"run": "999999"}).status_code == 404


@pytest.mark.db
def test_performance_routes_need_a_session(db_factory: sessionmaker[Session]) -> None:
    services = make_services(test_core(db_factory, FixedClock(NOW)))
    client = make_client(services, performance.router, user=None)
    for path in ("/api/metrics", "/api/equity", "/api/export/trades.csv"):
        assert client.get(path).status_code == 401, path


# --- equity ------------------------------------------------------------------------------------------------


def _snapshot(run_id: int, ts: datetime, equity: str, dd: str = "0") -> m.EquitySnapshot:
    return m.EquitySnapshot(
        run_id=run_id,
        ts=ts,
        equity=Decimal(equity),
        cash=Decimal(equity),
        settled_cash=Decimal(equity),
        peak_equity=Decimal(equity),
        drawdown_pct=Decimal(dd),
    )


@pytest.mark.db
def test_equity_in_order_and_range(db_factory: sessionmaker[Session]) -> None:
    run_id = _live(db_factory)
    with db_factory() as s:
        other = add_run(s, mode="replay", status="completed")
        # 23:30 ET on D2 is 03:30 UTC on D3: it belongs to D2 (ranges use the ET date)
        s.add(_snapshot(run_id, datetime(2026, 10, 7, 3, 30, tzinfo=UTC), "10200"))
        s.add(_snapshot(run_id, datetime(2026, 10, 5, 20, 0, tzinfo=UTC), "10000"))
        s.add(_snapshot(run_id, datetime(2026, 10, 7, 20, 0, tzinfo=UTC), "10300", "0.0100"))
        s.add(_snapshot(other, datetime(2026, 10, 6, 20, 0, tzinfo=UTC), "1"))
        s.commit()
    client = _client(db_factory)
    body = client.get("/api/equity").json()
    assert body["run_id"] == run_id
    assert [p["equity"] for p in body["points"]] == ["10000.0000", "10200.0000", "10300.0000"]
    assert body["points"][0]["ts"] == "2026-10-05T20:00:00Z"
    assert body["points"][2]["drawdown_pct"] == "0.0100"
    ranged = client.get("/api/equity", params={"from": "2026-10-06", "to": "2026-10-06"}).json()
    assert [p["equity"] for p in ranged["points"]] == ["10200.0000"]


@pytest.mark.db
def test_equity_thins_to_5000_points_keeping_first_and_last(db_factory: sessionmaker[Session]) -> None:
    run_id = _live(db_factory)
    start = datetime(2026, 10, 1, 13, 30, tzinfo=UTC)
    rows = [
        {
            "run_id": run_id,
            "ts": start + timedelta(minutes=i),
            "equity": Decimal(10000 + i),
            "cash": Decimal(10000),
            "settled_cash": Decimal(10000),
            "peak_equity": Decimal(10000 + i),
            "drawdown_pct": Decimal(0),
        }
        for i in range(6000)
    ]
    with db_factory() as s:
        s.execute(insert(m.EquitySnapshot), rows)
        s.commit()
    points = _client(db_factory).get("/api/equity").json()["points"]
    assert 0 < len(points) <= 5000
    assert points[0]["equity"] == "10000.0000" and points[-1]["equity"] == "15999.0000"
    stamps = [p["ts"] for p in points]
    assert stamps == sorted(stamps) and len(set(stamps)) == len(stamps)


def test_thin_keeps_first_and_last() -> None:
    small = performance.thin(list(range(10)), 5)
    assert len(small) == 5 and small[0] == 0 and small[-1] == 9 and small == sorted(set(small))
    assert performance.thin([1, 2, 3], 5) == [1, 2, 3]
    thinned = performance.thin(list(range(6000)), 5000)
    assert len(thinned) == 5000 and thinned[0] == 0 and thinned[-1] == 5999
    assert len(set(thinned)) == 5000


# --- export ------------------------------------------------------------------------------------------------


@pytest.mark.db
def test_export_trades_csv(db_factory: sessionmaker[Session]) -> None:
    run_id = _live(db_factory)
    _seed_four(db_factory, run_id)
    with db_factory() as s:
        sym = add_symbol(s, "DDD")
        add_trade(s, run_id, sym, D3, "-1.0000", "-0.1000", exit_reason="=cmd()")
        other = add_run(s, mode="replay", status="completed")
        add_trade(s, other, sym, D3, "1.0000", "0.1000")
        s.commit()
    client = _client(db_factory)
    resp = client.get("/api/export/trades.csv", params={"from": "2026-10-06", "to": "2026-10-07"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert resp.headers["content-disposition"] == (
        f'attachment; filename="trades-{run_id}-2026-10-06-2026-10-07.csv"'
    )
    lines = resp.text.splitlines()
    assert lines[0] == ",".join(TRADE_CSV_COLUMNS)
    assert len(lines) == 1 + 3  # D2, D3 and the =cmd() trade; never the other run's
    assert any(line.split(",")[TRADE_CSV_COLUMNS.index("exit_reason")] == "'=cmd()" for line in lines[1:])
    whole = client.get("/api/export/trades.csv")
    assert whole.headers["content-disposition"] == f'attachment; filename="trades-{run_id}-all-all.csv"'
    assert len(whole.text.splitlines()) == 1 + 5


# --- fix round 1 ------------------------------------------------------------------------------------------


@pytest.mark.db
async def test_an_unfinished_export_is_closed_at_once(db_factory: sessionmaker[Session]) -> None:
    """A client that disconnects mid-export: the stream wrapper closes the generator, which closes its
    server-side cursor and returns the connection (the transaction ends) without waiting for GC."""
    run_id = _live(db_factory)
    _seed_four(db_factory, run_id)
    engine = db_factory.kw["bind"]
    lines = trades_csv(db_factory, run_id, None, None)
    stream = performance.close_when_done(lines)
    assert (await stream.__anext__()).startswith("trade_id,")
    await stream.__anext__()  # the first row: the cursor and its transaction are open
    assert engine.pool.checkedout() == 1
    await stream.aclose()
    assert inspect.getgeneratorstate(lines) == inspect.GEN_CLOSED
    assert engine.pool.checkedout() == 0
