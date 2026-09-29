"""The activity feed and the rejections panel (live dashboard plan S7, S8).

`activity_feed` merges one typed, plain-text item per stored event of the live run in the ET day of the
dashboard's session day: orders placed and cancelled, fills, exits, proposals (created, decided, expired),
kill-switch trips and resets, failed jobs, error events and one line for the 9:35 scan. Each source is one
bounded query (the run and the day's ET bounds), so the statement count does not grow with the rows.

`rejections` counts the day's rejections by (stage, rule) from the decision log (scan and risk stages), using
the scan summary's counts when the scan was recorded as a summary only, and the `candidates` table while the
decision log has no scan rows yet (it refreshes every 60 s and pauses around 9:35).

Read-only; every free-form stored text (reasons, errors, event messages) passes `redact_text`; times in texts
are in the configured display zone (`EnvSettings.tz_display`, Mountain Time: the web shows MT, never
UTC), which the route passes as `tz`.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, aliased, sessionmaker

from trader.api import views as api_views
from trader.api.feed import live_or_unscoped
from trader.api.livedata import periods
from trader.api.livedata.types import ACTIVITY_LIMIT, REJECTION_TICKERS_MAX
from trader.api.schemas import (
    ActivityChip,
    ActivityItemOut,
    ActivityKind,
    ActivityTone,
    RejectionRuleOut,
    RejectionSource,
    RejectionsOut,
)
from trader.config import EnvSettings
from trader.db import models as m
from trader.engine.proposals import AUTO_FLATTEN_ACTOR
from trader.logging_setup import redact_text
from trader.notify.messages import fmt_price

# The zone when the caller passes none: `tz_display`'s default (the route passes the configured one).
DEFAULT_DISPLAY_TZ = ZoneInfo(str(EnvSettings.model_fields["tz_display"].default))
JOB_ERROR_CHARS = 120
ALERT_CHARS = 160
REASON_CHARS = 160
ALERT_LEVELS = ("error", "critical")
LOG_MIRROR_PREFIX = "log."  # the log mirror's events live on the Control page, not in the feed
SCAN_TOP = 3
REJECTION_STAGES = ("scan", "risk")  # the signal stage repeats the risk rejection, so it is not counted
UNKNOWN_RULE = "unknown"
PCT_SWITCHES = frozenset({"daily_loss_pct", "max_drawdown_pct"})

_CENT = Decimal("0.01")
_TENTH = Decimal("0.1")

# Same-instant items read in lifecycle order (newest first means the later step is listed first).
_STEP: Mapping[ActivityKind, int] = {
    "scan": 0,
    "proposal_created": 1,
    "proposal_approved": 2,
    "proposal_rejected": 2,
    "proposal_expired": 2,
    "order_placed": 3,
    "fill": 4,
    "order_cancelled": 5,
    "exit": 6,
    "kill_switch_tripped": 7,
    "kill_switch_reset": 8,
    "job_failed": 9,
    "alert": 10,
}
_CHIP: Mapping[ActivityKind, ActivityChip] = {
    "order_placed": "trades",
    "order_cancelled": "trades",
    "fill": "trades",
    "exit": "trades",
    "proposal_created": "proposals",
    "proposal_approved": "proposals",
    "proposal_rejected": "proposals",
    "proposal_expired": "proposals",
    "kill_switch_tripped": "alerts",
    "kill_switch_reset": "alerts",
    "job_failed": "alerts",
    "alert": "alerts",
    "scan": "scan",
}
_ORDER_TYPES = {"market": "market", "limit": "limit", "stop": "stop", "stop_limit": "stop limit"}


@dataclass(frozen=True, slots=True)
class _Item:
    ts: datetime
    kind: ActivityKind
    row_id: int
    text: str
    ticker: str | None = None
    amount: Decimal | None = None
    tone: ActivityTone = "neutral"
    link: str | None = None

    def out(self) -> ActivityItemOut:
        return ActivityItemOut(
            id=f"{self.kind}:{self.row_id}",
            ts=self.ts,
            kind=self.kind,
            chip=_CHIP[self.kind],
            ticker=self.ticker,
            text=self.text,
            amount=self.amount,
            tone=self.tone,
            link=self.link,
        )


# --- formatting ---------------------------------------------------------------------------------------------


def _clean(text: str | None, chars: int = REASON_CHARS) -> str:
    return redact_text(text or "").strip()[:chars]


def _mt(ts: datetime, tz: ZoneInfo) -> str:
    return ts.astimezone(tz).strftime("%H:%M")


def _signed(value: Decimal) -> str:
    q = value.quantize(_CENT, ROUND_HALF_UP)
    return f"+{q}" if q > 0 else f"{q}"


def _position_link(position_id: int | None) -> str | None:
    return f"/trades?position={position_id}" if position_id is not None else None


def _proposal_link(proposal_id: int) -> str:
    return f"/dashboard?proposal={proposal_id}"


def _order_price(o: m.Order) -> str:
    if o.order_type == "stop_limit" and o.stop_price is not None and o.limit_price is not None:
        return f" @ {fmt_price(o.stop_price)} limit {fmt_price(o.limit_price)}"
    price = o.stop_price if o.order_type in ("stop", "stop_limit") else o.limit_price
    return f" @ {fmt_price(price)}" if price is not None and o.order_type != "market" else ""


def _switch_value(switch: str, value: Decimal) -> str:
    if switch in PCT_SWITCHES:
        return f"{(value * 100).quantize(_TENTH, ROUND_HALF_UP)}%"
    return f"{value.quantize(_CENT, ROUND_HALF_UP)} R"


def _words(*parts: str | None) -> str:
    return " ".join(p for p in parts if p)


# --- the sources (one query each) ---------------------------------------------------------------------------


def _orders(s: Session, run_id: int, start: datetime, end: datetime) -> Iterable[_Item]:
    placed = (m.Order.submitted_at >= start) & (m.Order.submitted_at < end)
    cancelled = (m.Order.status == "cancelled") & (m.Order.closed_at >= start) & (m.Order.closed_at < end)
    for o, ticker in s.execute(
        select(m.Order, m.Symbol.ticker)
        .join(m.Symbol, m.Symbol.id == m.Order.symbol_id)
        .where(m.Order.run_id == run_id, or_(placed, cancelled))
    ).tuples():
        link = _position_link(o.position_id)
        if start <= o.submitted_at < end:
            type_text = _ORDER_TYPES.get(o.order_type, o.order_type)
            text = f"{o.side.capitalize()} {type_text} {o.qty} {ticker}{_order_price(o)} ({o.purpose})"
            yield _Item(o.submitted_at, "order_placed", o.id, text, ticker, link=link)
        if o.status == "cancelled" and o.closed_at is not None and start <= o.closed_at < end:
            reason = _clean(o.cancel_reason)
            text = f"Cancelled {o.purpose} {ticker}" + (f": {reason}" if reason else "")
            yield _Item(o.closed_at, "order_cancelled", o.id, text, ticker, link=link)


def _fills(s: Session, run_id: int, start: datetime, end: datetime) -> Iterable[_Item]:
    for f, o, ticker in s.execute(
        select(m.Fill, m.Order, m.Symbol.ticker)
        .join(m.Order, m.Order.id == m.Fill.order_id)
        .join(m.Symbol, m.Symbol.id == m.Order.symbol_id)
        .where(m.Fill.run_id == run_id, m.Fill.ts >= start, m.Fill.ts < end)
    ).tuples():
        text = (
            f"Filled {o.side} {f.qty} {ticker} @ {fmt_price(f.price)}, slippage {fmt_price(f.slippage)}/share"
        )
        yield _Item(f.ts, "fill", f.id, text, ticker, amount=f.price, link=_position_link(o.position_id))


def _exits(s: Session, run_id: int, start: datetime, end: datetime) -> Iterable[_Item]:
    for t, ticker in s.execute(
        select(m.Trade, m.Symbol.ticker)
        .join(m.Symbol, m.Symbol.id == m.Trade.symbol_id)
        .where(m.Trade.run_id == run_id, m.Trade.closed_at >= start, m.Trade.closed_at < end)
    ).tuples():
        text = f"Exit {ticker} ({_clean(t.exit_reason)}): {_signed(t.pnl)}"
        if t.pnl_r is not None:
            text += f", {_signed(t.pnl_r)} R"
        tone: ActivityTone = "up" if t.pnl > 0 else "down" if t.pnl < 0 else "neutral"
        yield _Item(t.closed_at, "exit", t.id, text, ticker, t.pnl, tone, _position_link(t.position_id))


def _proposals(s: Session, run_id: int, start: datetime, end: datetime, tz: ZoneInfo) -> Iterable[_Item]:
    def within(col: Any) -> Any:
        return and_(col >= start, col < end)

    p = m.Proposal
    for prop, ticker in s.execute(
        select(p, m.Symbol.ticker)
        .outerjoin(m.Signal, m.Signal.id == p.signal_id)
        .outerjoin(m.Symbol, m.Symbol.id == m.Signal.symbol_id)
        .where(p.run_id == run_id, or_(within(p.created_at), within(p.decided_at), within(p.expired_at)))
    ).tuples():
        link = _proposal_link(prop.id)
        auto_flatten = prop.decided_by == AUTO_FLATTEN_ACTOR
        if start <= prop.created_at < end:
            mode = "auto" if prop.decided_via == "auto" and not auto_flatten else "manual"
            text = _words("Proposal", prop.kind, ticker, f"{prop.qty} sh ({mode})")
            yield _Item(prop.created_at, "proposal_created", prop.id, text, ticker, link=link)
        if (
            prop.decided_at is not None
            and prop.decided_via
            and not auto_flatten
            and start <= prop.decided_at < end
        ):
            at = f"via {_clean(prop.decided_via)} at {_mt(prop.decided_at, tz)} MT"
            error = _clean(prop.error)
            kind: ActivityKind
            tone: ActivityTone
            if prop.status == "rejected":
                kind, verb, tone = "proposal_rejected", "Rejected", "warn"
            elif prop.status == "failed":
                kind, verb, tone = "proposal_rejected", "Failed", "warn"
            else:
                kind, verb, tone = "proposal_approved", "Approved", "neutral"
            text = _words(verb, prop.kind, ticker, at) + (f": {error}" if error and tone == "warn" else "")
            yield _Item(prop.decided_at, kind, prop.id, text, ticker, tone=tone, link=link)
        if prop.expired_at is not None and start <= prop.expired_at < end:
            text = _words("Expired", prop.kind, ticker) + (" (auto-flatten)" if auto_flatten else "")
            yield _Item(prop.expired_at, "proposal_expired", prop.id, text, ticker, tone="warn", link=link)


def _kill_switches(s: Session, run_id: int, start: datetime, end: datetime) -> Iterable[_Item]:
    k = m.KillSwitchEvent
    for ev in s.execute(
        select(k).where(
            k.run_id == run_id,
            or_(and_(k.tripped_at >= start, k.tripped_at < end), and_(k.reset_at >= start, k.reset_at < end)),
        )
    ).scalars():
        label = api_views.KILLSWITCH_LABELS.get(ev.switch, ev.switch).lower()
        if start <= ev.tripped_at < end:
            text = f"Kill switch {label} tripped"
            if ev.value is not None and ev.threshold is not None:
                text += f": {_switch_value(ev.switch, ev.value)} vs {_switch_value(ev.switch, ev.threshold)}"
            yield _Item(ev.tripped_at, "kill_switch_tripped", ev.id, text, tone="warn", link="/control")
        if ev.reset_at is not None and start <= ev.reset_at < end:
            reason = _clean(ev.reset_reason)
            text = _words(f"Kill switch {label} reset", f"by {_clean(ev.reset_by)}" if ev.reset_by else None)
            text += f": {reason}" if reason else ""
            yield _Item(ev.reset_at, "kill_switch_reset", ev.id, text, link="/control")


def _job_failures(s: Session, start: datetime, end: datetime, limit: int) -> Iterable[_Item]:
    j, earlier = m.JobRun, aliased(m.JobRun)
    attempt = (
        select(func.count())
        .select_from(earlier)
        .where(
            earlier.job == j.job,
            earlier.session_date == j.session_date,
            or_(
                earlier.started_at < j.started_at,
                and_(earlier.started_at == j.started_at, earlier.id <= j.id),
            ),
        )
        .scalar_subquery()
    )
    for run, n in s.execute(
        select(j, attempt)
        .where(j.status == "failed", j.finished_at >= start, j.finished_at < end)
        .order_by(j.finished_at.desc(), j.id.desc())
        .limit(limit)
    ).tuples():
        assert run.finished_at is not None  # the filter above
        error = _clean(run.error, JOB_ERROR_CHARS)
        text = f"{run.job} failed (attempt {n})" + (f": {error}" if error else "")
        yield _Item(run.finished_at, "job_failed", run.id, text, tone="warn", link="/control")


def _alerts(s: Session, start: datetime, end: datetime, limit: int) -> Iterable[_Item]:
    e = m.EventLog
    for ev in s.execute(
        select(e)
        .where(
            e.level.in_(ALERT_LEVELS),
            live_or_unscoped(e.run_id),  # never a replay's event
            ~e.source.startswith(LOG_MIRROR_PREFIX, autoescape=True),
            e.ts >= start,
            e.ts < end,
        )
        .order_by(e.ts.desc(), e.id.desc())
        .limit(limit)
    ).scalars():
        text = f"{_clean(ev.source, ALERT_CHARS)}: {_clean(ev.message, ALERT_CHARS)}"
        yield _Item(ev.ts, "alert", ev.id, text, tone="warn", link="/control")


def _scan(s: Session, run_id: int, day: date, tz: ZoneInfo) -> Iterable[_Item]:
    c = m.Candidate
    of_day = (c.run_id == run_id, c.session_date == day)
    total, passed, first_at, first_id = s.execute(
        select(func.count(), func.count().filter(c.passed), func.min(c.created_at), func.min(c.id)).where(
            *of_day
        )
    ).one()
    if not total or first_at is None or first_id is None:
        return
    top = s.execute(
        select(m.Symbol.ticker)
        .select_from(c)
        .join(m.Symbol, m.Symbol.id == c.symbol_id)
        .where(*of_day, c.passed)
        .order_by(c.rank.asc().nulls_last(), c.id)
        .limit(SCAN_TOP)
    ).scalars()
    text = f"{_mt(first_at, tz)} MT scan: {total} → {passed} passed"
    names = ", ".join(top)
    text += f" → {names}" if names else ""
    yield _Item(first_at, "scan", int(first_id), text, link=f"/reports?day={day.isoformat()}")


def activity_feed(
    factory: sessionmaker[Session],
    run_id: int,
    day: date,
    *,
    limit: int = ACTIVITY_LIMIT,
    tz: ZoneInfo = DEFAULT_DISPLAY_TZ,
) -> list[ActivityItemOut]:
    """Newest first, at most `limit` items whose time falls in the ET day of `day`; times in texts in `tz`."""
    start, end = periods.et_day_bounds(day)
    with factory() as s:
        items = [
            *_orders(s, run_id, start, end),
            *_fills(s, run_id, start, end),
            *_exits(s, run_id, start, end),
            *_proposals(s, run_id, start, end, tz),
            *_kill_switches(s, run_id, start, end),
            *_job_failures(s, start, end, limit),
            *_alerts(s, start, end, limit),
            *_scan(s, run_id, day, tz),
        ]
    items.sort(key=lambda i: (i.ts, _STEP[i.kind], i.row_id), reverse=True)
    return [i.out() for i in items[: max(limit, 0)]]


# --- rejections ---------------------------------------------------------------------------------------------


@dataclass(slots=True)
class _Rule:
    stage: str
    rule: str
    count: int
    tickers: list[str]


def _rule_of(value: object) -> str:
    return str(value) if value else UNKNOWN_RULE


def _tickers(values: Iterable[str | None]) -> list[str]:
    return sorted({t for t in values if t})


def _summary_scan_counts(rows: Iterable[Any]) -> dict[str, int] | None:
    """The scan counts of a summary-only scan (`scan_detail` other than `all`), else None."""
    out: dict[str, int] | None = None
    for data in rows:
        if not isinstance(data, Mapping) or data.get("scan_detail") == "all":
            continue
        counts = data.get("counts")
        rejects = counts.get("rejects_by_rule") if isinstance(counts, Mapping) else None
        if not isinstance(rejects, Mapping):
            continue
        out = out if out is not None else {}
        for rule, n in rejects.items():
            if isinstance(n, int) and not isinstance(n, bool) and n > 0:
                out[_rule_of(rule)] = out.get(_rule_of(rule), 0) + n
    return out


def rejections(factory: sessionmaker[Session], run_id: int, day: date) -> RejectionsOut:
    d = m.DecisionLog
    of_day = (d.run_id == run_id, d.session_date == day)
    rule_col = func.coalesce(func.nullif(d.rule, ""), UNKNOWN_RULE)
    with factory() as s:
        rows, scan_rows, recorded_at, final = s.execute(
            select(
                func.count(),
                func.count().filter(d.stage == "scan"),
                func.max(d.recorded_at),
                func.coalesce(func.bool_or(d.final), False),
            ).where(*of_day)
        ).one()
        rules = [
            _Rule(stage, rule, int(n), _tickers(tickers or ()))
            for stage, rule, n, tickers in s.execute(
                select(
                    d.stage, rule_col, func.count(), func.array_agg(func.coalesce(d.ticker, m.Symbol.ticker))
                )
                .select_from(d)
                .outerjoin(m.Symbol, m.Symbol.id == d.symbol_id)
                .where(
                    *of_day, d.stage.in_(REJECTION_STAGES), d.outcome == "rejected", d.symbol_id.is_not(None)
                )
                .group_by(d.stage, rule_col)
            ).tuples()
        ]
        source: RejectionSource = "decision_log" if rows else "none"
        if scan_rows:
            summary = _summary_scan_counts(
                s.execute(select(d.data).where(*of_day, d.stage == "scan", d.symbol_id.is_(None))).scalars()
            )
            if summary is not None:
                rules = [r for r in rules if r.stage != "scan"]
                rules += [_Rule("scan", rule, n, []) for rule, n in summary.items()]
        else:
            c = m.Candidate
            reason = func.coalesce(func.nullif(c.reject_reason, ""), UNKNOWN_RULE)
            cand_of_day = (c.run_id == run_id, c.session_date == day)
            if s.execute(select(func.count()).select_from(c).where(*cand_of_day)).scalar_one():
                source = "candidates"
                rules += [
                    _Rule("scan", rule, int(n), _tickers(tickers or ()))
                    for rule, n, tickers in s.execute(
                        select(reason, func.count(), func.array_agg(m.Symbol.ticker))
                        .select_from(c)
                        .outerjoin(m.Symbol, m.Symbol.id == c.symbol_id)
                        .where(*cand_of_day, ~c.passed)
                        .group_by(reason)
                    ).tuples()
                ]
    rules.sort(key=lambda r: (-r.count, r.stage, r.rule))
    base = f"/reports?day={day.isoformat()}"
    return RejectionsOut(
        session_date=day,
        source=source,
        total=sum(r.count for r in rules),
        rules=[
            RejectionRuleOut(
                stage=r.stage,  # a DecisionStage (the table's CHECK); pydantic validates it
                rule=r.rule,
                count=r.count,
                tickers=r.tickers[:REJECTION_TICKERS_MAX],
                truncated=len(r.tickers) > REJECTION_TICKERS_MAX,
                link=f"{base}&stage={r.stage}&outcome=rejected",
            )
            for r in rules
        ],
        final=bool(final) if rows else False,
        recorded_at=recorded_at if rows else None,
    )
