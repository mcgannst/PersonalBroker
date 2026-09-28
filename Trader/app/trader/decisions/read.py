"""Reads of the decision log for the API and the CLI (P6-T12).

`resolve_run(None)` is the active live run (else the latest live run); an explicit id of a live or replay run
is that run; an unknown id is None.

**Replay isolation.** Without an explicit run id only live runs are ever served (the `live_or_unscoped`
rule: rows of an older, non-active live run are still live rows): `list_days(run_id=None)` lists the days of
every live run, and `load_day(None, D)` serves the resolved live run's day D, or, when it has none, the
newest live run that has rows for D. A replay's rows need its explicit `run_id`.

`load_day` orders rows by `seq`, filters by stage, outcome and ticker (case-insensitive), and takes the
summary from the day's `day` row whatever the filters. The `day` row's `data` holds the `DaySummary` fields
(as the recorder serialises them: Decimals as strings, rule counts as `[rule, count]` pairs, `approvals` a
mapping) plus `text` (the summary line); a summary nested under `data["summary"]` is read the same way. A
`day` row that can't be read as a summary gives `summary = None` (its `text` is still returned); a day
without a `day` row is final only when every row is.
"""

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, cast

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session, sessionmaker

from trader.db import models as m
from trader.decisions.types import (
    DayItem,
    DaySummary,
    DayView,
    DecisionOutcome,
    DecisionRowView,
    DecisionStage,
)

LIVE = "live"
APPROVAL_KEYS = ("manual", "auto", "declined", "expired", "blocked")
_INT_FIELDS = (
    "premarket_listed",
    "premarket_classified",
    "scanned",
    "rvol_passed",
    "ranked",
    "passed",
    "signals",
    "proposals",
    "fills",
    "trades",
    "wins",
    "losses",
)


def resolve_run(factory: sessionmaker[Session], run_id: int | None) -> tuple[int, str] | None:
    """`(id, mode)` of the run to serve: an explicit id if it exists, else the active live run, else the
    latest live run; None when there is none."""
    with factory() as s:
        return _resolve(s, run_id)


def _resolve(s: Session, run_id: int | None) -> tuple[int, str] | None:
    if run_id is not None:
        run = s.get(m.Run, run_id)
        return None if run is None else (run.id, run.mode)
    row = s.execute(
        select(m.Run.id)
        .where(m.Run.mode == LIVE)
        .order_by((m.Run.status == "active").desc(), m.Run.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    return None if row is None else (row, LIVE)


def pick_run(s: Session, run_id: int | None, session_date: date) -> tuple[int, str] | None:
    """The run whose day `session_date` is served: an explicit run (any mode) as given; without one the
    resolved live run when it has rows for the day, else the newest live run that has."""
    resolved = _resolve(s, run_id)
    if run_id is not None or (resolved is not None and has_rows(s, resolved[0], session_date)):
        return resolved
    other = s.execute(
        select(m.DecisionLog.run_id)
        .join(m.Run, m.Run.id == m.DecisionLog.run_id)
        .where(m.Run.mode == LIVE, m.DecisionLog.session_date == session_date)
        .order_by(m.DecisionLog.run_id.desc())
        .limit(1)
    ).scalar_one_or_none()
    return None if other is None else (other, LIVE)


def has_rows(s: Session, run_id: int, session_date: date) -> bool:
    found = s.execute(
        select(m.DecisionLog.id)
        .where(m.DecisionLog.run_id == run_id, m.DecisionLog.session_date == session_date)
        .limit(1)
    ).first()
    return found is not None


def list_days(factory: sessionmaker[Session], *, run_id: int | None, limit: int = 30) -> list[DayItem]:
    """The newest `limit` days with rows: of the given run, or (None) of every live run."""
    d = m.DecisionLog
    stmt = select(d.run_id, d.session_date, func.bool_and(d.final)).group_by(d.run_id, d.session_date)
    if run_id is None:
        stmt = stmt.join(m.Run, m.Run.id == d.run_id).where(m.Run.mode == LIVE)
    else:
        stmt = stmt.where(d.run_id == run_id)
    stmt = stmt.order_by(d.session_date.desc(), d.run_id.desc()).limit(limit)
    with factory() as s:
        keys = [(r, day, bool(final)) for r, day, final in s.execute(stmt)]
        items: list[DayItem] = []
        for r, day, all_final in keys:
            day_row = _day_row(s, r, day)
            data = _mapping(day_row.data) if day_row is not None else {}
            src = _summary_source(data)
            items.append(
                DayItem(
                    run_id=r,
                    session_date=day,
                    final=day_row.final if day_row is not None else all_final,
                    summary_text=_text(data),
                    proposals=_int(src.get("proposals")) or 0,
                    trades=_int(src.get("trades")) or 0,
                )
            )
    return items


def load_day(
    factory: sessionmaker[Session],
    run_id: int | None,
    session_date: date,
    *,
    stage: DecisionStage | None = None,
    outcome: DecisionOutcome | None = None,
    ticker: str | None = None,
    limit: int = 2000,
    offset: int = 0,
) -> DayView | None:
    """One run's day (see the module docstring for which run); None when that day has no rows."""
    d = m.DecisionLog
    with factory() as s:
        picked = pick_run(s, run_id, session_date)
        if picked is None:
            return None
        rid, mode = picked
        day = (d.run_id == rid, d.session_date == session_date)
        count, all_final, recorded_at = s.execute(
            select(func.count(), func.bool_and(d.final), func.max(d.recorded_at)).where(*day)
        ).one()
        if not count:
            return None
        where = list(day)
        if stage is not None:
            where.append(d.stage == stage)
        if outcome is not None:
            where.append(d.outcome == outcome)
        if ticker:
            where.append(func.upper(d.ticker) == ticker.strip().upper())
        total = s.execute(select(func.count()).where(*where)).scalar_one()
        rows = s.scalars(select(d).where(*where).order_by(d.seq).limit(limit).offset(offset)).all()
        day_row = _day_row(s, rid, session_date)
        final = day_row.final if day_row is not None else bool(all_final)
        data = _mapping(day_row.data) if day_row is not None else {}
        return DayView(
            run_id=rid,
            run_mode=mode,
            session_date=session_date,
            final=final,
            recorded_at=cast(datetime | None, recorded_at),
            summary=summary_from_data(data, run_id=rid, session_date=session_date, final=final)
            if day_row is not None
            else None,
            summary_text=_text(data),
            rows=tuple(row_view(r) for r in rows),
            total=int(total),
        )


def _day_row(s: Session, run_id: int, session_date: date) -> m.DecisionLog | None:
    d = m.DecisionLog
    stmt: Select[tuple[m.DecisionLog]] = (
        select(d)
        .where(d.run_id == run_id, d.session_date == session_date, d.stage == "day")
        .order_by(d.seq.desc())
        .limit(1)
    )
    return s.scalars(stmt).one_or_none()


def row_view(r: m.DecisionLog) -> DecisionRowView:
    return DecisionRowView(
        id=r.id,
        run_id=r.run_id,
        session_date=r.session_date,
        seq=r.seq,
        stage=cast(DecisionStage, r.stage),
        strategy_key=r.strategy_key,
        symbol_id=r.symbol_id,
        ticker=r.ticker,
        outcome=cast(DecisionOutcome, r.outcome),
        rule=r.rule,
        reason=r.reason,
        ts=r.ts,
        ref=_mapping(r.ref),
        data=_mapping(r.data),
        recorded_at=r.recorded_at,
        final=r.final,
    )


# --- the day row's summary ----------------------------------------------------------------------------------
class _Unreadable(ValueError):
    pass


def summary_from_data(
    data: Mapping[str, Any], *, run_id: int, session_date: date, final: bool
) -> DaySummary | None:
    """The `DaySummary` stored in a `day` row's `data`, or None when it can't be read."""
    src = _summary_source(data)
    if "scanned" not in src:
        return None
    try:
        ints = {name: _strict_int(src.get(name, 0)) for name in _INT_FIELDS}
        approvals = src.get("approvals") or {}
        if not isinstance(approvals, Mapping):
            raise _Unreadable("approvals")
        median = src.get("median_decision_seconds")
        return DaySummary(
            run_id=run_id,
            session_date=session_date,
            final=final,
            universe_size=_opt_int(src.get("universe_size")),
            universe_source=_opt_str(src.get("universe_source")),
            rejects_by_rule=_pairs(src.get("rejects_by_rule")),
            risk_rejections=_pairs(src.get("risk_rejections")),
            approvals={str(k): _strict_int(v) for k, v in approvals.items()},
            median_decision_seconds=None if median is None else float(median),
            avg_fill_diff_per_share=_opt_decimal(src.get("avg_fill_diff_per_share")),
            pnl=_opt_decimal(src.get("pnl")) or Decimal(0),
            pnl_r=_opt_decimal(src.get("pnl_r")),
            exits_by_reason=_pairs(src.get("exits_by_reason")),
            notes=tuple(str(n) for n in _seq(src.get("notes"))),
            **ints,
        )
    except (_Unreadable, TypeError, ValueError, InvalidOperation):
        return None


def _summary_source(data: Mapping[str, Any]) -> Mapping[str, Any]:
    nested = data.get("summary")
    return nested if isinstance(nested, Mapping) else data


def _text(data: Mapping[str, Any]) -> str | None:
    for key in ("text", "summary_text"):
        value = data.get(key)
        if isinstance(value, str):
            return value
    return None


def _pairs(value: Any) -> tuple[tuple[str, int], ...]:
    """Rule counts from `[[rule, n], ...]`, `[{"rule", "count"}, ...]` or `{rule: n}`, in stored order."""
    if value is None:
        return ()
    if isinstance(value, Mapping):
        return tuple((str(k), _strict_int(v)) for k, v in value.items())
    out: list[tuple[str, int]] = []
    for item in _seq(value):
        if isinstance(item, Mapping):
            out.append((str(item["rule"]), _strict_int(item["count"])))
        elif isinstance(item, Sequence) and not isinstance(item, str) and len(item) == 2:
            out.append((str(item[0]), _strict_int(item[1])))
        else:
            raise _Unreadable("pair")
    return tuple(out)


def _seq(value: Any) -> Sequence[Any]:
    if value is None:
        return ()
    if isinstance(value, Sequence) and not isinstance(value, str):
        return value
    raise _Unreadable("sequence")


def _strict_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _Unreadable("int")
    return value


def _opt_int(value: Any) -> int | None:
    return None if value is None else _strict_int(value)


def _int(value: Any) -> int | None:
    """An int for the day list (lenient: anything else is None)."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _opt_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _opt_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise _Unreadable("decimal")
    out = Decimal(str(value))
    if not out.is_finite():
        raise _Unreadable("decimal")
    return out


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}
