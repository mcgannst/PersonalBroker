"""P6-T10 acceptance tests 3-8, 10, 11 and 17: the recorder over a seeded live day (testcontainers database).

`World` seeds one session's authoritative rows the way the engine and the jobs write them; `deps()` builds the
recorder over it with `LiveScanData`."""

import ast
import asyncio
import json
import time as wall_time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from tests.factories import add_run, add_strategy_config, add_symbol
from trader.db import models as m
from trader.db.session import session_scope
from trader.decisions import recorder
from trader.decisions.recorder import (
    EXIT_CATEGORIES,
    LiveScanData,
    cap_data,
    exit_category,
    lock_key,
    record_day,
    write_locked,
)
from trader.decisions.types import MAX_DATA_BYTES, RecorderDeps
from trader.market.calendar import SessionCalendar
from trader.market.clock import ET, FixedClock
from trader.settings_store import RuntimeSettings

pytestmark = pytest.mark.db

CAL = SessionCalendar()
D = date(2026, 10, 6)  # a Tuesday
OPEN = CAL.session_open(D)
SCAN_AT = OPEN + timedelta(minutes=5, seconds=5)
APP_DIR = Path(__file__).resolve().parents[2]


def et(hh: int, mm: int, ss: int = 0, day: date = D) -> datetime:
    return datetime.combine(day, time(hh, mm, ss), tzinfo=ET).astimezone(UTC)


def candle_json(o: str, h: str, lo: str, c: str, v: int) -> dict[str, Any]:
    return {"start": OPEN.isoformat(), "open": o, "high": h, "low": lo, "close": c, "volume": v}


@dataclass
class World:
    factory: sessionmaker[Session]
    run_id: int = 0
    orb_config: int = 0
    overlay_config: int = 0
    sym: dict[str, int] = field(default_factory=dict)

    # --- basic rows ---------------------------------------------------------------------------------------
    def base(self, *, orb_params: dict[str, Any] | None = None) -> "World":
        with session_scope(self.factory) as s:
            self.run_id = add_run(s)
            self.orb_config = add_strategy_config(
                s, "orb_sip", params=orb_params or {}, created_at=datetime(2026, 10, 1, tzinfo=UTC)
            )
            self.overlay_config = add_strategy_config(
                s, "spy_overlay", created_at=datetime(2026, 10, 1, tzinfo=UTC)
            )
        return self

    def symbol(self, ticker: str) -> int:
        if ticker not in self.sym:
            with session_scope(self.factory) as s:
                self.sym[ticker] = add_symbol(s, ticker)
        return self.sym[ticker]

    def member(
        self,
        ticker: str,
        *,
        bar: tuple[str, str, str, str, int] | None = None,
        avg_open: str | None = "1000",
        atr: str | None = "1.00",
        avg_volume: int | None = 2_000_000,
        price: str = "20",
    ) -> int:
        sid = self.symbol(ticker)
        with session_scope(self.factory) as s:
            s.add(
                m.UniverseSnapshot(
                    session_date=D,
                    symbol_id=sid,
                    price=Decimal(price),
                    avg_volume=avg_volume,
                    atr14=Decimal(atr) if atr else None,
                    source="finviz",
                )
            )
            if avg_open is not None or atr is not None:
                s.add(
                    m.OpenBarStat(
                        symbol_id=sid,
                        session_date=D,
                        avg_open_vol_14d=Decimal(avg_open) if avg_open else None,
                        atr14=Decimal(atr) if atr else None,
                    )
                )
            if bar is not None:
                o, h, lo, c, v = bar
                s.add(
                    m.IntradayCandle(
                        symbol_id=sid,
                        interval="5m",
                        ts=OPEN,
                        open=Decimal(o),
                        high=Decimal(h),
                        low=Decimal(lo),
                        close=Decimal(c),
                        volume=v,
                        vwap=None,
                    )
                )
        return sid

    def job(self, job: str, detail: dict[str, Any], at: datetime, *, status: str = "succeeded") -> int:
        with session_scope(self.factory) as s:
            row = m.JobRun(
                job=job,
                session_date=D,
                started_at=at,
                finished_at=at + timedelta(seconds=30),
                status=status,
                error=None,
                detail=detail,
            )
            s.add(row)
            s.flush()
            return row.id

    def event(
        self, source: str, message: str, data: dict[str, Any], at: datetime, level: str = "info"
    ) -> int:
        with session_scope(self.factory) as s:
            row = m.EventLog(
                ts=at, level=level, source=source, run_id=self.run_id, message=message, data=data
            )
            s.add(row)
            s.flush()
            return row.id

    def candidate(
        self,
        ticker: str,
        rank: int,
        rvol: str,
        *,
        reject: str | None,
        bar: tuple[str, str, str, str, int] = ("21.00", "21.50", "20.90", "21.40", 5000),
        at: datetime = SCAN_AT,
        extra: dict[str, Any] | None = None,
    ) -> int:
        sid = self.symbol(ticker)
        o, h, lo, c, v = bar
        data = {
            "ticker": ticker,
            "rvol": rvol,
            "rank": rank,
            "direction": "bullish",
            "atr14": "1.00",
            "atr_source": "open_bar_stats",
            "price": c,
            "avg_volume": 2_000_000,
            "avg_open_vol_14d": "1000",
            "universe_source": "finviz",
            **(extra or {}),
        }
        with session_scope(self.factory) as s:
            row = m.Candidate(
                run_id=self.run_id,
                session_date=D,
                strategy_key="orb_sip",
                symbol_id=sid,
                rvol=Decimal(rvol),
                rank=rank,
                candle=candle_json(o, h, lo, c, v),
                passed=reject is None,
                reject_reason=reject,
                data=data,
                created_at=at,
            )
            s.add(row)
            s.flush()
            return row.id

    def signal(
        self,
        ticker: str,
        *,
        evidence: dict[str, Any],
        intent: dict[str, Any] | None = None,
        at: datetime = SCAN_AT,
    ) -> int:
        sid = self.symbol(ticker)
        with session_scope(self.factory) as s:
            row = m.Signal(
                run_id=self.run_id,
                strategy_config_id=self.orb_config,
                symbol_id=sid,
                session_date=D,
                event_key="orb_open",
                ts=at,
                intent=intent
                or {
                    "type": "enter_long",
                    "symbol_id": sid,
                    "order_type": "stop",
                    "stop": "10.2500",
                    "limit": None,
                    "stop_loss": "10.1500",
                    "reason": "orb_breakout",
                },
                evidence=evidence,
            )
            s.add(row)
            s.flush()
            return row.id

    def proposal(
        self,
        signal_id: int,
        ticker: str,
        *,
        status: str,
        kind: str = "entry",
        qty: int = 10,
        via: str | None = None,
        by: str | None = None,
        latency: int | None = None,
        error: str | None = None,
        expired: bool = False,
        at: datetime = SCAN_AT,
        order_type: str = "stop",
        stop: str | None = "10.2500",
    ) -> int:
        sid = self.symbol(ticker)
        spec = {
            "symbol_id": sid,
            "side": "buy" if kind == "entry" else "sell",
            "order_type": order_type,
            "qty": qty,
            "stop": stop,
            "limit": None,
            "tif": "day",
            "purpose": kind if kind != "cancel" else "exit",
            "position_id": None,
            "proposal_id": None,
            "strategy_config_id": self.orb_config,
            "stop_loss": "10.1500" if kind == "entry" else None,
            "reason": "orb_breakout",
        }
        sizing = (
            {
                "equity": "720",
                "risk_pct": "0.02",
                "risk_dollars": "14.40",
                "entry": "10.2500",
                "slippage_buffer": "0.005",
                "shares_risk": "144",
                "shares_cash": "70",
                "limited_by": "cash",
            }
            if kind == "entry"
            else None
        )
        decided_at = at + timedelta(milliseconds=latency) if latency is not None else (at if via else None)
        with session_scope(self.factory) as s:
            row = m.Proposal(
                run_id=self.run_id,
                signal_id=signal_id,
                kind=kind,
                order_spec=spec,
                qty=qty,
                status=status,
                created_at=at,
                expires_at=at + timedelta(minutes=5),
                decided_at=decided_at,
                decided_via=via,
                decided_by=by,
                decision_latency_ms=latency,
                sizing=sizing,
                expired_at=at + timedelta(minutes=5) if expired else None,
                escalations=0,
                error=error,
            )
            s.add(row)
            s.flush()
            return row.id

    def order(
        self,
        ticker: str,
        *,
        side: str,
        order_type: str,
        purpose: str,
        status: str,
        stop: str | None = None,
        limit: str | None = None,
        qty: int = 10,
        proposal_id: int | None = None,
        position_id: int | None = None,
        reason: str = "orb_breakout",
        cancel_reason: str | None = None,
        at: datetime = SCAN_AT,
    ) -> int:
        with session_scope(self.factory) as s:
            row = m.Order(
                run_id=self.run_id,
                proposal_id=proposal_id,
                position_id=position_id,
                strategy_config_id=self.orb_config,
                symbol_id=self.symbol(ticker),
                side=side,
                order_type=order_type,
                purpose=purpose,
                qty=qty,
                stop_price=Decimal(stop) if stop else None,
                limit_price=Decimal(limit) if limit else None,
                stop_loss=None,
                tif="day",
                status=status,
                reason=reason,
                session_date=D,
                submitted_at=at,
                closed_at=None if status == "working" else at + timedelta(minutes=1),
                cancel_reason=cancel_reason,
                stale_since=None,
                stale_alerted=False,
            )
            s.add(row)
            s.flush()
            return row.id

    def fill(
        self,
        order_id: int,
        price: str,
        *,
        qty: int = 10,
        snapshot: dict[str, Any] | None = None,
        at: datetime,
    ) -> int:
        with session_scope(self.factory) as s:
            row = m.Fill(
                run_id=self.run_id,
                order_id=order_id,
                ts=at,
                qty=qty,
                price=Decimal(price),
                fees={"commission": "0", "ecn": "0", "sec": "0", "total": "0"},
                quote_snapshot=snapshot
                or {"bid": "10.26", "ask": "10.27", "last": "10.27", "time": at.isoformat()},
                slippage=Decimal("0.0100"),
            )
            s.add(row)
            s.flush()
            return row.id

    def trade(
        self,
        ticker: str,
        *,
        exit_reason: str,
        pnl: str,
        pnl_r: str | None,
        opened: datetime,
        closed: datetime,
    ) -> int:
        sid = self.symbol(ticker)
        with session_scope(self.factory) as s:
            pos = m.Position(
                run_id=self.run_id,
                symbol_id=sid,
                strategy_config_id=self.orb_config,
                qty=0,
                avg_price=Decimal("10.27"),
                stop_loss=Decimal("10.15"),
                planned_risk=Decimal("1.2"),
                session_date=D,
                opened_at=opened,
                closed_at=closed,
                entry_order_id=1,
                stop_order_id=None,
                unprotected_since=None,
                unprotected_seconds=3,
            )
            s.add(pos)
            s.flush()
            row = m.Trade(
                run_id=self.run_id,
                position_id=pos.id,
                symbol_id=sid,
                session_date=D,
                entry_price=Decimal("10.27"),
                exit_price=Decimal("10.49"),
                qty=10,
                pnl=Decimal(pnl),
                pnl_r=Decimal(pnl_r) if pnl_r else None,
                planned_risk=Decimal("1.2"),
                exit_reason=exit_reason,
                slippage_total=Decimal("0.2"),
                fees_total=Decimal("0.01"),
                opened_at=opened,
                closed_at=closed,
            )
            s.add(row)
            s.flush()
            return row.id

    def catalyst(
        self,
        ticker: str,
        *,
        classified: bool = True,
        reason: str | None = "Beat and raise.",
        gap: str | None = None,
        quality: int | None = 80,
        headlines: list[Any] | None = None,
        ctype: str = "earnings_beat",
        direction: str = "bullish",
    ) -> int:
        sid = self.symbol(ticker)
        at = et(8, 1)
        with session_scope(self.factory) as s:
            row = m.Catalyst(
                symbol_id=sid,
                session_date=D,
                headlines=headlines or [],
                gap_pct=Decimal(gap) if gap else None,
                catalyst_type=ctype if classified else "unknown",
                direction=direction if classified else "neutral",
                quality=quality if classified else None,
                confirmed=classified,
                reason=reason,
                model="claude-sonnet-5" if classified else None,
                cost_usd=Decimal("0.002"),
                classified_at=at if classified else None,
                created_at=at,
            )
            s.add(row)
            s.flush()
            return row.id


def make_world(factory: sessionmaker[Session], **kw: Any) -> World:
    return World(factory).base(**kw)


def deps(
    factory: sessionmaker[Session],
    *,
    settings: RuntimeSettings | None = None,
    scan: bool = True,
    now: datetime | None = None,
) -> RecorderDeps:
    s = settings or RuntimeSettings()
    return RecorderDeps(
        factory=factory,
        clock=FixedClock(now or et(16, 30)),
        calendar=CAL,
        settings=lambda: s,
        scan_data=LiveScanData(factory, CAL) if scan else None,
    )


def rows(factory: sessionmaker[Session], run_id: int, stage: str | None = None) -> list[m.DecisionLog]:
    with factory() as s:
        q = select(m.DecisionLog).where(m.DecisionLog.run_id == run_id).order_by(m.DecisionLog.seq)
        if stage is not None:
            q = q.where(m.DecisionLog.stage == stage)
        return list(s.execute(q).scalars())


def scan_world(factory: sessionmaker[Session], **kw: Any) -> World:
    """The 9:35 scan of test 4: 20 ranked candidates, 5 more names at or above rvol_min, one below it, one
    without a baseline, one without a stored bar (the fetch timed out), and SPY."""
    w = make_world(factory, **kw)
    names = [f"R{i:02d}" for i in range(1, 26)]  # rvol 5.00, 4.90, ... in this order
    for i, t in enumerate(names):
        vol = 5000 - i * 100
        w.member(t, bar=("21.00", "21.50", "20.90", "21.40", vol))
        if i < 20:
            w.candidate(t, i + 1, f"{Decimal(vol) / 1000:.4f}", reject=None if i == 0 else "lower_rank")
    w.member("LOW", bar=("21.00", "21.50", "20.90", "21.40", 800))
    w.member("NOBASE", bar=("21.00", "21.50", "20.90", "21.40", 5000), avg_open=None, atr=None)
    nobar = w.member("NOBAR")
    w.member("SPY", bar=("500", "501", "499", "500.5", 500_000), avg_open="1000000", price="500")
    w.event(
        "strategy.orb_sip",
        "orb: 1 symbols have no opening bar",
        {"missing": {str(nobar): "timeout"}},
        SCAN_AT,
        "warning",
    )
    w.event("strategy.orb_sip", "orb: ranked", {"ranked": 20, "selected": ["R01"]}, SCAN_AT)
    w.job("event:orb_open", {}, SCAN_AT - timedelta(seconds=1))
    return w


# --- 3: thresholds from the revision in effect -----------------------------------------------------------
async def test_3_thresholds_come_from_the_revision_in_effect(db_factory: sessionmaker[Session]) -> None:
    w = scan_world(db_factory)
    with session_scope(db_factory) as s:  # 10:00 ET: rvol_min 1.00 -> 2.00 (a new revision)
        add_strategy_config(s, "orb_sip", revision=2, params={"rvol_min": "2.00"}, created_at=et(10, 0))
    res = await record_day(deps(db_factory), w.run_id, D, rebuild=True)
    assert res.skipped is None
    (summary,) = [r for r in rows(db_factory, w.run_id, "scan") if r.symbol_id is None]
    assert summary.data["params"]["rvol_min"] == "1.00"
    assert summary.data["params_in_effect"]["revision"] == 1
    low = next(r for r in rows(db_factory, w.run_id, "scan") if r.ticker == "LOW")
    assert low.data["checks"][0]["threshold"] == "1.00"
    ranked = next(r for r in rows(db_factory, w.run_id, "scan") if r.ticker == "R02")
    assert {c["name"]: c["threshold"] for c in ranked.data["checks"]}["rvol"] == "1.00"


# --- 4: non-candidates -----------------------------------------------------------------------------------
async def test_4_every_universe_name_with_its_reason(db_factory: sessionmaker[Session]) -> None:
    w = scan_world(db_factory)
    await record_day(deps(db_factory), w.run_id, D)
    scan = [r for r in rows(db_factory, w.run_id, "scan") if r.symbol_id is not None]
    by = {r.ticker: r for r in scan}
    assert "SPY" not in by  # the overlay symbol never appears
    assert (by["NOBAR"].rule, by["NOBAR"].reason) == ("no_opening_bar", "timeout")
    assert by["NOBASE"].rule == "no_baseline"
    low = by["LOW"]
    assert low.rule == "rvol_below_min" and low.data["checks"] == [
        {"name": "rvol", "value": "0.8000", "op": ">=", "threshold": "1.00", "passed": False}
    ]
    outside = sorted((r.data["rank"], r.ticker) for r in scan if r.rule == "outside_top_n")
    assert outside == [(21, "R21"), (22, "R22"), (23, "R23"), (24, "R24"), (25, "R25")]
    assert by["R01"].outcome == "passed" and by["R02"].rule == "lower_rank"
    assert len(scan) == 28
    (summary,) = [r for r in rows(db_factory, w.run_id, "scan") if r.symbol_id is None]
    counts = summary.data["counts"]
    assert (counts["scanned"], counts["rvol_passed"], counts["ranked"], counts["passed"]) == (28, 25, 20, 1)
    assert counts["rejects_by_rule"] == {
        "lower_rank": 19,
        "no_baseline": 1,
        "no_opening_bar": 1,
        "outside_top_n": 5,
        "rvol_below_min": 1,
    }


async def test_4_ranked_detail_writes_only_candidates_and_the_counts(
    db_factory: sessionmaker[Session],
) -> None:
    w = scan_world(db_factory)
    settings = RuntimeSettings.model_validate({"reports.decisions_scan_detail": "ranked"})
    await record_day(deps(db_factory, settings=settings), w.run_id, D)
    scan = rows(db_factory, w.run_id, "scan")
    assert len([r for r in scan if r.symbol_id is not None]) == 20
    (summary,) = [r for r in scan if r.symbol_id is None]
    assert summary.data["counts"]["scanned"] == 28
    assert summary.data["counts"]["rejects_by_rule"]["outside_top_n"] == 5


async def test_4_without_scan_data_only_the_ranked_names_and_a_note(
    db_factory: sessionmaker[Session],
) -> None:
    w = scan_world(db_factory)
    await record_day(deps(db_factory, scan=False), w.run_id, D)
    scan = rows(db_factory, w.run_id, "scan")
    assert len([r for r in scan if r.symbol_id is not None]) == 20
    (day,) = rows(db_factory, w.run_id, "day")
    assert any("no scan data" in n for n in day.data["notes"])


async def test_a_skipped_scan_is_one_rejected_summary_row(db_factory: sessionmaker[Session]) -> None:
    w = make_world(db_factory)
    w.member("AAA", bar=("21.00", "21.50", "20.90", "21.40", 5000))
    w.event("strategy.orb_sip", "orb: max_positions already used today", {"entries_today": 1}, SCAN_AT)
    await record_day(deps(db_factory), w.run_id, D)
    (scan,) = rows(db_factory, w.run_id, "scan")
    assert (scan.outcome, scan.rule, scan.symbol_id) == ("rejected", "skipped:max_positions", None)


# --- 5: pre-market ---------------------------------------------------------------------------------------
async def test_5_premarket_sources_reasons_and_masking(db_factory: sessionmaker[Session]) -> None:
    w = make_world(db_factory)
    w.job(
        "premarket",
        {
            "screens": [
                {"label": "news", "filter": "news_date_today", "rows": 3, "matched": ["AAA", "BBB"]},
                {"label": "earnings", "filter": "earningsdate_today", "rows": 1, "matched": ["AAA"]},
            ],
            "screen_errors": ["earnings2: HTTP 503"],
            "quote_error": None,
            "budget_hit": ["DDD"],
            "over_cap": ["CCC"],
            "candidates": 4,
            "classified": 2,
        },
        et(8, 0),
    )
    long_reason = "Strong beat with token=abc123secret and raised guidance. " + "x" * 700
    w.catalyst(
        "AAA",
        gap="0.05",
        reason=long_reason,
        headlines=[
            {"title": "AAA beats; see Authorization: Bearer abcdef1234567890", "source": "Reuters"},
            {"title": "two"},
            {"title": "three"},
            {"title": "four"},
        ],
    )
    w.catalyst("BBB", gap="0.01", quality=40)
    w.catalyst("CCC", classified=False, reason="not classified (over cap)", gap="0.04")
    w.catalyst("DDD", classified=False, reason="daily budget US$1 reached")
    w.catalyst("EEE", classified=False, reason="classification failed: timeout")
    await record_day(deps(db_factory), w.run_id, D)
    pm = rows(db_factory, w.run_id, "premarket")
    summary = pm[0]
    assert summary.symbol_id is None and summary.data["screens"][0]["matched"] == 2
    assert summary.data["over_cap"] == 1 and summary.data["budget_hit"] == ["DDD"]
    by = {r.ticker: r for r in pm if r.symbol_id is not None}
    aaa = by["AAA"]
    assert aaa.outcome == "classified" and aaa.data["sources"] == ["earnings", "news", "gap"]
    assert aaa.data["gap_min_pct"] == "0.03"
    assert len(aaa.reason or "") == 500 and "abc123secret" not in (aaa.reason or "")
    assert len(aaa.data["catalyst_reason"]) == 500 and "abc123secret" not in aaa.data["catalyst_reason"]
    assert aaa.data["headlines"] == 4 and len(aaa.data["headline_titles"]) == 3
    assert "abcdef1234567890" not in json.dumps(aaa.data)
    assert by["BBB"].data["sources"] == ["news"]  # a 1% gap is below the 3% threshold
    assert (by["CCC"].outcome, by["CCC"].rule) == ("listed", "over_cap")
    assert by["CCC"].data["sources"] == ["gap"]
    assert (by["DDD"].outcome, by["DDD"].rule) == ("listed", "budget")
    assert (by["EEE"].outcome, by["EEE"].rule) == ("listed", "unclassified")


# --- 6: signals to approvals ------------------------------------------------------------------------------
async def test_6_signals_risk_proposals_and_approvals(db_factory: sessionmaker[Session]) -> None:
    w = make_world(db_factory)
    s1 = w.signal("AAA", evidence={"rvol": "3.2", "candle": {"high": "10.24"}})
    p1 = w.proposal(s1, "AAA", status="submitted", via="telegram", by="telegram:1", latency=42_000)
    sizing = {
        "equity": "720",
        "risk_pct": "0.02",
        "shares": "0",
        "per_share_risk": "0.10",
        "limited_by": "cash",
    }
    w.signal(
        "BBB",
        evidence={"rejection": {"check": "zero_shares", "reason": "rounds to zero", "detail": sizing}},
    )
    s3 = w.signal("CCC", evidence={})
    w.proposal(s3, "CCC", status="submitted", via="auto", by="auto", latency=0)
    s4 = w.signal("DDD", evidence={})
    w.proposal(s4, "DDD", status="rejected", via="web", by="web:stephen", latency=9_000)
    s5 = w.signal("EEE", evidence={})
    w.proposal(s5, "EEE", status="expired", expired=True)
    s6 = w.signal("FFF", evidence={})
    w.proposal(
        s6,
        "FFF",
        status="rejected",
        via="telegram",
        by="telegram:1",
        latency=5_000,
        error="entry blocked: kill switch daily_loss_pct is tripped",
    )
    w.signal("GGG", evidence={"error": {"type": "RuntimeError", "message": "boom password=hunter2"}})
    await record_day(deps(db_factory), w.run_id, D)

    sig = {r.ticker: r for r in rows(db_factory, w.run_id, "signal")}
    assert sig["AAA"].outcome == "proposed" and "candle" not in sig["AAA"].data["evidence"]
    assert (sig["BBB"].outcome, sig["BBB"].rule) == ("rejected", "zero_shares")
    assert sig["GGG"].outcome == "error" and "hunter2" not in (sig["GGG"].reason or "")
    (risk,) = rows(db_factory, w.run_id, "risk")
    assert (risk.ticker, risk.rule, risk.reason) == ("BBB", "zero_shares", "rounds to zero")
    assert risk.data["sizing"]["limited_by"] == "cash"

    prop = {r.ticker: r for r in rows(db_factory, w.run_id, "proposal")}
    a = prop["AAA"].data
    assert (a["entry"], a["stop_loss"], a["target"]) == ("10.2500", "10.1500", None)
    assert (a["r_per_share"], a["risk_dollars"], a["est_cost"]) == ("0.1000", "1.0000", "102.5000")
    assert a["est_cost_buffered"] == "103.0125" and a["est_fees"] == "0.0000"
    assert a["sizing"]["limited_by"] == "cash"

    appr = {r.ticker: r for r in rows(db_factory, w.run_id, "approval")}
    assert (appr["AAA"].outcome, appr["AAA"].data["decided_via"]) == ("approved", "telegram")
    assert appr["AAA"].data["decision_latency_ms"] == 42_000
    assert appr["CCC"].outcome == "auto_approved"
    assert appr["DDD"].outcome == "declined"
    assert appr["EEE"].outcome == "expired"
    assert (appr["FFF"].outcome, appr["FFF"].rule) == ("blocked", "daily_loss_pct")
    assert p1 > 0


# --- 7: fills and exits -----------------------------------------------------------------------------------
async def test_7_fill_differences_and_exit_categories(db_factory: sessionmaker[Session]) -> None:
    w = make_world(db_factory)
    buy = w.order("AAA", side="buy", order_type="stop", purpose="entry", status="filled", stop="10.25")
    w.fill(buy, "10.27", at=SCAN_AT + timedelta(seconds=40))
    sell_stop = w.order("AAA", side="sell", order_type="stop", purpose="stop", status="filled", stop="9.80")
    w.fill(sell_stop, "9.70", at=SCAN_AT + timedelta(minutes=30))
    mkt = w.order(
        "BBB", side="sell", order_type="market", purpose="exit", status="filled", reason="flatten_close"
    )
    w.fill(
        mkt,
        "10.49",
        snapshot={"bid": "10.50", "ask": "10.52", "last": "10.51", "time": et(15, 50).isoformat()},
        at=et(15, 50, 2),
    )
    w.order(
        "CCC",
        side="buy",
        order_type="stop",
        purpose="entry",
        status="cancelled",
        stop="5",
        cancel_reason="entry_cancel_at",
    )
    w.trade("AAA", exit_reason="protective_stop", pnl="-5.70", pnl_r="-1.1", opened=SCAN_AT, closed=et(10, 5))
    w.trade("BBB", exit_reason="flatten_close", pnl="2.20", pnl_r="0.8", opened=SCAN_AT, closed=et(15, 50, 2))
    await record_day(deps(db_factory), w.run_id, D)

    fills = rows(db_factory, w.run_id, "fill")
    keys = ("planned_price", "fill_price", "diff_per_share")
    assert [tuple(Decimal(f.data[k]) for k in keys) for f in fills] == [
        (Decimal("10.25"), Decimal("10.27"), Decimal("0.02")),  # a buy stop filled 2 cents above: worse
        (Decimal("9.80"), Decimal("9.70"), Decimal("0.10")),  # a sell stop gapped through: worse
        (Decimal("10.50"), Decimal("10.49"), Decimal("0.01")),  # a market sell from the snapshot's bid
    ]
    assert (
        Decimal(fills[0].data["diff_total"]) == Decimal("0.20")
        and fills[2].data["planned_source"] == "quote_bid"
    )
    orders = {o.ref["order_id"]: o for o in rows(db_factory, w.run_id, "order")}
    cancelled = next(o for o in orders.values() if o.ticker == "CCC")
    assert (cancelled.outcome, cancelled.rule) == ("cancelled", "entry_cancel_at")
    exits = rows(db_factory, w.run_id, "exit")
    assert [(e.ticker, e.rule, e.reason) for e in exits] == [
        ("AAA", "stop", "protective_stop"),
        ("BBB", "flatten", "flatten_close"),
    ]
    assert exits[0].data["minutes_held"] == "29.9" and exits[0].data["unprotected_seconds"] == 3


