"""Decision log routes (P6-T12; SPEC §11): browse a day's decisions and download them as CSV.

- `GET /api/decisions/days?limit=30&run_id=` -> `DecisionDaysOut`: the newest days with rows.
- `GET /api/decisions?date=YYYY-MM-DD&run_id=&stage=&outcome=&ticker=&limit=2000&offset=0` ->
  `DecisionDayOut`: one run's day, rows in `seq` order, each row's `checks` lifted out of its `data`, and
  the summary from the day's `day` row; 404 `not_found` when the day has no rows; 422 for a bad stage or
  outcome, or a limit over 5,000.
- `GET /api/export/decisions.csv?date=&run_id=` -> `text/csv`, attachment
  `decisions-<date>-run<id>.csv` (`trader.decisions.export.decisions_csv`, streamed and closed as soon as
  the response ends); 404 when the day has no rows.

**Replay isolation.** Without `run_id` only a live run is ever served (`trader.decisions.read`); a replay's
decisions need its explicit `run_id`. An unknown `run_id` is 404. Sync routes: FastAPI runs them in a worker
thread, so the database reads stay off the event loop. Every route needs the session cookie (GET only, so no
CSRF token).

A stored check that doesn't fit `CheckOut` (an unknown op, not an object) and a non-integer `ref` value are
left out of the response rather than failing the whole day.
"""

from collections.abc import Mapping
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse
from pydantic import ValidationError

from trader.api.deps import ApiServices, CurrentUser, Services
from trader.api.errors import ApiError
from trader.api.routers.performance import close_when_done
from trader.api.schemas import (
    CheckOut,
    DecisionDayItemOut,
    DecisionDayOut,
    DecisionDaysOut,
    DecisionRowOut,
    DecisionSummaryOut,
    RuleCountOut,
)
from trader.decisions import read
from trader.decisions.export import decisions_csv
from trader.decisions.types import DaySummary, DecisionOutcome, DecisionRowView, DecisionStage

router = APIRouter(tags=["decisions"])

MAX_ROWS = 5000
MAX_DAYS = 400
MAX_OFFSET = 2**31 - 1  # far past any day (≈1,000 rows), well inside bigint: a larger one is 422, not a 500

RunIdQuery = Annotated[int | None, Query(ge=1, le=2**63 - 1)]


def _unknown_run() -> ApiError:
    return ApiError(404, "not_found", "Unknown run")


def _no_day() -> ApiError:
    return ApiError(404, "not_found", "No decisions recorded for that day")


def checks_out(raw: Any) -> list[CheckOut]:
    if not isinstance(raw, list):
        return []
    out: list[CheckOut] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        try:
            out.append(CheckOut.model_validate(dict(item)))
        except ValidationError:
            continue
    return out


def row_out(r: DecisionRowView) -> DecisionRowOut:
    data = dict(r.data)
    checks = checks_out(data.pop("checks", None))
    ref = {str(k): v for k, v in r.ref.items() if isinstance(v, int) and not isinstance(v, bool)}
    return DecisionRowOut(
        seq=r.seq,
        stage=r.stage,
        strategy_key=r.strategy_key,
        symbol_id=r.symbol_id,
        ticker=r.ticker,
        outcome=r.outcome,
        rule=r.rule,
        reason=r.reason,
        ts=r.ts,
        ref=ref,
        checks=checks,
        data=data,
    )


def _counts(pairs: tuple[tuple[str, int], ...]) -> list[RuleCountOut]:
    return [RuleCountOut(rule=rule, count=count) for rule, count in pairs]


def summary_out(s: DaySummary, text: str | None) -> DecisionSummaryOut:
    return DecisionSummaryOut(
        text=text or "",
        universe_size=s.universe_size,
        premarket_listed=s.premarket_listed,
        premarket_classified=s.premarket_classified,
        scanned=s.scanned,
        rvol_passed=s.rvol_passed,
        ranked=s.ranked,
        passed=s.passed,
        rejects_by_rule=_counts(s.rejects_by_rule),
        signals=s.signals,
        risk_rejections=_counts(s.risk_rejections),
        proposals=s.proposals,
        approvals=dict(s.approvals),
        median_decision_seconds=s.median_decision_seconds,
        fills=s.fills,
        avg_fill_diff_per_share=s.avg_fill_diff_per_share,
        trades=s.trades,
        wins=s.wins,
        losses=s.losses,
        pnl=s.pnl,
        pnl_r=s.pnl_r,
        exits_by_reason=_counts(s.exits_by_reason),
        notes=list(s.notes),
    )


def _check_run(services: ApiServices, run_id: int | None) -> None:
    if run_id is not None and read.resolve_run(services.core.factory, run_id) is None:
        raise _unknown_run()


@router.get("/decisions/days")
def decision_days(
    _user: CurrentUser,
    services: Services,
    limit: Annotated[int, Query(ge=1, le=MAX_DAYS)] = 30,
    run_id: RunIdQuery = None,
) -> DecisionDaysOut:
    _check_run(services, run_id)
    items = read.list_days(services.core.factory, run_id=run_id, limit=limit)
    return DecisionDaysOut(
        days=[
            DecisionDayItemOut(
                run_id=i.run_id,
                session_date=i.session_date,
                final=i.final,
                summary_text=i.summary_text,
                proposals=i.proposals,
                trades=i.trades,
            )
            for i in items
        ]
    )


@router.get("/decisions")
def decision_day(
    _user: CurrentUser,
    services: Services,
    session_date: Annotated[date, Query(alias="date")],
    run_id: RunIdQuery = None,
    stage: DecisionStage | None = None,
    outcome: DecisionOutcome | None = None,
    ticker: Annotated[str | None, Query(max_length=20)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_ROWS)] = 2000,
    offset: Annotated[int, Query(ge=0, le=MAX_OFFSET)] = 0,
) -> DecisionDayOut:
    _check_run(services, run_id)
    view = read.load_day(
        services.core.factory,
        run_id,
        session_date,
        stage=stage,
        outcome=outcome,
        ticker=ticker,
        limit=limit,
        offset=offset,
    )
    if view is None:
        raise _no_day()
    return DecisionDayOut(
        run_id=view.run_id,
        run_mode=view.run_mode,
        session_date=view.session_date,
        final=view.final,
        recorded_at=view.recorded_at,
        summary=summary_out(view.summary, view.summary_text) if view.summary is not None else None,
        rows=[row_out(r) for r in view.rows],
        total=view.total,
    )


@router.get("/export/decisions.csv", response_class=StreamingResponse)
def export_decisions(
    _user: CurrentUser,
    services: Services,
    session_date: Annotated[date, Query(alias="date")],
    run_id: RunIdQuery = None,
) -> StreamingResponse:
    _check_run(services, run_id)
    factory = services.core.factory
    with factory() as s:
        picked = read.pick_run(s, run_id, session_date)
        if picked is None or not read.has_rows(s, picked[0], session_date):
            raise _no_day()
    rid = picked[0]
    name = f"decisions-{session_date.isoformat()}-run{rid}.csv"
    return StreamingResponse(
        close_when_done(decisions_csv(factory, rid, session_date)),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )
