"""The decision recorder (P6-T10).

`record_day` builds run R's journal for session D from the authoritative rows (catalysts, job details,
universe snapshots, candidates, orb notes, signals, proposals, orders, fills, trades, kill-switch and overlay
events) and the stored opening bars, and writes only `decision_log` (insert and delete). It never calls
Questrade, Claude, Telegram or FinViz, takes the time only from `deps.clock`, and raises on database errors
(callers isolate them).

A pass (never on the event loop: every database step runs in `asyncio.to_thread` with its own session):
1. a quick unlocked peek (the day row's `final` flag and the source fingerprint): `final` or `unchanged`
   ends the pass early, so the worker loop's passes are cheap;
2. the scan inputs from `deps.scan_data` (async: the replay adapter serves the bars it already loaded);
3. one transaction under `pg_advisory_xact_lock(hashtext('trader.decisions:<run>:<date>'))`: the `final`
   and fingerprint checks are made again AFTER taking the lock (a pass that read "not final" just before the
   post-close's final pass committed must not rebuild the day and un-freeze it), then the day is rebuilt
   (delete, insert). The fingerprint stored is the one read in step 1, before the inputs, so a source that
   changed during the pass is picked up by the next one.

Row order: `STAGE_ORDER`, then `ts`, then the source id; `seq` numbers them from 1. The `day` row is last.
"""

import asyncio
import hashlib
import json
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from pydantic import ValidationError
from sqlalchemy import Text, cast, delete, func, insert, select, text
from sqlalchemy.orm import Session, sessionmaker

from trader.broker.fill_model import FillParams, QuoteFillModel
from trader.db import models as m
from trader.decisions.orb_explain import CONTEXT_CHECKS, candle_from, check_json, dec, explain_orb, levels
from trader.decisions.summary import SUMMARY_NOTE, summarize, summary_text, summary_to_json
from trader.decisions.types import (
    LOCK_PREFIX,
    MAX_DATA_BYTES,
    MAX_REASON_CHARS,
    STAGE_ORDER,
    DecisionOutcome,
    DecisionRecord,
    DecisionRowView,
    DecisionStage,
    RecorderDeps,
    RecordResult,
    RecordSkip,
    ScanData,
    ScanDetail,
)
from trader.logging_setup import REDACTED, is_secret_key, redact_text
from trader.market.calendar import SessionCalendar
from trader.market.clock import ET
from trader.market.indicators import rvol
from trader.market.types import INTERVAL_CODES, Candle, OpenBarStats, UniverseMember
from trader.settings_store import OVERLAY_SYMBOL, RuntimeSettings
from trader.strategies.orb_sip import OrbSipParams

ORB = "orb_sip"
ORB_SOURCE = "strategy.orb_sip"
OVERLAY_SOURCE = "strategy.spy_overlay"
EVENT_SOURCES = (ORB_SOURCE, OVERLAY_SOURCE)  # the only event_log sources the recorder reads
ORB_JOB = "event:orb_open"
OPENING_BAR_CODE = INTERVAL_CODES["FiveMinutes"]
OPENING_BAR = timedelta(minutes=5)
Q4 = Decimal("0.0001")
TRUNCATED = "truncated"
MAX_HEADLINES_SHOWN = 3
MAX_TITLE_CHARS = 200
MAX_NOTES = 50

# The catalyst classifier's stored-reason prefixes (trader.adapters.claude.catalyst OVER_CAP and
# BUDGET_EXCEEDED). Copied, not imported: that module imports the FinViz scraper and so httpx, which the
# decision package must not load (P6-T9 test 4); tests/decisions/test_static.py pins that they are equal.
OVER_CAP_PREFIX = "not classified (over cap)"
BUDGET_PREFIX = "daily budget"
# trader.engine.proposals.AUTO_FLATTEN_ACTOR, copied for the same reason (pinned by the same test).
AUTO_FLATTEN_ACTOR = "auto_flatten_on_expiry"

# Every exit reason string on trunk -> its category (tests/decisions/test_recorder.py pins the complete
# mapping; a new reason fails that test until it is mapped here).
EXIT_CATEGORIES: dict[str, str] = {
    "protective_stop": "stop",
    "stop": "stop",  # trades.exit_reason falls back to the order's purpose when it has no reason
    "flatten_close": "flatten",
    "overlay_negative": "overlay",
    "replay_forced_close": "replay_close",
    "exit": "other",
}
KILL_SWITCH_EXIT_PREFIX = "kill_switch"
SKIPPED_NOTES = {
    "orb: max_positions already used today": "skipped:max_positions",
    "orb: the universe is a stale fallback; no entries today": "skipped:stale_universe",
    "orb: no universe for this session": "skipped:no_universe",
}
# trader.engine.proposals rejects a blocked entry with f"entry blocked: {reason}", the reason from the
# kill-switch entry guard (trader.engine.killswitch: f"kill switch {switch} is tripped"); both pinned to
# their sources by tests/decisions/test_static.py, as are the orb_sip note texts in SKIPPED_NOTES.
ENTRY_BLOCKED_PREFIX = "entry blocked: "
_BLOCKED_SWITCH = re.compile(r"kill switch (\S+) is tripped")
_STAGE_INDEX = {s: i for i, s in enumerate(STAGE_ORDER)}


# --- masking and caps ---------------------------------------------------------------------------------------
def _jsonable(v: Any) -> Any:
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, datetime | date):
        return v.isoformat()
    if isinstance(v, Mapping):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, list | tuple | set | frozenset):
        return [_jsonable(x) for x in v]
    return v


def mask(v: Any) -> Any:
    """A JSON-safe, masked copy: strings by pattern, secret-named keys entirely."""
    v = _jsonable(v)
    if isinstance(v, str):
        return redact_text(v)
    if isinstance(v, Mapping):
        return {k: REDACTED if is_secret_key(k) and x is not None else mask(x) for k, x in v.items()}
    if isinstance(v, list):
        return [mask(x) for x in v]
    return v


def cap_text(s: Any, limit: int = MAX_REASON_CHARS) -> str | None:
    if s is None:
        return None
    return " ".join(redact_text(str(s)).split())[:limit]


def _size(d: Any) -> int:
    return len(json.dumps(d, sort_keys=True, default=str).encode())


def _longest_list(d: dict[str, Any]) -> tuple[dict[str, Any], str] | None:
    best: tuple[int, dict[str, Any], str] | None = None
    stack: list[dict[str, Any]] = [d]
    while stack:
        cur = stack.pop()
        for k, v in cur.items():
            if isinstance(v, list) and len(v) > 1:
                n = _size(v)
                if best is None or n > best[0]:
                    best = (n, cur, k)
            elif isinstance(v, dict):
                stack.append(v)
    return None if best is None else (best[1], best[2])


def cap_data(data: Mapping[str, Any]) -> dict[str, Any]:
    """Masked, and at most `MAX_DATA_BYTES` of JSON: the longest lists are halved (a `truncated` count of the
    items dropped is added) until it fits; as a last resort only the scalars are kept."""
    out: dict[str, Any] = mask(dict(data))
    dropped = 0
    while _size(out) > MAX_DATA_BYTES:
        found = _longest_list(out)
        if found is None:
            break
        holder, key = found
        items = holder[key]
        keep = len(items) // 2
        dropped += len(items) - keep
        holder[key] = items[:keep]
        out[TRUNCATED] = dropped
    if _size(out) > MAX_DATA_BYTES:
        small = {k: v for k, v in out.items() if not isinstance(v, dict | list) and _size(v) < 256}
        small[TRUNCATED] = max(dropped, 1)
        out = small
    return out


# --- values -------------------------------------------------------------------------------------------------
def _s(v: Any) -> str | None:
    return None if v is None else str(v)


def _pairs(rows: Iterable[Any]) -> dict[Any, Any]:
    """Two-column result rows as a dict."""
    return {r[0]: r[1] for r in rows}


def _day_window(d: date) -> tuple[datetime, datetime]:
    start = datetime.combine(d, time(0), tzinfo=ET)
    return start, datetime.combine(d + timedelta(days=1), time(0), tzinfo=ET)


def _minutes(a: datetime | None, b: datetime | None) -> str | None:
    if a is None or b is None:
        return None
    return str((Decimal((b - a).total_seconds()) / 60).quantize(Decimal("0.1"), ROUND_HALF_UP))


def _seconds(a: datetime | None, b: datetime | None) -> str | None:
    if a is None or b is None:
        return None
    return str(Decimal((b - a).total_seconds()).quantize(Decimal("0.001"), ROUND_HALF_UP))


def exit_category(exit_reason: str, *, expiry: bool = False) -> str:
    if expiry:
        return "expiry"
    if exit_reason.startswith(KILL_SWITCH_EXIT_PREFIX):
        return "kill_switch"
    return EXIT_CATEGORIES.get(exit_reason, "other")


# --- LiveScanData -------------------------------------------------------------------------------------------
class LiveScanData(ScanData):
    """`ScanData` over the database only (universe snapshots, open-bar stats, stored 5-minute opening bars in
    `intraday_candles`, else `candle_archive`). Never a network call; each read runs in a worker thread."""

    def __init__(self, factory: sessionmaker[Session], calendar: SessionCalendar | None = None) -> None:
        self._factory = factory
        self._calendar = calendar or SessionCalendar()

    def _universe(self, session_date: date) -> list[UniverseMember]:
        with self._factory() as s:
            rows = s.execute(
                select(m.UniverseSnapshot, m.Symbol)
                .join(m.Symbol, m.Symbol.id == m.UniverseSnapshot.symbol_id)
                .where(m.UniverseSnapshot.session_date == session_date)
                .order_by(m.Symbol.ticker)
            ).all()
        return [
            UniverseMember(sym.id, sym.ticker, sym.name, snap.price, snap.avg_volume, snap.atr14, snap.source)
            for snap, sym in rows
        ]

    def _stats(self, session_date: date) -> dict[int, OpenBarStats]:
        with self._factory() as s:
            rows = s.execute(
                select(m.OpenBarStat).where(m.OpenBarStat.session_date == session_date)
            ).scalars()
            return {r.symbol_id: OpenBarStats(r.symbol_id, r.avg_open_vol_14d, r.atr14) for r in rows}

    def _bars(self, session_date: date, symbol_ids: list[int]) -> dict[int, Candle]:
        open_ = self._calendar.session_open(session_date)
        ids = sorted(set(symbol_ids))
        out: dict[int, Candle] = {}
        if not ids:
            return out
        with self._factory() as s:
            for r in s.execute(
                select(m.IntradayCandle).where(
                    m.IntradayCandle.interval == OPENING_BAR_CODE,
                    m.IntradayCandle.ts == open_,
                    m.IntradayCandle.symbol_id.in_(ids),
                )
            ).scalars():
                out[r.symbol_id] = Candle(
                    r.ts, r.ts + OPENING_BAR, r.open, r.high, r.low, r.close, r.volume, r.vwap
                )
            rest = [i for i in ids if i not in out]
            if rest:
                for a in s.execute(
                    select(m.CandleArchive).where(
                        m.CandleArchive.interval == OPENING_BAR_CODE,
                        m.CandleArchive.start_ts == open_,
                        m.CandleArchive.symbol_id.in_(rest),
                    )
                ).scalars():
                    out[a.symbol_id] = Candle(
                        a.start_ts, a.start_ts + OPENING_BAR, a.open, a.high, a.low, a.close, a.volume, a.vwap
                    )
        return out

    async def universe(self, session_date: date) -> list[UniverseMember]:
        return await asyncio.to_thread(self._universe, session_date)

    async def open_bar_stats(self, session_date: date) -> dict[int, OpenBarStats]:
        return await asyncio.to_thread(self._stats, session_date)

    async def stored_opening_bars(self, session_date: date, symbol_ids: list[int]) -> dict[int, Candle]:
        return await asyncio.to_thread(self._bars, session_date, symbol_ids)


# --- the fingerprint ----------------------------------------------------------------------------------------
def _digest(s: Session, stmt: Any) -> str:
    rows = s.execute(stmt).all()
    return hashlib.sha256(repr([tuple(r) for r in rows]).encode()).hexdigest()[:16]


def settings_used(settings: RuntimeSettings) -> dict[str, str]:
    """The settings the rows are computed with (besides the scan detail): the pre-market gap threshold, the
    proposal cost buffer, the fee and slippage settings behind `est_fees`, and the FinViz filters shown on the
    universe row. Part of the fingerprint, so an edit rebuilds a day that is not final yet."""
    return {
        "premarket_gap_min_pct": str(settings.premarket_gap_min_pct),
        "slippage_buffer": str(settings.slippage_buffer),
        "slippage_min": str(settings.slippage_min),
        "slippage_bps": str(settings.slippage_bps),
        "stale_quote_seconds": str(settings.stale_quote_seconds),
        "fees_commission": str(settings.fees_commission),
        "fees_direct_route": str(settings.fees_direct_route),
        "fees_ecn_per_share": str(settings.fees_ecn_per_share),
        "fees_sec_rate": str(settings.fees_sec_rate),
        "universe_finviz_filters": settings.universe_finviz_filters,
    }


def fingerprint(
    s: Session,
    calendar: SessionCalendar,
    run_id: int,
    d: date,
    detail: ScanDetail,
    settings: RuntimeSettings,
) -> str:
    """A digest of the day's source rows. Run-scoped tables: count, max id and the columns that change in
    place (statuses, decisions, amended evidence); `catalysts` by D only (not run-scoped); `event_log` only
    the two strategy sources the recorder reads, for R within D (never the whole table, which the log mirror
    and alerts keep growing); the count of D's stored opening bars; the latest nightly and premarket job ids;
    D's manual watchlist; the scan detail setting and the other settings the rows use (`settings_used`)."""
    start, end = _day_window(d)
    open_ = calendar.session_open(d)
    signal_ids = select(m.Signal.id).where(m.Signal.run_id == run_id, m.Signal.session_date == d)
    order_ids = select(m.Order.id).where(m.Order.run_id == run_id, m.Order.session_date == d)
    parts: dict[str, Any] = {
        "candidates": _digest(
            s,
            select(m.Candidate.id, m.Candidate.created_at, m.Candidate.reject_reason, m.Candidate.passed)
            .where(m.Candidate.run_id == run_id, m.Candidate.session_date == d)
            .order_by(m.Candidate.id),
        ),
        "signals": _digest(
            s,
            select(m.Signal.id, func.md5(cast(m.Signal.evidence, Text)))
            .where(m.Signal.id.in_(signal_ids))
            .order_by(m.Signal.id),
        ),
        "proposals": _digest(
            s,
            select(
                m.Proposal.id,
                m.Proposal.status,
                m.Proposal.decided_at,
                m.Proposal.expired_at,
                m.Proposal.error,
            )
            .where(m.Proposal.signal_id.in_(signal_ids))
            .order_by(m.Proposal.id),
        ),
        "orders": _digest(
            s,
            select(m.Order.id, m.Order.status, m.Order.cancel_reason)
            .where(m.Order.id.in_(order_ids))
            .order_by(m.Order.id),
        ),
        "fills": _digest(
            s,
            select(func.count(), func.max(m.Fill.id)).where(
                m.Fill.run_id == run_id, m.Fill.order_id.in_(order_ids)
            ),
        ),
        "trades": _digest(
            s,
            select(func.count(), func.max(m.Trade.id)).where(
                m.Trade.run_id == run_id, m.Trade.session_date == d
            ),
        ),
        "kill_switch": _digest(
            s,
            select(m.KillSwitchEvent.id, m.KillSwitchEvent.reset_at)
            .where(
                m.KillSwitchEvent.run_id == run_id,
                (m.KillSwitchEvent.session_date == d)
                | ((m.KillSwitchEvent.reset_at >= start) & (m.KillSwitchEvent.reset_at < end)),
            )
            .order_by(m.KillSwitchEvent.id),
        ),
        "catalysts": _digest(
            s,
            select(m.Catalyst.id, m.Catalyst.classified_at, m.Catalyst.quality, m.Catalyst.reason)
            .where(m.Catalyst.session_date == d)
            .order_by(m.Catalyst.id),
        ),
        "events": _digest(
            s,
            select(func.count(), func.max(m.EventLog.id)).where(
                m.EventLog.source.in_(EVENT_SOURCES),
                m.EventLog.run_id == run_id,
                m.EventLog.ts >= start,
                m.EventLog.ts < end,
            ),
        ),
        "bars": s.execute(
            select(func.count()).where(
                m.IntradayCandle.interval == OPENING_BAR_CODE, m.IntradayCandle.ts == open_
            )
        ).scalar_one(),
        "jobs": _digest(
            s,
            select(m.JobRun.job, func.max(m.JobRun.id))
            .where(
                m.JobRun.job.in_(("nightly", "premarket", ORB_JOB)),
                m.JobRun.session_date == d,
                m.JobRun.status == "succeeded",
            )
            .group_by(m.JobRun.job)
            .order_by(m.JobRun.job),
        ),
        "universe": _digest(
            s,
            select(func.count(), m.UniverseSnapshot.source)
            .where(m.UniverseSnapshot.session_date == d)
            .group_by(m.UniverseSnapshot.source)
            .order_by(m.UniverseSnapshot.source),
        ),
        "watchlist": _digest(
            s,
            select(m.ManualWatchlist.filename, m.ManualWatchlist.uploaded_at).where(
                m.ManualWatchlist.session_date == d
            ),
        ),
        "detail": detail,
        "settings": settings_used(settings),
    }
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


# --- the pass -----------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class _Peek:
    final: bool
    stored: str | None
    current: str
    scan_needed: bool


@dataclass
class _Scan:
    members: list[UniverseMember]
    stats: dict[int, OpenBarStats]
    bars: dict[int, Candle]


def _day_row(s: Session, run_id: int, d: date) -> m.DecisionLog | None:
    return s.execute(
        select(m.DecisionLog)
        .where(m.DecisionLog.run_id == run_id, m.DecisionLog.session_date == d, m.DecisionLog.stage == "day")
        .order_by(m.DecisionLog.seq.desc())
        .limit(1)
    ).scalar_one_or_none()


def _stored_fp(row: m.DecisionLog | None) -> str | None:
    if row is None or not isinstance(row.data, dict):
        return None
    fp = row.data.get("fingerprint")
    return fp if isinstance(fp, str) else None


def _scan_needed(s: Session, run_id: int, d: date) -> bool:
    start, end = _day_window(d)
    has_candidates = s.execute(
        select(m.Candidate.id)
        .where(m.Candidate.run_id == run_id, m.Candidate.session_date == d, m.Candidate.strategy_key == ORB)
        .limit(1)
    ).first()
    if has_candidates:
        return True
    return bool(
        s.execute(
            select(m.EventLog.id)
            .where(
                m.EventLog.source == ORB_SOURCE,
                m.EventLog.run_id == run_id,
                m.EventLog.ts >= start,
                m.EventLog.ts < end,
            )
            .limit(1)
        ).first()
    )


def _peek(
    deps: RecorderDeps, run_id: int, d: date
) -> tuple[RuntimeSettings, RecordSkip | None, _Peek | None]:
    """The first worker-thread step of a pass: the settings (the live callable reads them from the database,
    so it is called here, never on the event loop), the disabled and non-session checks, then the unlocked
    peek at the day row and the fingerprint."""
    settings = deps.settings()
    if not settings.reports_decisions_enabled:
        return settings, "disabled", None
    if not deps.calendar.is_session(d):
        return settings, "not_session", None
    detail: ScanDetail = settings.reports_decisions_scan_detail
    with deps.factory() as s:
        row = _day_row(s, run_id, d)
        return (
            settings,
            None,
            _Peek(
                final=bool(row is not None and row.final),
                stored=_stored_fp(row),
                current=fingerprint(s, deps.calendar, run_id, d, detail, settings),
                scan_needed=_scan_needed(s, run_id, d),
            ),
        )


async def _gather(scan_data: ScanData | None, d: date) -> _Scan | None:
    if scan_data is None:
        return None
    members = await scan_data.universe(d)
    stats = await scan_data.open_bar_stats(d)
    ids = sorted({u.symbol_id for u in members})
    bars = await scan_data.stored_opening_bars(d, ids) if ids else {}
    return _Scan(members, stats, bars)


async def record_day(
    deps: RecorderDeps, run_id: int, session_date: date, *, final: bool = False, rebuild: bool = False
) -> RecordResult:
    """Build (or skip) run `run_id`'s journal for `session_date`. See the module docstring."""
    settings, skip, peek = await asyncio.to_thread(_peek, deps, run_id, session_date)
    if skip is not None or peek is None:
        return RecordResult(run_id, session_date, skip or "disabled", {}, False)
    detail: ScanDetail = settings.reports_decisions_scan_detail
    if peek.final and not rebuild:
        return RecordResult(run_id, session_date, "final", {}, True)
    if not rebuild and not final and peek.stored is not None and peek.stored == peek.current:
        return RecordResult(run_id, session_date, "unchanged", {}, False)
    scan = await _gather(deps.scan_data, session_date) if peek.scan_needed else None
    return await asyncio.to_thread(
        _write, deps, settings, run_id, session_date, final, rebuild, detail, peek.current, scan
    )


def lock_key(run_id: int, d: date) -> str:
    return f"{LOCK_PREFIX}:{run_id}:{d.isoformat()}"


def _write(
    deps: RecorderDeps,
    settings: RuntimeSettings,
    run_id: int,
    d: date,
    final: bool,
    rebuild: bool,
    detail: ScanDetail,
    fp: str,
    scan: _Scan | None,
) -> RecordResult:
    with deps.factory() as s, s.begin():
        s.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": lock_key(run_id, d)})
        return write_locked(
            s, deps, settings, run_id, d, final=final, rebuild=rebuild, detail=detail, fp=fp, scan=scan
        )


def write_locked(
    s: Session,
    deps: RecorderDeps,
    settings: RuntimeSettings,
    run_id: int,
    d: date,
    *,
    final: bool,
    rebuild: bool,
    detail: ScanDetail,
    fp: str | None,
    scan: _Scan | None,
) -> RecordResult:
    """The locked part of a pass (the caller holds the advisory lock in `s`'s transaction): the final and
    fingerprint checks, then the rebuild."""
    row = _day_row(s, run_id, d)
    if row is not None and row.final:
        if not rebuild:
            return RecordResult(run_id, d, "final", {}, True)
        final = True  # a rebuild of a frozen day keeps it frozen, whatever `final` the caller passed
    current = fingerprint(s, deps.calendar, run_id, d, detail, settings)
    stored = _stored_fp(row)
    if not rebuild and not final and stored is not None and stored == current:
        return RecordResult(run_id, d, "unchanged", {}, False)
    records = _Builder(s, deps, settings, run_id, d, detail, scan).build()
    now = deps.clock.now()
    views = [_view(run_id, i, r, now, final) for i, r in enumerate(records, start=1)]
    summary = summarize(views, run_id=run_id, session_date=d, final=final)
    day_data = {
        **summary_to_json(summary),
        "text": summary_text(summary, link=None),
        "fingerprint": fp or current,
    }
    last_ts = max([r.ts for r in records], default=deps.calendar.session_close(d))
    records.append(
        DecisionRecord(
            session_date=d,
            stage="day",
            outcome="info",
            ts=max(last_ts, deps.calendar.session_close(d)),
            data=day_data,
        )
    )
    s.execute(delete(m.DecisionLog).where(m.DecisionLog.run_id == run_id, m.DecisionLog.session_date == d))
    values = [
        {
            "run_id": run_id,
            "session_date": d,
            "seq": i,
            "stage": r.stage,
            "strategy_key": r.strategy_key,
            "symbol_id": r.symbol_id,
            "ticker": r.ticker,
            "outcome": r.outcome,
            "rule": None if r.rule is None else r.rule[:60],
            "reason": cap_text(r.reason),
            "ts": r.ts,
            "ref": {k: int(v) for k, v in r.ref.items()},
            "data": day_data if r.stage == "day" else cap_data(r.data),
            "recorded_at": now,
            "final": final,
        }
        for i, r in enumerate(records, start=1)
    ]
    s.execute(insert(m.DecisionLog), values)
    stages: Counter[str] = Counter(r.stage for r in records)
    return RecordResult(run_id, d, None, dict(stages), final)