def _exit_reason_literals() -> set[str]:
    """Every exit reason string on trunk: the reason argument of each `Exit(...)` in trader/, and the replay's
    forced close."""
    found: set[str] = set()
    for path in (APP_DIR / "trader").rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Exit":
                if len(node.args) >= 4 and isinstance(node.args[3], ast.Constant):
                    found.add(str(node.args[3].value))
    from trader.replay.runner import FORCED_CLOSE

    found.add(FORCED_CLOSE)
    return found


def test_7_the_exit_mapping_covers_every_reason_on_trunk() -> None:
    reasons = _exit_reason_literals()
    assert reasons >= {"protective_stop", "flatten_close", "overlay_negative", "replay_forced_close"}
    assert reasons <= EXIT_CATEGORIES.keys(), reasons - EXIT_CATEGORIES.keys()
    assert {r: exit_category(r) for r in sorted(reasons)} == {
        "flatten_close": "flatten",
        "overlay_negative": "overlay",
        "protective_stop": "stop",
        "replay_forced_close": "replay_close",
    }
    assert exit_category("flatten_close", expiry=True) == "expiry"
    assert exit_category("kill_switch_flatten") == "kill_switch"
    assert exit_category("something_new") == "other"


async def test_7_an_expiry_auto_flatten_is_its_own_category(db_factory: sessionmaker[Session]) -> None:
    w = make_world(db_factory)
    sig = w.signal(
        "AAA", evidence={}, intent={"type": "exit", "order_type": "market", "reason": "flatten_close"}
    )
    p = w.proposal(
        sig,
        "AAA",
        kind="exit",
        status="submitted",
        via="auto",
        by="auto_flatten_on_expiry",
        expired=True,
        order_type="market",
        stop=None,
    )
    tid = w.trade("AAA", exit_reason="flatten_close", pnl="1", pnl_r="0.1", opened=SCAN_AT, closed=et(15, 56))
    with db_factory() as s:
        pos_id = s.get(m.Trade, tid).position_id  # type: ignore[union-attr]
    w.order(
        "AAA",
        side="sell",
        order_type="market",
        purpose="exit",
        status="filled",
        proposal_id=p,
        position_id=pos_id,
        reason="flatten_close",
        at=et(15, 55),
    )
    await record_day(deps(db_factory), w.run_id, D)
    (ex,) = rows(db_factory, w.run_id, "exit")
    assert ex.rule == "expiry"
    (appr,) = rows(db_factory, w.run_id, "approval")
    assert (appr.outcome, appr.rule) == ("expired", "auto_executed_on_expiry")


# --- 8: kill switches and overlay -------------------------------------------------------------------------
async def test_8_kill_switch_trips_resets_and_overlay_decisions(db_factory: sessionmaker[Session]) -> None:
    w = make_world(db_factory)
    with session_scope(db_factory) as s:
        s.add(
            m.KillSwitchEvent(
                run_id=w.run_id,
                switch="daily_loss_pct",
                session_date=D,
                tripped_at=et(11, 0),
                value=Decimal("-0.021"),
                threshold=Decimal("0.02"),
                reset_at=et(12, 0),
                reset_reason="checked it, secret=abc123 fine",
                reset_by="web:stephen",
            )
        )
    w.event(
        "strategy.spy_overlay",
        "overlay: decision",
        {"decision": "exit", "spy_return": "-0.002000", "benchmark": "SPY", "position_ids": [1]},
        et(15, 30),
    )
    w.event("strategy.spy_overlay", "overlay: something else", {"note": "no decision"}, et(15, 30))
    await record_day(deps(db_factory), w.run_id, D)
    ks = rows(db_factory, w.run_id, "kill_switch")
    assert [(k.outcome, k.rule) for k in ks] == [("tripped", "daily_loss_pct"), ("reset", "daily_loss_pct")]
    assert ks[0].data["value"] == "-0.021000" and ks[0].data["threshold"] == "0.020000"
    assert "abc123" not in (ks[1].reason or "")
    (ov,) = rows(db_factory, w.run_id, "overlay")
    assert (ov.rule, ov.ticker, ov.data["spy_return"]) == ("exit", "SPY", "-0.002000")


# --- 10: passes --------------------------------------------------------------------------------------------
def _snapshot(factory: sessionmaker[Session], run_id: int) -> list[tuple[Any, ...]]:
    with factory() as s:
        return [
            tuple(r)
            for r in s.execute(
                select(m.DecisionLog.__table__)
                .where(m.DecisionLog.run_id == run_id)
                .order_by(m.DecisionLog.seq)
            ).all()
        ]


async def test_10_pass_semantics(db_factory: sessionmaker[Session]) -> None:
    w = scan_world(db_factory)
    buy = w.order("R01", side="buy", order_type="stop", purpose="entry", status="working", stop="21.51")
    d = deps(db_factory)
    first = await record_day(d, w.run_id, D)
    assert first.skipped is None and first.stages["scan"] == 29 and first.stages["day"] == 1
    before = _snapshot(db_factory, w.run_id)
    again = await record_day(deps(db_factory, now=et(17, 0)), w.run_id, D)
    assert again.skipped == "unchanged" and _snapshot(db_factory, w.run_id) == before

    # an unrelated event (the log mirror, an alert) changes nothing
    w.event("log.worker", "a log line", {}, SCAN_AT)
    with session_scope(db_factory) as s:
        s.add(m.EventLog(ts=SCAN_AT, level="error", source="engine", run_id=None, message="x", data={}))
    assert (await record_day(d, w.run_id, D)).skipped == "unchanged"

    # a new fill does
    w.fill(buy, "21.53", at=SCAN_AT + timedelta(minutes=1))
    rebuilt = await record_day(d, w.run_id, D)
    assert rebuilt.skipped is None and rebuilt.stages["fill"] == 1
    # an order status change does too (a row updated in place)
    with session_scope(db_factory) as s:
        s.get(m.Order, buy).status = "filled"  # type: ignore[union-attr]
    assert (await record_day(d, w.run_id, D)).skipped is None

    final = await record_day(d, w.run_id, D, final=True)
    assert final.skipped is None and final.final
    assert all(r.final for r in rows(db_factory, w.run_id))
    frozen = _snapshot(db_factory, w.run_id)
    w.fill(
        w.order("R02", side="buy", order_type="stop", purpose="entry", status="filled", stop="21"),
        "21",
        at=et(11, 0),
    )
    assert (await record_day(d, w.run_id, D)).skipped == "final"
    assert (await record_day(d, w.run_id, D, final=True)).skipped == "final"
    assert _snapshot(db_factory, w.run_id) == frozen
    forced = await record_day(d, w.run_id, D, rebuild=True, final=True)
    assert forced.skipped is None and forced.stages["fill"] == 2


async def test_10_disabled_and_not_session(db_factory: sessionmaker[Session]) -> None:
    w = make_world(db_factory)
    off = RuntimeSettings.model_validate({"reports.decisions_enabled": False})
    assert (await record_day(deps(db_factory, settings=off), w.run_id, D)).skipped == "disabled"
    assert (await record_day(deps(db_factory), w.run_id, date(2026, 10, 3))).skipped == "not_session"
    assert rows(db_factory, w.run_id) == []


async def test_10_concurrent_passes_leave_one_consistent_set(db_factory: sessionmaker[Session]) -> None:
    w = scan_world(db_factory)
    d = deps(db_factory)
    results = await asyncio.gather(*(record_day(d, w.run_id, D, rebuild=True) for _ in range(4)))
    assert all(r.skipped is None for r in results)
    seqs = [r.seq for r in rows(db_factory, w.run_id)]
    assert seqs == list(range(1, len(seqs) + 1))
    assert len([r for r in rows(db_factory, w.run_id) if r.stage == "day"]) == 1


async def test_10_a_pass_blocked_by_the_final_pass_returns_final(db_factory: sessionmaker[Session]) -> None:
    """A final=False pass that starts before, and blocks on the lock held by, a final=True pass returns
    `final` afterwards and leaves every row final (the check is made under the lock)."""
    w = scan_world(db_factory)
    d = deps(db_factory)
    await record_day(d, w.run_id, D)  # a first, non-final version exists
    w.fill(
        w.order("R01", side="buy", order_type="stop", purpose="entry", status="filled", stop="21.51"),
        "21.53",
        at=et(9, 40),
    )

    holder = db_factory()
    holder.begin()
    holder.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": lock_key(w.run_id, D)})
    task = asyncio.create_task(record_day(d, w.run_id, D))  # peeks "changed, not final", then waits
    for _ in range(200):
        await asyncio.sleep(0.05)
        with db_factory() as s:
            waiting = s.execute(
                text("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted")
            ).scalar_one()
        if waiting:
            break
    assert waiting and not task.done()
    # the post-close's final pass commits while the other pass waits
    done = write_locked(
        holder, d, RuntimeSettings(), w.run_id, D, final=True, rebuild=False, detail="all", fp=None, scan=None
    )
    assert done.skipped is None and done.final
    holder.commit()
    holder.close()
    result = await asyncio.wait_for(task, 10)
    assert result.skipped == "final"
    assert rows(db_factory, w.run_id) and all(r.final for r in rows(db_factory, w.run_id))


async def test_a_rebuild_of_a_final_day_keeps_it_final(db_factory: sessionmaker[Session]) -> None:
    """Fix round 1 nit: `rebuild=True` with `final=False` (e.g. `trader decisions record --rebuild`) rebuilds
    a frozen day but never un-freezes it."""
    w = scan_world(db_factory)
    d = deps(db_factory)
    assert (await record_day(d, w.run_id, D, final=True)).final
    res = await record_day(d, w.run_id, D, rebuild=True)
    assert res.skipped is None and res.final
    assert all(r.final for r in rows(db_factory, w.run_id))
    assert (await record_day(d, w.run_id, D)).skipped == "final"


async def test_the_fingerprint_covers_the_settings_the_rows_use_and_the_watchlist(
    db_factory: sessionmaker[Session],
) -> None:
    """Fix round 1 nit: the pre-market gap threshold, the cost buffer, the fee settings, the FinViz filters
    and the day's manual watchlist are shown on rows, so a change to any of them rebuilds a day not yet
    final."""
    w = scan_world(db_factory)
    base: dict[str, Any] = {}
    assert (await record_day(deps(db_factory), w.run_id, D)).skipped is None
    for key, value in (
        ("premarket.gap_min_pct", "0.05"),
        ("slippage_buffer", "0.01"),
        ("fees.ecn_per_share", "0.0040"),
        ("universe.finviz_filters", "ind_stocksonly,geo_usa"),
    ):
        base[key] = value
        changed = deps(db_factory, settings=RuntimeSettings.model_validate(dict(base)))
        assert (await record_day(changed, w.run_id, D)).skipped is None, key
        assert (await record_day(changed, w.run_id, D)).skipped == "unchanged", key
    same = deps(db_factory, settings=RuntimeSettings.model_validate(dict(base)))
    with session_scope(db_factory) as s:
        s.add(
            m.ManualWatchlist(
                session_date=D,
                tickers=["R01"],
                filename="mine.csv",
                uploaded_at=et(8, 0),
                uploaded_by="stephen",
            )
        )
    assert (await record_day(same, w.run_id, D)).skipped is None
    (universe,) = rows(db_factory, w.run_id, "universe")
    assert universe.data["watchlist"] == "mine.csv"
    assert universe.data["finviz_filters"] == "ind_stocksonly,geo_usa"


# --- 11: masking and caps ---------------------------------------------------------------------------------
def test_11_cap_data_truncates_long_lists_with_a_marker() -> None:
    big = {"notes": [{"message": f"note {i} " + "y" * 100} for i in range(200)], "count": 200, "token": "abc"}
    out = cap_data(big)
    assert len(json.dumps(out, sort_keys=True).encode()) <= MAX_DATA_BYTES
    assert out["truncated"] > 0 and len(out["notes"]) + out["truncated"] == 200
    assert out["count"] == 200 and out["token"] == "[REDACTED]"
    huge = {"blob": "z" * 20_000}
    capped = cap_data(huge)
    assert len(json.dumps(capped).encode()) <= MAX_DATA_BYTES and capped["truncated"] == 1


async def test_11_errors_and_reasons_are_masked_and_capped(db_factory: sessionmaker[Session]) -> None:
    w = make_world(db_factory)
    w.signal(
        "AAA",
        evidence={
            "error": {"type": "HTTPError", "message": "GET https://api/?refresh_token=SECRET123 " + "e" * 900}
        },
    )
    w.catalyst("AAA", reason='{"api_key": "sk-ant-XYZ"} ' + "r" * 900)
    await record_day(deps(db_factory), w.run_id, D)
    (sig,) = rows(db_factory, w.run_id, "signal")
    assert "SECRET123" not in (sig.reason or "") and len(sig.reason or "") == 500
    assert "SECRET123" not in json.dumps(sig.data)
    (pm,) = rows(db_factory, w.run_id, "premarket")
    assert "sk-ant-XYZ" not in (pm.reason or "") and "sk-ant-XYZ" not in json.dumps(pm.data)
    for r in rows(db_factory, w.run_id):
        assert len(json.dumps(r.data).encode()) <= MAX_DATA_BYTES or r.stage == "day"


# --- 17: performance ---------------------------------------------------------------------------------------
async def test_17_a_1000_symbol_day_records_in_under_5_seconds(db_factory: sessionmaker[Session]) -> None:
    w = make_world(db_factory)
    with session_scope(db_factory) as s:
        syms = [
            m.Symbol(ticker=f"S{i:04d}", exchange="NYSE", questrade_id=None, currency="USD", name=None)
            for i in range(1000)
        ]
        s.add_all(syms)
        s.flush()
        for i, sym in enumerate(syms):
            s.add(
                m.UniverseSnapshot(
                    session_date=D,
                    symbol_id=sym.id,
                    price=Decimal("20"),
                    avg_volume=2_000_000,
                    atr14=Decimal("1"),
                    source="finviz",
                )
            )
            s.add(
                m.OpenBarStat(
                    symbol_id=sym.id, session_date=D, avg_open_vol_14d=Decimal("1000"), atr14=Decimal("1")
                )
            )
            if i % 10:
                s.add(
                    m.IntradayCandle(
                        symbol_id=sym.id,
                        interval="5m",
                        ts=OPEN,
                        open=Decimal("21"),
                        high=Decimal("21.5"),
                        low=Decimal("20.9"),
                        close=Decimal("21.4"),
                        volume=100 + i * 7,
                        vwap=None,
                    )
                )
        w.sym.update({sym.ticker: sym.id for sym in syms})
    for rank, i in enumerate(range(999, 979, -1), start=1):
        w.candidate(f"S{i:04d}", rank, "7.0", reject=None if rank == 1 else "lower_rank")
    w.event("strategy.orb_sip", "orb: ranked", {"ranked": 20}, SCAN_AT)
    started = wall_time.perf_counter()
    res = await record_day(deps(db_factory), w.run_id, D)
    elapsed = wall_time.perf_counter() - started
    assert res.skipped is None and res.stages["scan"] == 1001
    assert elapsed < 5.0, elapsed
    assert recorder.MAX_NOTES == 50


# --- the P6-T12 read side over what the recorder writes -----------------------------------------------------
async def test_the_reader_and_the_csv_read_what_the_recorder_writes(
    db_factory: sessionmaker[Session],
) -> None:
    import csv
    import io

    from tests.decisions.test_readonly import seed_full_day
    from trader.decisions.export import DECISION_CSV_COLUMNS, decisions_csv
    from trader.decisions.read import load_day
    from trader.decisions.summary import summary_from_json

    run_id = seed_full_day(db_factory)
    await record_day(deps(db_factory), run_id, D, final=True)
    view = load_day(db_factory, run_id, D)
    assert view is not None and view.final and view.summary is not None
    (day,) = rows(db_factory, run_id, "day")
    assert view.summary == summary_from_json(day.data)
    assert view.summary_text == day.data["text"]
    assert view.summary.trades == 1 and view.summary.approvals["manual"] == 1

    lines = list(csv.DictReader(io.StringIO("".join(decisions_csv(db_factory, run_id, D)))))
    assert len(lines) == view.total and set(lines[0]) == set(DECISION_CSV_COLUMNS)
    fill = next(x for x in lines if x["stage"] == "fill")
    assert (Decimal(fill["planned_price"]), Decimal(fill["fill_price"])) == (
        Decimal("21.51"),
        Decimal("21.53"),
    )
    assert Decimal(fill["diff_per_share"]) == Decimal("0.02")
    ranked = next(x for x in lines if x["stage"] == "scan" and x["ticker"] == "R01")
    assert ranked["or_high"] and ranked["entry"] and "rvol" in ranked["checks"]
    assert ranked["catalyst_type"] == "earnings_beat" and ranked["catalyst_reason"] == "Beat and raise."
    approval = next(x for x in lines if x["stage"] == "approval")
    assert (approval["decided_via"], approval["decision_latency_ms"]) == ("telegram", "30000")
    ex = next(x for x in lines if x["stage"] == "exit")
    assert (ex["exit_category"], ex["pnl"]) == ("flatten", "3.1000")
    proposal = next(x for x in lines if x["stage"] == "proposal")
    assert proposal["qty"] == "10" and proposal["risk_dollars"] and proposal["est_cost"]
