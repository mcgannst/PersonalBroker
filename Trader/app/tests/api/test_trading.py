"""P4-T5 acceptance tests 7-9: the trading reads (`/api/candidates`, `/orders`, `/fills`, `/positions`,
`/positions/{id}`, `/trades`) on a real test database with seeded rows.

The seed helpers here (`seed_closed_trade`, `seed_open_position`, `live_run`) are also used by
`test_dashboard.py`.
"""

import asyncio
import time as time_module
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

from tests.api.conftest import make_client
from tests.factories import add_run, add_strategy_config, add_symbol
from tests.fakes_api import fake_candles, fake_quotes, make_services, test_core
from trader.api.deps import ApiServices
from trader.api.routers import dashboard, trading
from trader.db import models as m
from trader.engine.runs import get_live_run
from trader.market.clock import ET, FixedClock
from trader.market.types import Candle
from trader.notify import views
from trader.settings_store import RuntimeSettings

DAY = date(2026, 10, 6)  # a Tuesday session
NOW = datetime(2026, 10, 6, 14, 0, tzinfo=UTC)  # 10:00 ET


def at_et(d: date, hh: int, mm: int) -> datetime:
    return datetime.combine(d, time(hh, mm), tzinfo=ET).astimezone(UTC)


# --- seed helpers -------------------------------------------------------------------------------------------


def live_run(factory: sessionmaker[Session], clock: FixedClock) -> int:
    return get_live_run(factory, clock, RuntimeSettings()).id


def _config(s: Session, key: str) -> int:
    existing = s.query(m.StrategyConfig).filter_by(strategy_key=key).first()
    return existing.id if existing is not None else add_strategy_config(s, key)


def _signal(s: Session, run_id: int, cfg: int, sym: int, day: datetime | date, ts: datetime) -> int:
    sig = m.Signal(
        run_id=run_id,
        strategy_config_id=cfg,
        symbol_id=sym,
        session_date=day,
        event_key="orb_open",
        ts=ts,
        intent={"side": "buy", "reason": "opening range breakout"},
        evidence={"rvol": "3.2", "orb_high": "21.55"},
    )
    s.add(sig)
    s.flush()
    return sig.id


def _proposal(
    s: Session,
    run_id: int,
    sig: int,
    sym: int,
    *,
    kind: str,
    created_at: datetime,
    status: str = "approved",
    order_type: str = "stop",
    side: str = "buy",
    position_id: int | None = None,
    order_id: int | None = None,
    qty: int = 10,
) -> m.Proposal:
    p = m.Proposal(
        run_id=run_id,
        signal_id=sig,
        kind=kind,
        order_spec={
            "symbol_id": sym,
            "side": side,
            "order_type": order_type,
            "stop": "21.55",
            "stop_loss": "21.05",
            "reason": f"{kind} proposal",
        },
        qty=qty,
        status=status,
        created_at=created_at,
        expires_at=created_at + timedelta(seconds=90),
        decided_at=created_at + timedelta(seconds=12) if status != "pending" else None,
        decided_via="telegram" if status != "pending" else None,
        decided_by="telegram" if status != "pending" else None,
        decision_latency_ms=12000 if status != "pending" else None,
        position_id=position_id,
        order_id=order_id,
        sizing={"per_share_risk": "0.50"} if kind == "entry" else None,
        escalations=0,
    )
    s.add(p)
    s.flush()
    return p


def _order(
    s: Session,
    run_id: int,
    sym: int,
    *,
    purpose: str,
    side: str,
    order_type: str,
    status: str,
    day: date,
    submitted_at: datetime,
    proposal_id: int | None = None,
    position_id: int | None = None,
    stop_price: str | None = None,
    reason: str = "",
    qty: int = 10,
) -> m.Order:
    o = m.Order(
        run_id=run_id,
        proposal_id=proposal_id,
        position_id=position_id,
        symbol_id=sym,
        side=side,
        order_type=order_type,
        purpose=purpose,
        qty=qty,
        stop_price=Decimal(stop_price) if stop_price is not None else None,
        stop_loss=Decimal("21.05") if purpose == "entry" else None,
        tif="day",
        status=status,
        reason=reason or purpose,
        session_date=day,
        submitted_at=submitted_at,
        closed_at=submitted_at + timedelta(minutes=1) if status != "working" else None,
        stale_alerted=False,
    )
    s.add(o)
    s.flush()
    return o


def _fill(s: Session, run_id: int, order: m.Order, price: str, ts: datetime) -> int:
    f = m.Fill(
        run_id=run_id,
        order_id=order.id,
        ts=ts,
        qty=order.qty,
        price=Decimal(price),
        fees={"commission": "0.00", "total": "0.00"},
        quote_snapshot={"bid": price, "ask": price, "ts": ts.isoformat()},
        slippage=Decimal("0.0100"),
    )
    s.add(f)
    s.flush()
    return f.id


@dataclass(frozen=True)
class Chain:
    symbol_id: int
    signal_id: int
    position_id: int
    entry_proposal_id: int
    stop_proposal_id: int | None
    order_ids: tuple[int, ...]
    fill_ids: tuple[int, ...]
    trade_id: int | None


def seed_closed_trade(
    s: Session,
    run_id: int,
    *,
    day: date = DAY,
    ticker: str = "AAA",
    strategy: str = "orb_sip",
    pnl: str = "12.5000",
    symbol_id: int | None = None,
) -> Chain:
    """signal -> entry proposal -> entry order (filled) -> position -> stop proposal -> stop order (filled
    partially... here: cancelled after its fill is recorded) -> exit order (filled) -> trade."""
    sym = symbol_id if symbol_id is not None else add_symbol(s, ticker)
    cfg = _config(s, strategy)
    t0 = at_et(day, 9, 36)
    sig = _signal(s, run_id, cfg, sym, day, t0)
    entry_p = _proposal(s, run_id, sig, sym, kind="entry", created_at=t0)
    entry_o = _order(
        s, run_id, sym, purpose="entry", side="buy", order_type="stop", status="filled", day=day,
        submitted_at=t0 + timedelta(seconds=12), proposal_id=entry_p.id, stop_price="21.55",
    )  # fmt: skip
    entry_p.order_id = entry_o.id
    pos = m.Position(
        run_id=run_id,
        symbol_id=sym,
        strategy_config_id=cfg,
        qty=10,
        avg_price=Decimal("21.56"),
        stop_loss=Decimal("21.05"),
        planned_risk=Decimal("5.10"),
        session_date=day,
        opened_at=t0 + timedelta(minutes=1),
        closed_at=at_et(day, 15, 55),
        entry_order_id=entry_o.id,
        unprotected_seconds=7,
    )
    s.add(pos)
    s.flush()
    entry_o.position_id = pos.id
    entry_p.position_id = pos.id
    stop_p = _proposal(
        s, run_id, sig, sym, kind="stop", created_at=t0 + timedelta(minutes=1), side="sell",
        position_id=pos.id,
    )  # fmt: skip
    stop_o = _order(
        s, run_id, sym, purpose="stop", side="sell", order_type="stop", status="filled", day=day,
        submitted_at=t0 + timedelta(minutes=2), proposal_id=stop_p.id, position_id=pos.id,
        stop_price="21.05", qty=4,
    )  # fmt: skip
    stop_p.order_id = stop_o.id
    exit_o = _order(
        s, run_id, sym, purpose="exit", side="sell", order_type="market", status="filled", day=day,
        submitted_at=at_et(day, 15, 55), position_id=pos.id, reason="flatten_close", qty=6,
    )  # fmt: skip
    fills = (
        _fill(s, run_id, entry_o, "21.56", t0 + timedelta(minutes=1)),
        _fill(s, run_id, stop_o, "21.05", t0 + timedelta(minutes=30)),
        _fill(s, run_id, exit_o, "22.81", at_et(day, 15, 55)),
    )
    trade = m.Trade(
        run_id=run_id,
        position_id=pos.id,
        symbol_id=sym,
        session_date=day,
        entry_price=Decimal("21.56"),
        exit_price=Decimal("22.81"),
        qty=10,
        pnl=Decimal(pnl),
        pnl_r=Decimal("2.4500"),
        planned_risk=Decimal("5.10"),
        exit_reason="flatten_close",
        slippage_total=Decimal("0.2000"),
        fees_total=Decimal("0.0000"),
        opened_at=pos.opened_at,
        closed_at=at_et(day, 15, 55),
    )
    s.add(trade)
    s.flush()
    return Chain(sym, sig, pos.id, entry_p.id, stop_p.id, (entry_o.id, stop_o.id, exit_o.id), fills, trade.id)


def seed_open_position(
    s: Session,
    run_id: int,
    *,
    day: date = DAY,
    ticker: str = "BBB",
    working_stop: bool = True,
    opened_at: datetime | None = None,
) -> Chain:
    """An open position with a filled entry and (optionally) a working stop order."""
    sym = add_symbol(s, ticker)
    cfg = _config(s, "orb_sip")
    t0 = opened_at or at_et(day, 9, 36)
    sig = _signal(s, run_id, cfg, sym, day, t0)
    entry_p = _proposal(s, run_id, sig, sym, kind="entry", created_at=t0)
    entry_o = _order(
        s, run_id, sym, purpose="entry", side="buy", order_type="stop", status="filled", day=day,
        submitted_at=t0 + timedelta(seconds=12), proposal_id=entry_p.id, stop_price="21.55",
    )  # fmt: skip
    entry_p.order_id = entry_o.id
    pos = m.Position(
        run_id=run_id,
        symbol_id=sym,
        strategy_config_id=cfg,
        qty=10,
        avg_price=Decimal("20.00"),
        stop_loss=Decimal("19.50"),
        planned_risk=Decimal("5.00"),
        session_date=day,
        opened_at=t0 + timedelta(minutes=1),
        entry_order_id=entry_o.id,
        unprotected_seconds=4,
    )
    s.add(pos)
    s.flush()
    entry_o.position_id = pos.id
    entry_p.position_id = pos.id
    orders = [entry_o.id]
    if working_stop:
        stop_o = _order(
            s, run_id, sym, purpose="stop", side="sell", order_type="stop", status="working", day=day,
            submitted_at=t0 + timedelta(minutes=2), position_id=pos.id, stop_price="19.60",
        )  # fmt: skip
        orders.append(stop_o.id)
    fill = _fill(s, run_id, entry_o, "20.00", t0 + timedelta(minutes=1))
    return Chain(sym, sig, pos.id, entry_p.id, None, tuple(orders), (fill,), None)


def client_for(factory: sessionmaker[Session], clock: FixedClock, **overrides: Any) -> TestClient:
    services: ApiServices = make_services(test_core(factory, clock), **overrides)
    return make_client(services, dashboard.router, trading.router)


# --- auth ---------------------------------------------------------------------------------------------------


@pytest.mark.db
@pytest.mark.parametrize(
    "path",
    ["/api/candidates", "/api/orders", "/api/fills", "/api/positions", "/api/positions/1", "/api/trades"],
)
def test_every_read_needs_a_session(db_factory: sessionmaker[Session], path: str) -> None:
    services = make_services(test_core(db_factory, FixedClock(NOW)))
    client = make_client(services, trading.router, user=None)
    assert client.get(path).status_code == 401


# --- 7. candidates ------------------------------------------------------------------------------------------


@pytest.mark.db
def test_candidates_for_a_date(db_factory: sessionmaker[Session]) -> None:
    clock = FixedClock(NOW)
    run_id = live_run(db_factory, clock)
    with db_factory() as s:
        other_run = add_run(s, mode="replay")
        a, b, c, d = (add_symbol(s, t) for t in ("AAA", "BBB", "CCC", "DDD"))

        def cand(run: int, sym: int, rank: int | None, passed: bool, reason: str | None, key: str) -> None:
            s.add(
                m.Candidate(
                    run_id=run,
                    session_date=DAY,
                    strategy_key=key,
                    symbol_id=sym,
                    rvol=Decimal("3.1000"),
                    rank=rank,
                    candle={"open": "21.00", "close": "21.50"},
                    passed=passed,
                    reject_reason=reason,
                    data={"atr": "0.9"},
                    created_at=NOW,
                )
            )

        cand(run_id, c, None, False, "rvol_below_min", "orb_sip")
        cand(run_id, b, 2, True, None, "orb_sip")
        cand(run_id, a, 1, True, None, "orb_sip")
        cand(run_id, d, 3, False, "price_out_of_range", "orb_sip")
        cand(other_run, a, 1, True, None, "orb_sip")  # another run: never shown
        cand(run_id, a, 1, True, None, "other_day")
        s.execute(update(m.Candidate).where(m.Candidate.strategy_key == "other_day").values(
            session_date=date(2026, 10, 5)
        ))  # fmt: skip
        headlines = [
            {
                "ts": "2026-10-06T11:05:00+00:00",
                "title": "AAA beats",
                "source": "Reuters",
                "url": "https://x/a",
            },
            {"ts": None, "title": "AAA guidance", "source": None, "url": None},
        ]
        for sym, quality, hl in ((a, 4, headlines), (b, 9, [])):
            s.add(
                m.Catalyst(
                    symbol_id=sym,
                    session_date=DAY,
                    headlines=hl,
                    gap_pct=Decimal("5.2500"),
                    earnings_date=DAY,
                    catalyst_type="earnings",
                    direction="up",
                    quality=quality,
                    confirmed=True,
                    reason="beat and raise",
                    model="claude-x",
                    cost_usd=Decimal("0.001"),
                    classified_at=NOW,
                    created_at=NOW,
                )
            )
        s.add(
            m.JobRun(
                job="premarket",
                session_date=DAY,
                started_at=at_et(DAY, 8, 0),
                finished_at=at_et(DAY, 8, 2),
                status="succeeded",
                detail={"candidates": 4, "brief": "Pre-market brief: AAA, BBB"},
            )
        )
        s.commit()
    body = client_for(db_factory, clock).get("/api/candidates", params={"date": "2026-10-06"}).json()
    assert body["session_date"] == "2026-10-06"
    assert body["brief"] == "Pre-market brief: AAA, BBB"
    ranking = body["ranking"]
    assert [(r["ticker"], r["rank"]) for r in ranking] == [("AAA", 1), ("BBB", 2), ("DDD", 3), ("CCC", None)]
    assert ranking[2]["reject_reason"] == "price_out_of_range" and ranking[2]["passed"] is False
    assert ranking[0]["rvol"] == "3.1000" and ranking[0]["candle"] == {"open": "21.00", "close": "21.50"}
    catalysts = body["catalysts"]
    assert [c["ticker"] for c in catalysts] == ["BBB", "AAA"]  # highest quality first
    aaa = catalysts[1]
    assert aaa["catalyst_type"] == "earnings" and aaa["gap_pct"] == "5.2500" and aaa["quality"] == 4
    assert aaa["headlines"][0] == {
        "ts": "2026-10-06T11:05:00Z",
        "title": "AAA beats",
        "source": "Reuters",
        "url": "https://x/a",
    }
    assert aaa["headlines"][1] == {"ts": None, "title": "AAA guidance", "source": None, "url": None}


@pytest.mark.db
def test_candidates_default_to_the_current_session_and_may_be_empty(
    db_factory: sessionmaker[Session],
) -> None:
    clock = FixedClock(datetime(2026, 10, 10, 15, 0, tzinfo=UTC))  # Saturday -> Monday 2026-10-12
    body = client_for(db_factory, clock).get("/api/candidates").json()
    assert body == {"session_date": "2026-10-12", "brief": None, "catalysts": [], "ranking": []}


# --- 8. position detail -------------------------------------------------------------------------------------


def _archive(s: Session, sym: int, day: date, n: int) -> None:
    start = at_et(day, 9, 30)
    for i in range(n):
        s.add(
            m.CandleArchive(
                symbol_id=sym,
                interval="5m",
                start_ts=start + timedelta(minutes=5 * i),
                open=Decimal("21.00"),
                high=Decimal("21.60"),
                low=Decimal("20.90"),
                close=Decimal("21.50"),
                volume=1000 + i,
            )
        )
    # a 1m row and another day's 5m row are not part of the chart
    s.add(
        m.CandleArchive(
            symbol_id=sym, interval="1m", start_ts=start, open=Decimal(1), high=Decimal(1), low=Decimal(1),
            close=Decimal(1), volume=1,
        )
    )  # fmt: skip
    s.add(
        m.CandleArchive(
            symbol_id=sym, interval="5m", start_ts=start - timedelta(days=1), open=Decimal(1),
            high=Decimal(1), low=Decimal(1), close=Decimal(1), volume=1,
        )
    )  # fmt: skip


@pytest.mark.db
def test_position_detail_of_a_closed_trade(db_factory: sessionmaker[Session]) -> None:
    clock = FixedClock(datetime(2026, 10, 7, 14, 0, tzinfo=UTC))
    run_id = live_run(db_factory, clock)
    with db_factory() as s:
        chain = seed_closed_trade(s, run_id)
        _archive(s, chain.symbol_id, DAY, 78)
        other = add_run(s, mode="replay")
        foreign = seed_closed_trade(s, other, symbol_id=chain.symbol_id)
        s.commit()
    candles = fake_candles([])
    client = client_for(db_factory, clock, candles=candles)
    body = client.get(f"/api/positions/{chain.position_id}").json()
    pos = body["position"]
    assert pos["id"] == chain.position_id and pos["status"] == "closed" and pos["ticker"] == "AAA"
    assert pos["strategy_key"] == "orb_sip" and pos["last"] is None and pos["closed_at"] is not None
    assert pos["entry"] == "21.5600" and pos["unprotected_seconds"] == 7
    assert body["trade"]["id"] == chain.trade_id and body["trade"]["pnl"] == "12.5000"
    assert body["trade"]["exit_reason"] == "flatten_close" and body["trade"]["ticker"] == "AAA"
    assert body["signal"]["id"] == chain.signal_id
    assert body["signal"]["evidence"] == {"rvol": "3.2", "orb_high": "21.55"}
    assert body["signal"]["strategy_key"] == "orb_sip" and body["signal"]["config_revision"] == 1
    assert sorted(p["id"] for p in body["proposals"]) == sorted(
        [chain.entry_proposal_id, chain.stop_proposal_id or 0]
    )
    entry = next(p for p in body["proposals"] if p["kind"] == "entry")
    assert entry["risk_usd"] == "5.00" and entry["decided_via"] == "telegram"
    assert [o["id"] for o in body["orders"]] == list(chain.order_ids)
    assert [o["purpose"] for o in body["orders"]] == ["entry", "stop", "exit"]
    assert [f["id"] for f in body["fills"]] == list(chain.fill_ids)
    assert all(f["quote_snapshot"]["bid"] for f in body["fills"]) and body["fills"][2]["purpose"] == "exit"
    assert len(body["candles"]) == 78 and body["chart_error"] is None
    assert body["candles"][0]["start"] == "2026-10-06T13:30:00Z" and body["candles"][0]["volume"] == 1000
    assert candles.calls == []  # the archive had them
    assert client.get(f"/api/positions/{foreign.position_id}").status_code == 404
    assert client.get("/api/positions/999999").status_code == 404


@pytest.mark.db
def test_position_detail_candles_prefer_intraday_then_fall_back_to_the_source(
    db_factory: sessionmaker[Session],
) -> None:
    clock = FixedClock(NOW)
    run_id = live_run(db_factory, clock)
    with db_factory() as s:
        chain = seed_open_position(s, run_id)
        start = at_et(DAY, 9, 30)
        for i in range(3):
            s.add(
                m.IntradayCandle(
                    symbol_id=chain.symbol_id, interval="5m", ts=start + timedelta(minutes=5 * i),
                    open=Decimal("20"), high=Decimal("21"), low=Decimal("19"), close=Decimal("20.5"),
                    volume=10,
                )
            )  # fmt: skip
        other = seed_open_position(s, run_id, ticker="CCC")
        s.commit()
    source = fake_candles(
        [
            Candle(
                start, start + timedelta(minutes=5), Decimal("1"), Decimal("2"), Decimal("0.5"),
                Decimal("1.5"), 7, None,
            )
        ]
    )  # fmt: skip
    client = client_for(
        db_factory, clock, candles=source, quotes=fake_quotes({chain.symbol_id: Decimal("20.40")})
    )
    body = client.get(f"/api/positions/{chain.position_id}").json()
    assert len(body["candles"]) == 3 and source.calls == []
    pos = body["position"]
    assert pos["status"] == "open" and pos["last"] == "20.40" and pos["stop"] == "19.6000"
    assert pos["stop_working"] is True and pos["unrealized_pnl"] == "4.0000"
    assert body["trade"] is None
    # no stored candles: the CandleSource for the session's hours
    body = client.get(f"/api/positions/{other.position_id}").json()
    assert len(body["candles"]) == 1 and body["candles"][0]["close"] == "1.5"
    sym, begin, end = source.calls[0]
    assert sym == other.symbol_id and begin == at_et(DAY, 9, 30) and end == at_et(DAY, 16, 0)
    # a failing source gives chart_error, never a failed request
    source.error = TimeoutError("slow")
    body = client.get(f"/api/positions/{other.position_id}").json()
    assert body["candles"] == [] and body["chart_error"] == "TimeoutError"


# --- 9. trades, positions, orders, fills --------------------------------------------------------------------


@pytest.mark.db
def test_trades_filter_by_session_date_and_limit_bounds(db_factory: sessionmaker[Session]) -> None:
    clock = FixedClock(datetime(2026, 10, 9, 21, 0, tzinfo=UTC))
    run_id = live_run(db_factory, clock)
    with db_factory() as s:
        for i, day in enumerate((date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 7), date(2026, 10, 8))):
            seed_closed_trade(s, run_id, day=day, ticker=f"T{i}")
        other = add_run(s, mode="replay")
        replay_chain = seed_closed_trade(s, other, day=date(2026, 10, 6), ticker="RPL")
        s.commit()
    client = client_for(db_factory, clock)
    got = client.get("/api/trades", params={"from": "2026-10-06", "to": "2026-10-07"}).json()["items"]
    assert sorted(t["session_date"] for t in got) == ["2026-10-06", "2026-10-07"]
    assert {t["ticker"] for t in got} == {"T1", "T2"}
    all_live = client.get("/api/trades").json()["items"]
    assert len(all_live) == 4 and all_live[0]["session_date"] == "2026-10-08"  # newest first
    page = client.get("/api/trades", params={"limit": 2, "offset": 1}).json()["items"]
    assert [t["session_date"] for t in page] == ["2026-10-07", "2026-10-06"]
    replay = client.get("/api/trades", params={"run": str(other)}).json()["items"]
    assert [t["id"] for t in replay] == [replay_chain.trade_id]
    assert client.get("/api/trades", params={"run": "999999"}).status_code == 404
    assert client.get("/api/trades", params={"limit": 501}).status_code == 422
    assert client.get("/api/trades", params={"limit": 0}).status_code == 422
    assert client.get("/api/trades", params={"offset": -1}).status_code == 422
    assert client.get("/api/trades", params={"limit": 500}).status_code == 200


@pytest.mark.db
def test_positions_by_status(db_factory: sessionmaker[Session]) -> None:
    clock = FixedClock(NOW)
    run_id = live_run(db_factory, clock)
    with db_factory() as s:
        closed = seed_closed_trade(s, run_id, ticker="AAA")
        open_ = seed_open_position(s, run_id, ticker="BBB")
        s.commit()
    client = client_for(db_factory, clock, quotes=fake_quotes({open_.symbol_id: Decimal("20.50")}))
    closed_items = client.get("/api/positions", params={"status": "closed"}).json()["items"]
    assert [p["id"] for p in closed_items] == [closed.position_id]
    assert closed_items[0]["status"] == "closed" and closed_items[0]["last"] is None
    open_items = client.get("/api/positions").json()["items"]  # default: open
    assert [p["id"] for p in open_items] == [open_.position_id]
    assert open_items[0]["last"] == "20.50" and open_items[0]["unrealized_pnl"] == "5.0000"
    all_items = client.get("/api/positions", params={"status": "all"}).json()["items"]
    assert {p["id"] for p in all_items} == {closed.position_id, open_.position_id}
    other_day = client.get("/api/positions", params={"status": "all", "date": "2026-10-05"}).json()["items"]
    assert other_day == []
    assert client.get("/api/positions", params={"status": "bogus"}).status_code == 422


@pytest.mark.db
def test_orders_and_fills_for_a_date(db_factory: sessionmaker[Session]) -> None:
    clock = FixedClock(NOW)
    run_id = live_run(db_factory, clock)
    with db_factory() as s:
        today = seed_closed_trade(s, run_id, ticker="AAA")
        seed_closed_trade(s, run_id, ticker="BBB", day=date(2026, 10, 5))
        other = add_run(s, mode="replay")
        seed_closed_trade(s, other, ticker="RPL")
        s.commit()
    client = client_for(db_factory, clock)
    orders = client.get("/api/orders").json()["items"]  # default: the current session
    assert sorted(o["id"] for o in orders) == sorted(today.order_ids)
    assert {o["ticker"] for o in orders} == {"AAA"}
    entry = next(o for o in orders if o["purpose"] == "entry")
    assert entry["proposal_id"] == today.entry_proposal_id and entry["stop_price"] == "21.5500"
    assert entry["session_date"] == "2026-10-06" and entry["tif"] == "day"
    filled_exit = client.get("/api/orders", params={"status": "filled", "limit": 1}).json()["items"]
    assert len(filled_exit) == 1
    assert client.get("/api/orders", params={"status": "working"}).json()["items"] == []
    fills = client.get("/api/fills", params={"date": "2026-10-06"}).json()["items"]
    assert sorted(f["id"] for f in fills) == sorted(today.fill_ids)
    exit_fill = next(f for f in fills if f["purpose"] == "exit")
    assert exit_fill["ticker"] == "AAA" and exit_fill["side"] == "sell" and exit_fill["price"] == "22.8100"
    assert exit_fill["quote_snapshot"]["bid"] == "22.81" and exit_fill["slippage"] == "0.0100"
    assert client.get("/api/fills", params={"limit": 501}).status_code == 422
    assert client.get("/api/orders", params={"limit": 501}).status_code == 422


# --- fix round 1 ------------------------------------------------------------------------------------------


@pytest.mark.db
def test_offsets_and_ids_are_bounded_so_nothing_overflows_a_bigint(db_factory: sessionmaker[Session]) -> None:
    clock = FixedClock(NOW)
    live_run(db_factory, clock)
    client = client_for(db_factory, clock)
    assert client.get("/api/trades", params={"offset": trading.MAX_OFFSET}).status_code == 200
    for offset in (trading.MAX_OFFSET + 1, 2**63, -1):
        assert client.get("/api/trades", params={"offset": offset}).status_code == 422, offset
    for pid in (0, -1, 2**63, 10**20):
        assert client.get(f"/api/positions/{pid}").status_code == 422, pid
    assert client.get(f"/api/positions/{2**63 - 1}").status_code == 404


@pytest.mark.db
def test_run_takes_only_ascii_digits_within_a_bigint(db_factory: sessionmaker[Session]) -> None:
    clock = FixedClock(NOW)
    live_run(db_factory, clock)
    client = client_for(db_factory, clock)
    # superscript two, Arabic-Indic and fullwidth digits pass str.isdigit() but are not ids
    for bad in ("²", "١٢", "９", "0", str(2**63), "9" * 19, "+1", "1_0"):
        assert client.get("/api/trades", params={"run": bad}).status_code == 404, bad


@pytest.mark.db
def test_candle_fallback_is_bounded_by_a_timeout(
    db_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(trading, "CANDLE_TIMEOUT_SECONDS", 0.1)
    clock = FixedClock(NOW)
    run_id = live_run(db_factory, clock)
    with db_factory() as s:
        chain = seed_open_position(s, run_id)
        s.commit()

    async def hangs(symbol_id: int, start: datetime, end: datetime) -> list[Candle]:
        await asyncio.sleep(30)
        return []

    client = client_for(db_factory, clock, candles=hangs)
    t0 = time_module.monotonic()
    body = client.get(f"/api/positions/{chain.position_id}").json()
    assert time_module.monotonic() - t0 < 5
    assert body["candles"] == [] and body["chart_error"] == "TimeoutError"


@pytest.mark.db
def test_open_positions_read_the_database_off_the_event_loop(
    db_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FixedClock(NOW)
    run_id = live_run(db_factory, clock)
    with db_factory() as s:
        chain = seed_open_position(s, run_id)
        s.commit()
    on_loop: list[bool] = []
    real = views.open_position_rows

    def spy(factory: sessionmaker[Session], run: int) -> views.OpenPositionRows:
        try:
            asyncio.get_running_loop()
            on_loop.append(True)
        except RuntimeError:
            on_loop.append(False)
        return real(factory, run)

    monkeypatch.setattr(views, "open_position_rows", spy)
    quotes = fake_quotes({chain.symbol_id: Decimal("20.40")})
    client = client_for(db_factory, clock, quotes=quotes)
    for path in ("/api/positions", f"/api/positions/{chain.position_id}", "/api/dashboard"):
        assert client.get(path).status_code == 200, path
    assert on_loop == [False, False, False]