def _view(run_id: int, seq: int, r: DecisionRecord, now: datetime, final: bool) -> DecisionRowView:
    return DecisionRowView(
        id=0,
        run_id=run_id,
        session_date=r.session_date,
        seq=seq,
        stage=r.stage,
        strategy_key=r.strategy_key,
        symbol_id=r.symbol_id,
        ticker=r.ticker,
        outcome=r.outcome,
        rule=r.rule,
        reason=r.reason,
        ts=r.ts,
        ref=r.ref,
        data=r.data,
        recorded_at=now,
        final=final,
    )


# --- building the rows --------------------------------------------------------------------------------------
@dataclass(order=True)
class _Keyed:
    key: tuple[int, datetime, int]
    record: DecisionRecord = field(compare=False)


class _Builder:
    """Reads the day's source rows in one session and turns them into `DecisionRecord`s."""

    def __init__(
        self,
        s: Session,
        deps: RecorderDeps,
        settings: RuntimeSettings,
        run_id: int,
        d: date,
        detail: ScanDetail,
        scan: _Scan | None,
    ) -> None:
        self.s = s
        self.cal = deps.calendar
        self.settings = settings
        self.run_id = run_id
        self.d = d
        self.detail = detail
        self.scan = scan
        self.start, self.end = _day_window(d)
        self.open = self.cal.session_open(d)
        self.close = self.cal.session_close(d)
        run = s.get(m.Run, run_id)
        self.mode = run.mode if run is not None else "live"
        self.run_params: Mapping[str, Any] = (
            run.params if run is not None and isinstance(run.params, dict) else {}
        )
        self.out: list[_Keyed] = []
        self._tickers: dict[int, str] = {}

    # helpers
    def add(self, rec: DecisionRecord, source_id: int = 0) -> None:
        self.out.append(_Keyed((_STAGE_INDEX[rec.stage], rec.ts, source_id), rec))

    def ticker(self, symbol_id: int | None) -> str | None:
        if symbol_id is None:
            return None
        if symbol_id not in self._tickers:
            t = self.s.execute(select(m.Symbol.ticker).where(m.Symbol.id == symbol_id)).scalar_one_or_none()
            self._tickers[symbol_id] = t or ""
        return self._tickers[symbol_id] or None

    def load_tickers(self, ids: Iterable[int | None]) -> None:
        want = sorted({i for i in ids if i is not None} - self._tickers.keys())
        if want:
            rows = self.s.execute(select(m.Symbol.id, m.Symbol.ticker).where(m.Symbol.id.in_(want))).all()
            self._tickers.update(_pairs(rows))

    def rec(
        self,
        stage: DecisionStage,
        outcome: DecisionOutcome,
        ts: datetime,
        *,
        source_id: int = 0,
        strategy_key: str | None = None,
        symbol_id: int | None = None,
        ticker: str | None = None,
        rule: str | None = None,
        reason: str | None = None,
        ref: Mapping[str, int] | None = None,
        data: Mapping[str, Any] | None = None,
    ) -> None:
        self.add(
            DecisionRecord(
                session_date=self.d,
                stage=stage,
                outcome=outcome,
                ts=ts,
                strategy_key=strategy_key,
                symbol_id=symbol_id,
                ticker=ticker if ticker is not None else self.ticker(symbol_id),
                rule=rule,
                reason=reason,
                ref=dict(ref or {}),
                data=dict(data or {}),
            ),
            source_id,
        )

    def build(self) -> list[DecisionRecord]:
        self.universe()
        self.premarket()
        self.orb_scan()
        signal_rows = self.signals()
        self.proposals(signal_rows)
        orders = self.orders()
        self.fills(orders)
        self.exits()
        self.overlay()
        self.kill_switches()
        return [k.record for k in sorted(self.out)]

    def _latest_job(self, job: str) -> m.JobRun | None:
        return self.s.execute(
            select(m.JobRun)
            .where(m.JobRun.job == job, m.JobRun.session_date == self.d, m.JobRun.status == "succeeded")
            .order_by(m.JobRun.id.desc())
            .limit(1)
        ).scalar_one_or_none()

    # --- universe -------------------------------------------------------------------------------------------
    def universe(self) -> None:
        job = self._latest_job("nightly")
        jd: Mapping[str, Any] = job.detail if job is not None and isinstance(job.detail, dict) else {}
        sources: dict[Any, Any] = _pairs(
            self.s.execute(
                select(m.UniverseSnapshot.source, func.count())
                .where(m.UniverseSnapshot.session_date == self.d)
                .group_by(m.UniverseSnapshot.source)
            ).all()
        )
        size: int | None = sum(sources.values()) if sources else None
        if not sources and self.scan is not None and self.scan.members:  # a replay's biased universe
            counted = Counter(u.source for u in self.scan.members)
            sources, size = dict(counted), len(self.scan.members)
        source = next(iter(sources)) if len(sources) == 1 else ("mixed" if sources else None)
        watch = self.s.get(m.ManualWatchlist, self.d)
        data: dict[str, Any] = {
            "size": size,
            "source": source,
            "sources": sources,
            "fallback_from": jd.get("fallback_from"),
            "fallback_age_sessions": jd.get("fallback_age_sessions"),
            "fallback_stale": jd.get("fallback_stale"),
            "unresolved": jd.get("unresolved"),
            "candle_errors": jd.get("candle_errors"),
            "nightly_job_id": None if job is None else job.id,
            "watchlist": None if watch is None else watch.filename or "(uploaded)",
            "finviz_filters": self.settings.universe_finviz_filters,
            "note": "finviz_filters are the current settings (settings history is not kept)",
        }
        if size is None:
            data[SUMMARY_NOTE] = "no universe stored for this session"
        elif jd.get("fallback_from"):
            stale = ", stale" if jd.get("fallback_stale") else ""
            data[SUMMARY_NOTE] = f"fallback universe from {jd.get('fallback_from')}{stale}"
        ts = (job.finished_at or job.started_at) if job is not None else self.open
        self.rec("universe", "info", ts, source_id=0 if job is None else job.id, data=data)

    # --- premarket ------------------------------------------------------------------------------------------
    def premarket(self) -> None:
        job = self._latest_job("premarket")
        jd: Mapping[str, Any] = job.detail if job is not None and isinstance(job.detail, dict) else {}
        screens = [x for x in jd.get("screens") or [] if isinstance(x, dict)]
        matched: dict[str, list[str]] = {}
        for sc in screens:
            for t in sc.get("matched") or []:
                matched.setdefault(str(t), []).append(str(sc.get("label")))
        if job is not None:
            errors = list(jd.get("screen_errors") or [])
            data: dict[str, Any] = {
                "screens": [
                    {
                        "label": sc.get("label"),
                        "filter": sc.get("filter"),
                        "rows": sc.get("rows"),
                        "matched": len(sc.get("matched") or []),
                    }
                    for sc in screens
                ],
                "screen_errors": [cap_text(e, 300) for e in errors],
                "quote_error": cap_text(jd.get("quote_error"), 300),
                "budget_hit": list(jd.get("budget_hit") or []),
                "over_cap": len(jd.get("over_cap") or []),
                "candidates": jd.get("candidates"),
                "classified": jd.get("classified"),
                "premarket_job_id": job.id,
            }
            if errors:
                data[SUMMARY_NOTE] = f"{len(errors)} FinViz screen error(s) in the pre-market"
            self.rec("premarket", "info", job.finished_at or job.started_at, source_id=0, data=data)
        replay_mode = self.run_params.get("catalyst_mode") if self.mode == "replay" else None
        if replay_mode == "unknown":
            return  # the replay saw no catalysts: listing the live ones would mislead
        gap_min = self.settings.premarket_gap_min_pct
        rows = self.s.execute(
            select(m.Catalyst, m.Symbol.ticker)
            .join(m.Symbol, m.Symbol.id == m.Catalyst.symbol_id)
            .where(m.Catalyst.session_date == self.d)
            .order_by(m.Catalyst.id)
        ).all()
        for c, ticker in rows:
            self._tickers[c.symbol_id] = ticker
            sources = sorted(set(matched.get(ticker, [])))
            if c.gap_pct is not None and abs(c.gap_pct) >= gap_min:
                sources.append("gap")
            headlines = [h for h in (c.headlines or []) if isinstance(h, dict | str)]
            titles = [
                cap_text(h.get("title") if isinstance(h, dict) else h, MAX_TITLE_CHARS)
                for h in headlines[:MAX_HEADLINES_SHOWN]
            ]
            classified = c.classified_at is not None
            rule: str | None = None
            if not classified:
                why = c.reason or ""
                rule = (
                    "over_cap"
                    if why.startswith(OVER_CAP_PREFIX)
                    else "budget"
                    if why.startswith(BUDGET_PREFIX)
                    else "unclassified"
                )
            self.rec(
                "premarket",
                "classified" if classified else "listed",
                c.classified_at or c.created_at,
                source_id=c.id,
                symbol_id=c.symbol_id,
                ticker=ticker,
                rule=rule,
                reason=c.reason,
                ref={"catalyst_id": c.id},
                data={
                    "sources": sources,
                    "gap_pct": _s(c.gap_pct),
                    "gap_min_pct": str(gap_min),
                    "catalyst_type": c.catalyst_type,
                    "direction": c.direction,
                    "catalyst_quality": c.quality,
                    "confirmed": c.confirmed,
                    "headlines": len(headlines),
                    "headline_titles": titles,
                    "catalyst_reason": cap_text(c.reason),
                    "model": c.model,
                    "cost_usd": _s(c.cost_usd),
                },
            )

    # --- the 9:35 scan --------------------------------------------------------------------------------------
    def _params(self, ref_ts: datetime) -> tuple[OrbSipParams, dict[str, Any], str | None]:
        """The orb_sip params in effect: a replay's pinned params, else the latest live revision created at or
        before the scan."""
        raw: Mapping[str, Any] | None = None
        info: dict[str, Any] = {}
        if self.mode == "replay":
            for pin in self.run_params.get("strategies") or []:
                if isinstance(pin, dict) and pin.get("key") == ORB:
                    raw = pin.get("params") or {}
                    info = {k: pin.get(k) for k in ("config_id", "revision", "scope", "version")}
                    info["source"] = "replay_pin"
        else:
            cfg = self.s.execute(
                select(m.StrategyConfig)
                .where(
                    m.StrategyConfig.strategy_key == ORB,
                    m.StrategyConfig.scope == "live",
                    m.StrategyConfig.created_at <= ref_ts,
                )
                .order_by(m.StrategyConfig.created_at.desc(), m.StrategyConfig.revision.desc())
                .limit(1)
            ).scalar_one_or_none()
            if cfg is not None:
                raw = cfg.params or {}
                info = {
                    "config_id": cfg.id,
                    "revision": cfg.revision,
                    "scope": cfg.scope,
                    "version": cfg.version,
                    "created_at": cfg.created_at.isoformat(),
                    "source": "strategy_configs",
                }
        note = None
        try:
            params = OrbSipParams.model_validate(dict(raw or {}))
        except ValidationError:
            params, note = OrbSipParams(), "orb_sip params in effect did not validate; defaults shown"
        if raw is None:
            info["source"] = "defaults"
        return params, info, note

    def _catalyst_rows(self, ids: Sequence[int]) -> dict[int, m.Catalyst]:
        if not ids:
            return {}
        rows = self.s.execute(
            select(m.Catalyst).where(m.Catalyst.session_date == self.d, m.Catalyst.symbol_id.in_(list(ids)))
        ).scalars()
        return {c.symbol_id: c for c in rows}

    def _catalyst_view(self, sid: int, row: m.Catalyst | None) -> dict[str, Any] | None:
        """The catalyst the strategy would have seen: the stored row, or for a replay the replay's mode."""
        if self.mode == "replay" and (self.run_params.get("catalyst_mode") == "unknown" or row is None):
            return {"type": "unknown", "direction": "neutral", "quality": None, "classified": False}
        if row is None:
            return None
        return {
            "type": row.catalyst_type,
            "direction": row.direction,
            "quality": row.quality,
            "classified": row.classified_at is not None,
        }

    def orb_scan(self) -> None:
        cands = list(
            self.s.execute(
                select(m.Candidate)
                .where(
                    m.Candidate.run_id == self.run_id,
                    m.Candidate.session_date == self.d,
                    m.Candidate.strategy_key == ORB,
                )
                .order_by(m.Candidate.rank, m.Candidate.id)
            ).scalars()
        )
        notes = list(
            self.s.execute(
                select(m.EventLog)
                .where(
                    m.EventLog.source == ORB_SOURCE,
                    m.EventLog.run_id == self.run_id,
                    m.EventLog.ts >= self.start,
                    m.EventLog.ts < self.end,
                )
                .order_by(m.EventLog.id)
            ).scalars()
        )
        if not cands and not notes:
            return
        job = self.s.execute(
            select(m.JobRun)
            .where(m.JobRun.job == ORB_JOB, m.JobRun.session_date == self.d)
            .order_by(m.JobRun.started_at)
            .limit(1)
        ).scalar_one_or_none()
        if cands:
            scan_ts = min(c.created_at for c in cands)
        elif job is not None:
            scan_ts = job.started_at
        else:
            scan_ts = notes[0].ts
        params, params_info, params_note = self._params(scan_ts)
        skipped = next((SKIPPED_NOTES[n.message] for n in notes if n.message in SKIPPED_NOTES), None)
        missing: dict[int, str] = {}
        for n in notes:
            data = n.data if isinstance(n.data, dict) else {}
            miss = data.get("missing")
            if isinstance(miss, dict):
                for k, v in miss.items():
                    if str(k).isdigit():
                        missing[int(k)] = str(v)
        self.load_tickers([c.symbol_id for c in cands])
        # The overlay symbol never appears in the scan stage (it is the market filter, not a candidate), even
        # when the strategy ranked it and stored a candidate for it: its row is dropped here and it is left
        # out of every count, so the counts equal the rows; it keeps its place in the outside_top_n ranking.
        overlay_cands = [c for c in cands if self._is_overlay(c)]
        cand_ids = {c.symbol_id for c in cands}
        cands = [c for c in cands if not self._is_overlay(c)]
        catalyst_rows = self._catalyst_rows([c.symbol_id for c in cands])
        rules: Counter[str] = Counter()
        for c in cands:
            data = c.data if isinstance(c.data, dict) else {}
            stored_cat = data.get("catalyst")
            catalyst = (
                stored_cat
                if isinstance(stored_cat, dict)
                else self._catalyst_view(c.symbol_id, catalyst_rows.get(c.symbol_id))
            )
            checks = explain_orb(data, c.candle, params, catalyst=catalyst, reject_reason=c.reject_reason)
            candle = c.candle if isinstance(c.candle, dict) else {}
            entry, stop_loss = dec(data.get("entry")), dec(data.get("stop_loss"))
            if entry is None or stop_loss is None:
                lv = levels(candle_from(candle), dec(data.get("atr14")), params)
                if lv is not None:
                    entry, stop_loss = lv
            cat_row = catalyst_rows.get(c.symbol_id)
            if c.reject_reason:
                rules[c.reject_reason] += 1
            self.rec(
                "scan",
                "passed" if c.passed else "rejected",
                c.created_at,
                source_id=c.id,
                strategy_key=ORB,
                symbol_id=c.symbol_id,
                ticker=str(data.get("ticker") or "") or None,
                rule=c.reject_reason,
                reason=None,
                ref={"candidate_id": c.id},
                data={
                    "rvol": _s(c.rvol) if c.rvol is not None else data.get("rvol"),
                    "rank": c.rank,
                    "direction": data.get("direction"),
                    "candle": candle,
                    "or_high": candle.get("high"),
                    "or_low": candle.get("low"),
                    "atr14": data.get("atr14"),
                    "atr_source": data.get("atr_source"),
                    "price": data.get("price"),
                    "avg_volume": data.get("avg_volume"),
                    "avg_open_vol_14d": data.get("avg_open_vol_14d"),
                    "universe_source": data.get("universe_source"),
                    "entry": _s(entry),
                    "stop_loss": _s(stop_loss),
                    "target": None,  # orb_sip has no target (SPEC §5.2)
                    "r_per_share": _s(entry - stop_loss)
                    if entry is not None and stop_loss is not None
                    else None,
                    "catalyst": catalyst,
                    "catalyst_type": None if catalyst is None else catalyst.get("type"),
                    "catalyst_quality": None if catalyst is None else catalyst.get("quality"),
                    "catalyst_reason": None if cat_row is None else cap_text(cat_row.reason),
                    "checks": [check_json(x) for x in checks],
                    "check_sources": dict.fromkeys(sorted(CONTEXT_CHECKS), "reject_reason"),
                },
            )
        counts: dict[str, Any]
        summary_note: str | None = None
        ran = skipped is None
        if ran and self.scan is not None:
            counts = self._non_candidates(params, cands, cand_ids, missing, scan_ts, rules)
        else:
            counts = {
                "scanned": len(cands),
                "rvol_passed": len(cands),
                "ranked": len(cands),
                "passed": sum(1 for c in cands if c.passed),
            }
            if ran and self.scan is None:
                summary_note = "no scan data: only the ranked names are recorded"
        counts["rejects_by_rule"] = dict(sorted(rules.items()))
        if skipped is not None:
            summary_note = f"9:35 scan {skipped}"
        elif params_note:
            summary_note = params_note
        summary: dict[str, Any] = {
            "counts": counts,
            "params": params.model_dump(mode="json"),
            "params_in_effect": params_info,
            "scan_detail": self.detail if self.scan is not None else "ranked",
            "universe_status": self._universe_status(),
            "overlay_candidate": [
                {
                    "ticker": OVERLAY_SYMBOL,
                    "rank": c.rank,
                    "passed": c.passed,
                    "reject_reason": c.reject_reason,
                }
                for c in overlay_cands
            ]
            or None,
            "notes": [
                {"ts": n.ts.isoformat(), "level": n.level, "message": n.message, "data": n.data}
                for n in notes[:MAX_NOTES]
            ],
        }
        if summary_note:
            summary[SUMMARY_NOTE] = summary_note
        self.rec(
            "scan",
            "rejected" if skipped else "info",
            scan_ts,
            source_id=0,
            strategy_key=ORB,
            rule=skipped,
            reason=None if skipped is None else next(n.message for n in notes if n.message in SKIPPED_NOTES),
            data=summary,
        )

    def _is_overlay(self, c: m.Candidate) -> bool:
        data = c.data if isinstance(c.data, dict) else {}
        return (self._tickers.get(c.symbol_id) or data.get("ticker")) == OVERLAY_SYMBOL

    def _universe_status(self) -> dict[str, Any]:
        job = self._latest_job("nightly")
        jd: Mapping[str, Any] = job.detail if job is not None and isinstance(job.detail, dict) else {}
        return {k: jd.get(k) for k in ("source", "fallback_from", "fallback_age_sessions", "fallback_stale")}

    def _non_candidates(
        self,
        params: OrbSipParams,
        cands: Sequence[m.Candidate],
        cand_ids: set[int],
        missing: Mapping[int, str],
        scan_ts: datetime,
        rules: Counter[str],
    ) -> dict[str, Any]:
        assert self.scan is not None
        members = [u for u in self.scan.members if u.ticker != OVERLAY_SYMBOL]
        for u in self.scan.members:
            self._tickers.setdefault(u.symbol_id, u.ticker)
        stats, bars = self.scan.stats, self.scan.bars
        # the strategy's ranking: every member whose opening bar met rvol_min, by (-rvol, ticker)
        scored: list[tuple[Decimal, str, int]] = []
        rv: dict[int, Decimal | None] = {}
        for u in self.scan.members:
            bar = bars.get(u.symbol_id)
            if bar is None:
                continue
            st = stats.get(u.symbol_id)
            r = rvol(bar.volume, st.avg_open_vol_14d if st else None)
            rv[u.symbol_id] = r
            if r is not None and r >= params.rvol_min:
                scored.append((r, u.ticker, u.symbol_id))
        scored.sort(key=lambda t: (-t[0], t[1]))
        rank_of = {sid: i for i, (_, _, sid) in enumerate(scored, start=1)}
        outside = 0
        write = self.detail == "all"
        for u in sorted(members, key=lambda x: x.ticker):
            sid = u.symbol_id
            if sid in cand_ids:
                continue
            bar = bars.get(sid)
            st = stats.get(sid)
            base: dict[str, Any] = {
                "price": _s(u.price),
                "avg_volume": u.avg_volume,
                "atr14": _s(st.atr14 if st is not None and st.atr14 is not None else u.atr14),
                "avg_open_vol_14d": _s(st.avg_open_vol_14d if st else None),
                "universe_source": u.source,
            }
            rule: str
            reason: str | None = None
            if bar is None:
                rule, reason = "no_opening_bar", missing.get(sid, "no stored opening bar")
                base["missing_reason"] = reason
            else:
                r = rv.get(sid)
                base.update(
                    rvol=_s(r),
                    or_high=str(bar.high),
                    or_low=str(bar.low),
                    candle={
                        "start": bar.start.isoformat(),
                        "open": str(bar.open),
                        "high": str(bar.high),
                        "low": str(bar.low),
                        "close": str(bar.close),
                        "volume": bar.volume,
                    },
                )
                if r is None:
                    rule = "no_baseline"
                elif r < params.rvol_min:
                    rule = "rvol_below_min"
                    base["checks"] = [
                        {
                            "name": "rvol",
                            "value": str(r),
                            "op": ">=",
                            "threshold": str(params.rvol_min),
                            "passed": False,
                        }
                    ]
                else:
                    rule = "outside_top_n"
                    outside += 1
                    base["rank"] = rank_of.get(sid)
                    base["checks"] = [
                        {
                            "name": "rvol",
                            "value": str(r),
                            "op": ">=",
                            "threshold": str(params.rvol_min),
                            "passed": True,
                        },
                        {
                            "name": "rank",
                            "value": _s(rank_of.get(sid)),
                            "op": "<=",
                            "threshold": str(params.top_n),
                            "passed": False,
                        },
                    ]
            rules[rule] += 1
            if write:
                self.rec(
                    "scan",
                    "rejected",
                    scan_ts,
                    source_id=sid,
                    strategy_key=ORB,
                    symbol_id=sid,
                    ticker=u.ticker,
                    rule=rule,
                    reason=reason,
                    data=base,
                )
        # `cands` excludes the overlay symbol's candidate, and `members` the overlay symbol
        scanned = len([u for u in members if u.symbol_id not in cand_ids]) + len(cands)
        return {
            "scanned": scanned,
            "rvol_passed": len(cands) + outside,
            "ranked": len(cands),
            "passed": sum(1 for c in cands if c.passed),
        }

    # --- signals, risk, proposals, approvals ----------------------------------------------------------------
    def signals(self) -> dict[int, m.Signal]:
        sigs = list(
            self.s.execute(
                select(m.Signal)
                .where(m.Signal.run_id == self.run_id, m.Signal.session_date == self.d)
                .order_by(m.Signal.ts, m.Signal.id)
            ).scalars()
        )
        if not sigs:
            return {}
        keys: dict[int, str] = _pairs(
            self.s.execute(
                select(m.StrategyConfig.id, m.StrategyConfig.strategy_key).where(
                    m.StrategyConfig.id.in_(sorted({x.strategy_config_id for x in sigs}))
                )
            ).all()
        )
        with_proposal = set(
            self.s.execute(
                select(m.Proposal.signal_id).where(m.Proposal.signal_id.in_([x.id for x in sigs]))
            ).scalars()
        )
        self.load_tickers([x.symbol_id for x in sigs])
        for sig in sigs:
            ev: Mapping[str, Any] = sig.evidence if isinstance(sig.evidence, dict) else {}
            intent: Mapping[str, Any] = sig.intent if isinstance(sig.intent, dict) else {}
            rejection = ev.get("rejection") if isinstance(ev.get("rejection"), dict) else None
            error = ev.get("error") if isinstance(ev.get("error"), dict) else None
            outcome: DecisionOutcome
            reason: str | None = None
            rule: str | None = None
            if error is not None:
                outcome, rule = "error", str(error.get("type") or "error")
                reason = str(error.get("message") or "")
            elif rejection is not None:
                outcome, rule = "rejected", str(rejection.get("check") or "")
                reason = str(rejection.get("reason") or "")
            elif sig.id in with_proposal:
                outcome = "proposed"
            else:
                outcome = "info"
            key = keys.get(sig.strategy_config_id)
            evidence = {k: v for k, v in ev.items() if k not in ("candle",)}
            self.rec(
                "signal",
                outcome,
                sig.ts,
                source_id=sig.id,
                strategy_key=key,
                symbol_id=sig.symbol_id,
                rule=rule,
                reason=reason,
                ref={"signal_id": sig.id},
                data={
                    "event_key": sig.event_key,
                    "intent": dict(intent),
                    "intent_type": intent.get("type"),
                    "order_type": intent.get("order_type"),
                    "stop": intent.get("stop"),
                    "limit": intent.get("limit"),
                    "stop_loss": intent.get("stop_loss"),
                    "evidence": evidence,
                },
            )
            if rejection is not None:
                detail = rejection.get("detail") if isinstance(rejection.get("detail"), dict) else {}
                self.rec(
                    "risk",
                    "rejected",
                    sig.ts,
                    source_id=sig.id,
                    strategy_key=key,
                    symbol_id=sig.symbol_id,
                    rule=str(rejection.get("check") or ""),
                    reason=str(rejection.get("reason") or ""),
                    ref={"signal_id": sig.id},
                    data={
                        "detail": detail,
                        "sizing": ev.get("sizing") or (detail if "shares" in detail else None),
                    },
                )
        return {x.id: x for x in sigs}

    def proposals(self, sigs: Mapping[int, m.Signal]) -> None:
        if not sigs:
            return
        rows = list(
            self.s.execute(
                select(m.Proposal)
                .where(m.Proposal.signal_id.in_(list(sigs)))
                .order_by(m.Proposal.created_at, m.Proposal.id)
            ).scalars()
        )
        keys: dict[int, str] = _pairs(
            self.s.execute(
                select(m.StrategyConfig.id, m.StrategyConfig.strategy_key).where(
                    m.StrategyConfig.id.in_(sorted({x.strategy_config_id for x in sigs.values()}))
                )
            ).all()
        )
        fees_model = QuoteFillModel(FillParams.from_settings(self.settings))
        for p in rows:
            sig = sigs[p.signal_id]
            spec: Mapping[str, Any] = p.order_spec if isinstance(p.order_spec, dict) else {}
            sizing: Mapping[str, Any] = p.sizing if isinstance(p.sizing, dict) else {}
            symbol_id = spec.get("symbol_id") if isinstance(spec.get("symbol_id"), int) else sig.symbol_id
            entry = (
                dec(spec.get("stop"))
                if spec.get("order_type") in ("stop", "stop_limit")
                else dec(spec.get("limit"))
            )
            if entry is None:
                entry = dec(sizing.get("entry"))
            stop_loss = dec(spec.get("stop_loss"))
            data: dict[str, Any] = {
                "kind": p.kind,
                "qty": p.qty,
                "order_type": spec.get("order_type"),
                "side": spec.get("side"),
                "entry": _s(entry) if p.kind == "entry" else None,
                "stop": spec.get("stop"),
                "limit": spec.get("limit"),
                "stop_loss": _s(stop_loss),
                "target": None,
                "expires_at": p.expires_at.isoformat(),
                "status": p.status,
            }
            if p.kind == "entry" and entry is not None:
                buffer = dec(sizing.get("slippage_buffer"))
                if buffer is None:
                    buffer = self.settings.slippage_buffer
                est_cost = (entry * p.qty).quantize(Q4, ROUND_HALF_UP)
                data.update(
                    r_per_share=_s(entry - stop_loss) if stop_loss is not None else None,
                    risk_dollars=_s(((entry - stop_loss) * p.qty).quantize(Q4, ROUND_HALF_UP))
                    if stop_loss is not None
                    else None,
                    est_cost=str(est_cost),
                    est_cost_buffered=str((est_cost * (1 + buffer)).quantize(Q4, ROUND_HALF_UP)),
                    est_fees=str(fees_model.fees("buy", p.qty, entry).total),
                    est_fees_source="fill_model",
                    sizing={
                        k: sizing.get(k)
                        for k in (
                            "equity",
                            "risk_pct",
                            "risk_dollars",
                            "shares_risk",
                            "shares_cash",
                            "limited_by",
                        )
                    }
                    # SIZECAP: the per-stock cap, on proposals sized with it (older ones never had it)
                    | {k: sizing[k] for k in ("shares_cap", "max_position_pct", "cap_dollars") if k in sizing}
                    | {"sized_risk_dollars": sizing.get("risk_dollars")},
                )
                data["sizing"].pop("risk_dollars", None)
            key = keys.get(sig.strategy_config_id)
            self.rec(
                "proposal",
                "proposed",
                p.created_at,
                source_id=p.id,
                strategy_key=key,
                symbol_id=symbol_id,
                ref={"proposal_id": p.id, "signal_id": sig.id},
                data=data,
            )
            self._approval(p, key, symbol_id)

    def _approval(self, p: m.Proposal, key: str | None, symbol_id: int | None) -> None:
        if p.status == "pending":
            return
        outcome: DecisionOutcome
        rule: str | None = None
        reason: str | None = None
        error = p.error or ""
        if p.status == "expired" or (p.decided_by == AUTO_FLATTEN_ACTOR and p.expired_at is not None):
            outcome = "expired"
            if p.decided_by == AUTO_FLATTEN_ACTOR:
                rule = "auto_executed_on_expiry"
        elif p.status == "rejected" and error.startswith(ENTRY_BLOCKED_PREFIX):
            outcome = "blocked"
            hit = _BLOCKED_SWITCH.search(error)
            rule, reason = (hit.group(1) if hit else "entry_blocked"), error
        elif p.status == "rejected":
            outcome = "declined"
        elif p.status == "failed":
            outcome, rule, reason = "error", "failed", error
        elif p.decided_via == "auto" or p.status == "auto_approved":
            outcome = "auto_approved"
        else:
            outcome, rule = "approved", "manual"
        ts = p.decided_at or p.expired_at or p.created_at
        self.rec(
            "approval",
            outcome,
            ts,
            source_id=p.id,
            strategy_key=key,
            symbol_id=symbol_id,
            rule=rule,
            reason=reason,
            ref={"proposal_id": p.id},
            data={
                "kind": p.kind,
                "status": p.status,
                "decided_via": p.decided_via,
                "decided_by": p.decided_by,
                "decided_at": p.decided_at,
                "decision_latency_ms": p.decision_latency_ms,
                "escalations": p.escalations,
                "expired_at": p.expired_at,
                "order_id": p.order_id,
            },
        )

    # --- orders, fills, exits -------------------------------------------------------------------------------
    def orders(self) -> dict[int, m.Order]:
        rows = list(
            self.s.execute(
                select(m.Order)
                .where(m.Order.run_id == self.run_id, m.Order.session_date == self.d)
                .order_by(m.Order.submitted_at, m.Order.id)
            ).scalars()
        )
        self.load_tickers([o.symbol_id for o in rows])
        for o in rows:
            outcome: DecisionOutcome = {
                "working": "submitted",
                "filled": "filled",
                "cancelled": "cancelled",
            }.get(o.status, "submitted")  # type: ignore[assignment]
            self.rec(
                "order",
                outcome,
                o.submitted_at,
                source_id=o.id,
                symbol_id=o.symbol_id,
                rule=o.cancel_reason if o.status == "cancelled" else None,
                reason=o.reason or None,
                ref={"order_id": o.id} | ({"proposal_id": o.proposal_id} if o.proposal_id else {}),
                data={
                    "purpose": o.purpose,
                    "side": o.side,
                    "order_type": o.order_type,
                    "qty": o.qty,
                    "stop": _s(o.stop_price),
                    "limit": _s(o.limit_price),
                    "stop_loss": _s(o.stop_loss),
                    "status": o.status,
                    "closed_at": o.closed_at,
                    "position_id": o.position_id,
                },
            )
        return {o.id: o for o in rows}

    def fills(self, orders: Mapping[int, m.Order]) -> None:
        if not orders:
            return
        rows = list(
            self.s.execute(
                select(m.Fill)
                .where(m.Fill.run_id == self.run_id, m.Fill.order_id.in_(list(orders)))
                .order_by(m.Fill.ts, m.Fill.id)
            ).scalars()
        )
        for f in rows:
            o = orders[f.order_id]
            snap: Mapping[str, Any] = f.quote_snapshot if isinstance(f.quote_snapshot, dict) else {}
            planned, planned_source = planned_price(o, snap)
            diff = None
            if planned is not None:
                diff = f.price - planned if o.side == "buy" else planned - f.price
            quote_age = None
            if snap.get("time") and snap.get("evaluated_at"):
                try:
                    quote_age = _seconds(
                        datetime.fromisoformat(snap["time"]), datetime.fromisoformat(snap["evaluated_at"])
                    )
                except (TypeError, ValueError):
                    quote_age = None
            self.rec(
                "fill",
                "filled",
                f.ts,
                source_id=f.id,
                symbol_id=o.symbol_id,
                ref={"fill_id": f.id, "order_id": o.id},
                data={
                    "purpose": o.purpose,
                    "side": o.side,
                    "order_type": o.order_type,
                    "qty": f.qty,
                    "planned_price": _s(planned),
                    "planned_source": planned_source,
                    "fill_price": str(f.price),
                    "diff_per_share": _s(diff),
                    "diff_total": _s(None if diff is None else (diff * f.qty).quantize(Q4, ROUND_HALF_UP)),
                    "slippage": str(f.slippage),
                    "fees": f.fees,
                    "quote": {
                        k: snap.get(k)
                        for k in ("source", "bid", "ask", "last", "time", "open", "half_spread")
                    },
                    "quote_age_seconds": quote_age,
                    "seconds_to_fill": _seconds(o.submitted_at, f.ts),
                },
            )

    def exits(self) -> None:
        trades = list(
            self.s.execute(
                select(m.Trade)
                .where(m.Trade.run_id == self.run_id, m.Trade.session_date == self.d)
                .order_by(m.Trade.closed_at, m.Trade.id)
            ).scalars()
        )
        if not trades:
            return
        self.load_tickers([t.symbol_id for t in trades])
        positions = {
            p.id: p
            for p in self.s.execute(
                select(m.Position).where(m.Position.id.in_([t.position_id for t in trades]))
            ).scalars()
        }
        for t in trades:
            exit_order = self.s.execute(
                select(m.Order)
                .where(
                    m.Order.run_id == self.run_id,
                    m.Order.position_id == t.position_id,
                    m.Order.purpose.in_(("stop", "exit")),
                    m.Order.status == "filled",
                )
                .order_by(m.Order.closed_at.desc(), m.Order.id.desc())
                .limit(1)
            ).scalar_one_or_none()
            expiry = False
            if exit_order is not None and exit_order.proposal_id is not None:
                decided_by = self.s.execute(
                    select(m.Proposal.decided_by).where(m.Proposal.id == exit_order.proposal_id)
                ).scalar_one_or_none()
                expiry = decided_by == AUTO_FLATTEN_ACTOR
            category = exit_category(t.exit_reason, expiry=expiry)
            pos = positions.get(t.position_id)
            self.rec(
                "exit",
                "exited",
                t.closed_at,
                source_id=t.id,
                symbol_id=t.symbol_id,
                rule=category,
                reason=t.exit_reason,
                ref={"trade_id": t.id, "position_id": t.position_id}
                | ({"order_id": exit_order.id} if exit_order is not None else {}),
                data={
                    "exit_category": category,
                    "entry_price": str(t.entry_price),
                    "exit_price": str(t.exit_price),
                    "qty": t.qty,
                    "pnl": str(t.pnl),
                    "pnl_r": _s(t.pnl_r),
                    "planned_risk": _s(t.planned_risk),
                    "fees": str(t.fees_total),
                    "slippage": str(t.slippage_total),
                    "minutes_held": _minutes(t.opened_at, t.closed_at),
                    "unprotected_seconds": None if pos is None else pos.unprotected_seconds,
                },
            )

    # --- overlay and kill switches --------------------------------------------------------------------------
    def overlay(self) -> None:
        for e in self.s.execute(
            select(m.EventLog)
            .where(
                m.EventLog.source == OVERLAY_SOURCE,
                m.EventLog.run_id == self.run_id,
                m.EventLog.ts >= self.start,
                m.EventLog.ts < self.end,
            )
            .order_by(m.EventLog.id)
        ).scalars():
            data = e.data if isinstance(e.data, dict) else {}
            decision = data.get("decision")
            if not isinstance(decision, str):
                continue
            benchmark = data.get("benchmark")
            self.rec(
                "overlay",
                "info",
                e.ts,
                source_id=e.id,
                strategy_key="spy_overlay",
                ticker=str(benchmark) if benchmark else None,
                rule=decision,
                reason=e.message,
                ref={"event_id": e.id},
                data=data,
            )

    def kill_switches(self) -> None:
        rows = self.s.execute(
            select(m.KillSwitchEvent)
            .where(
                m.KillSwitchEvent.run_id == self.run_id,
                (m.KillSwitchEvent.session_date == self.d)
                | ((m.KillSwitchEvent.reset_at >= self.start) & (m.KillSwitchEvent.reset_at < self.end)),
            )
            .order_by(m.KillSwitchEvent.id)
        ).scalars()
        for k in rows:
            base = {"value": _s(k.value), "threshold": _s(k.threshold), "session_date": k.session_date}
            if k.session_date == self.d:
                self.rec(
                    "kill_switch",
                    "tripped",
                    k.tripped_at,
                    source_id=k.id,
                    rule=k.switch,
                    ref={"kill_switch_event_id": k.id},
                    data=base,
                )
            if k.reset_at is not None and self.start <= k.reset_at < self.end:
                self.rec(
                    "kill_switch",
                    "reset",
                    k.reset_at,
                    source_id=k.id,
                    rule=k.switch,
                    reason=k.reset_reason,
                    ref={"kill_switch_event_id": k.id},
                    data=base | {"reset_by": k.reset_by},
                )


def planned_price(o: m.Order, snap: Mapping[str, Any]) -> tuple[Decimal | None, str | None]:
    """The price the order was planned at: its stop (stop and stop-limit orders), its limit, or for a market
    order the reference price it filled from before costs: the ask (buy) or bid (sell) of a live quote, or
    the bar's open in a replay (`candle_1m`)."""
    if o.order_type in ("stop", "stop_limit") and o.stop_price is not None:
        return o.stop_price, "stop"
    if o.order_type == "limit" and o.limit_price is not None:
        return o.limit_price, "limit"
    if snap.get("source") == "candle_1m":
        return dec(snap.get("open")), "candle_open"
    key = "ask" if o.side == "buy" else "bid"
    return dec(snap.get(key)), f"quote_{key}"


__all__ = [
    "EXIT_CATEGORIES",
    "LiveScanData",
    "cap_data",
    "exit_category",
    "fingerprint",
    "lock_key",
    "mask",
    "planned_price",
    "record_day",
    "write_locked",
]
