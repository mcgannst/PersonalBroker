"""Replay routes (SPEC §11 `/replays`, §12 Replay; BR-54; P5-T7).

- `GET /api/replays/options`: what the start form needs: the override keys (sorted), `replay.max_sessions`,
  `latest_allowed` (the last session whose close + 15 min has passed), `questrade_from` (the wall-clock ET
  date minus `replay.questrade_window_days`), the first archived 1-minute bar's ET date and the first universe
  snapshot's date (null when none), `busy` (a replay `queued` or `running` once abandoned runs are settled)
  and `offline_now` (a start now runs offline: 09:15-16:30 ET on a session day).
- `GET /api/replays?limit=50` (1-200): replay runs only, newest first, each with its trade count, expectancy
  (mean R of the trades with an R, 4 dp half-up) and total P&L from one grouped query over `trades`.
  Abandoned runs are settled first (`reconcile_abandoned`). A row whose `params` can't be read (missing or
  malformed dates or data mode) is left out of the list and logged, so the list never fails on one bad row.

Both GETs above (`/replays/options` and `/replays`) have a side effect: they call `reconcile_abandoned`,
which (when the replay lock is free) settles every `running` replay and every `queued` one older than two
minutes as `failed` ("abandoned"). That is how a replay whose process died is cleared without a sweeper.
- `GET /api/replays/{id}`: one replay (404 for an unknown id or a live run). `metrics` (the whole replay) and
  `live_metrics` (the live run over the replay's dates) once it is `completed` or `cancelled`, else null;
  `events` = its last 20 `event_log` rows, newest first, masked.
- `POST /api/replays` (202): `create_replay` in the thread pool (it validates, writes the `queued` run and
  the `replay.start` audit row), then `services.replays.launch(run_id)` (`trader replay --run <id>`).
  `ReplayInvalid` -> 422 with `fields` (the location only, never the value: each message passes
  `strategies.safe_field_message` against the submitted value at that path), `ReplayBusy` -> 409, no
  launcher -> 503, a cancel that lands between the create and the launch -> 409 ("cancelled before it
  started", the run stays `cancelled`), any other spawn failure -> the run is `failed` ("could not start:
  <type>") and 500.
- `POST /api/replays/{id}/cancel`: `request_cancel` (audit `replay.cancel`); 409 when it is not queued or
  running.

`created_at` is `runs.started_at` (the row's creation, wall clock). Every route needs a session; both changes
need the CSRF header. Blocking database work runs in the thread pool (sync routes, or `anyio.to_thread`).

Registered under `/api` by `trader.api.routers.ROUTERS`.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from functools import partial
from typing import Annotated, Any

import anyio.to_thread
import structlog
from fastapi import APIRouter, Path, Query
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from trader.api import views
from trader.api.deps import (
    MAX_ID,
    ApiServices,
    CsrfUser,
    CurrentUser,
    Services,
    _settings,
    actor,
    live_run_id,
)
from trader.api.errors import ApiError
from trader.api.routers.strategies import safe_field_message
from trader.api.schemas import (
    Items,
    ReplayIn,
    ReplayOptionsOut,
    ReplayOut,
    ReplayProgressOut,
    ReplayStrategyOut,
    ReplaySummaryOut,
)
from trader.db import models as m
from trader.db.session import session_scope
from trader.logging_setup import redact_text
from trader.market.calendar import SessionCalendar
from trader.market.clock import et_date
from trader.replay.runner import create_replay, in_offline_window, reconcile_abandoned, request_cancel
from trader.replay.types import (
    ACTIVE_STATUSES,
    REPLAY_OVERRIDE_KEYS,
    ReplayBusy,
    ReplayInvalid,
    ReplayNotFound,
    ReplayProgress,
    ReplayRequest,
    ReplayRun,
    StrategyOverride,
    load_replay_run,
)
from trader.reports.metrics import compute_metrics

log = structlog.get_logger("api.replays")

router = APIRouter(tags=["replays"])

MAX_LIMIT = 200
EVENTS_SHOWN = 20
FINISHED: tuple[str, ...] = ("completed", "cancelled")  # the statuses that have metrics
LATEST_DELAY = timedelta(minutes=15)  # a session can be replayed from its close + 15 min
MINUTE = "1m"  # candle_archive interval code of 1-minute bars
R_PLACES = Decimal("0.0001")

ReplayId = Annotated[int, Path(ge=1, le=MAX_ID)]


# --- pure helpers -------------------------------------------------------------------------------------------


def latest_allowed(calendar: SessionCalendar, now: datetime) -> date:
    """The last session whose close + 15 minutes is at or before `now`."""
    today = et_date(now)
    if calendar.is_session(today) and now >= calendar.session_close(today) + LATEST_DELAY:
        return today
    return calendar.previous_session(today)


def offline_now(calendar: SessionCalendar, now: datetime) -> bool:
    """True when a replay started at `now` runs offline (09:15 to 16:30 ET on a session day), so it never
    competes with the live worker for Questrade's rate limit. The runner's `in_offline_window`, so the form
    and `create_replay` always agree (P5-T17)."""
    return in_offline_window(calendar, now)


def to_request(body: ReplayIn) -> ReplayRequest:
    return ReplayRequest(
        date_from=body.date_from,
        date_to=body.date_to,
        label=body.label,
        overrides=dict(body.overrides),
        strategies={
            key: StrategyOverride(enabled=s.enabled, params=dict(s.params) if s.params is not None else None)
            for key, s in body.strategies.items()
        },
        offline=body.offline,
    )


def _value_at(body: Mapping[str, Any], path: str) -> Any:
    """The submitted value at a runner error path (`strategies.orb_sip.params.exit_at`). A key may itself
    hold dots (`overrides.replay.catalyst_mode`), so the longest matching key wins. Where the path stops
    matching, the enclosing value is returned (so every value below it counts as submitted)."""
    node: Any = body
    parts = path.split(".")
    while parts and isinstance(node, Mapping):
        for n in range(len(parts), 0, -1):
            key = ".".join(parts[:n])
            if key in node:
                node, parts = node[key], parts[n:]
                break
        else:
            break
    return node


def invalid(exc: ReplayInvalid, body: ReplayIn | None = None) -> ApiError:
    """422 naming each field by its path (`overrides.risk_pct` -> `["body", "overrides", "risk_pct"]`).
    The runner's own messages never carry the input value, and pydantic's messages (settings overrides,
    strategy params) are passed through `safe_field_message` against the value submitted at that path."""
    submitted = body.model_dump(mode="json") if body is not None else {}
    fields = [
        {"loc": ["body", *path.split(".")], "msg": safe_field_message(msg, [_value_at(submitted, path)])}
        for path, msg in exc.errors
    ]
    return ApiError(422, "validation", "The replay request is invalid", fields)


def _r4(value: Decimal | None) -> Decimal | None:
    return value.quantize(R_PLACES, rounding=ROUND_HALF_UP) if value is not None else None


# --- reads --------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Totals:
    trades: int
    expectancy_r: Decimal | None
    total_pnl: Decimal | None


def _trade_totals(s: Session, run_ids: Iterable[int]) -> dict[int, _Totals]:
    ids = list(run_ids)
    if not ids:
        return {}
    t = m.Trade
    rows = s.execute(
        select(t.run_id, func.count(), func.avg(t.pnl_r), func.sum(t.pnl))
        .where(t.run_id.in_(ids))
        .group_by(t.run_id)
    ).tuples()
    return {run_id: _Totals(int(n), _r4(avg_r), total) for run_id, n, avg_r, total in rows}


def _progress(row_progress: Any) -> ReplayProgress:
    return ReplayProgress.from_json(row_progress if isinstance(row_progress, Mapping) else None)


def _summary(row: m.Run, totals: _Totals | None) -> ReplaySummaryOut | None:
    """A list row straight from the `runs` row (the settings snapshot is not parsed here), or None (logged)
    when its `params` can't be read, so one malformed row never fails the whole list."""
    try:
        return _summary_of(row, totals)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:  # pydantic's ValidationError included
        log.warning("api.replay_row_unreadable", run_id=row.id, error_type=type(exc).__name__)
        return None


def _summary_of(row: m.Run, totals: _Totals | None) -> ReplaySummaryOut:
    p: Mapping[str, Any] = row.params if isinstance(row.params, Mapping) else {}
    return ReplaySummaryOut(
        id=row.id,
        label=row.label,
        status=row.status,  # validated against ReplayStatus by the model
        date_from=date.fromisoformat(p["date_from"]),
        date_to=date.fromisoformat(p["date_to"]),
        created_at=row.started_at,
        finished_at=row.finished_at,
        data_mode=p["data_mode"],
        biased=bool(_progress(row.progress).biased_days),
        trades=totals.trades if totals else 0,
        expectancy_r=totals.expectancy_r if totals else None,
        total_pnl=totals.total_pnl if totals else None,
    )


def _is_busy(s: Session) -> bool:
    active = select(m.Run.id).where(m.Run.mode == "replay", m.Run.status.in_(ACTIVE_STATUSES)).limit(1)
    return s.execute(active).first() is not None


def _load(services: ApiServices, run_id: int) -> ReplayRun:
    try:
        return load_replay_run(services.core.factory, run_id)
    except ReplayNotFound:
        raise ApiError(404, "not_found", "Unknown replay") from None


def replay_out(services: ApiServices, run: ReplayRun) -> ReplayOut:
    """The full view of one replay (blocking: call it from the thread pool)."""
    factory = services.core.factory
    metrics = live_metrics = None
    if run.status in FINISHED:
        metrics = views.metrics_out(compute_metrics(factory, run.id))
        live = live_run_id(services)
        live_metrics = views.metrics_out(compute_metrics(factory, live, run.date_from, run.date_to))
    with factory() as s:
        rows = s.scalars(
            select(m.EventLog)
            .where(m.EventLog.run_id == run.id)
            .order_by(m.EventLog.id.desc())
            .limit(EVENTS_SHOWN)
        )
        events = [views.event_out(r) for r in rows]
    p = run.progress
    return ReplayOut(
        id=run.id,
        label=run.label,
        status=run.status,
        date_from=run.date_from,
        date_to=run.date_to,
        created_at=run.created_at,
        finished_at=run.finished_at,
        data_mode=run.data_mode,
        catalyst_mode=run.catalyst_mode,
        half_spread_bps=run.half_spread_bps,
        overrides=dict(run.overrides),
        strategies=[
            ReplayStrategyOut(
                key=ps.key,
                config_id=ps.config_id,
                revision=ps.revision,
                version=ps.version,
                scope=ps.scope,
                enabled=ps.enabled,
                params=dict(ps.params),
            )
            for ps in run.strategies
        ],
        progress=ReplayProgressOut(
            sessions_total=p.sessions_total,
            sessions_done=p.sessions_done,
            current_date=p.current_date,
            trades=p.trades,
            forced_closes=p.forced_closes,
            biased_days=list(p.biased_days),
            missing_opening_bars=p.missing_opening_bars,
            missing_minute_bars=p.missing_minute_bars,
            questrade_requests=p.questrade_requests,
        ),
        biased=bool(p.biased_days),
        cancel_requested=run.cancel_requested,
        error=redact_text(run.error) if run.error is not None else None,
        metrics=metrics,
        live_metrics=live_metrics,
        events=events,
    )


def _replay_out_by_id(services: ApiServices, run_id: int) -> ReplayOut:
    return replay_out(services, _load(services, run_id))


# --- routes -------------------------------------------------------------------------------------------------


@router.get("/replays/options")
def replay_options(_user: CurrentUser, services: Services) -> ReplayOptionsOut:
    core = services.core
    now = core.clock.now()
    settings = _settings(services)
    reconcile_abandoned(core.factory, core.clock)
    with core.factory() as s:
        first_bar = s.scalar(
            select(func.min(m.CandleArchive.start_ts)).where(m.CandleArchive.interval == MINUTE)
        )
        first_snapshot = s.scalar(select(func.min(m.UniverseSnapshot.session_date)))
        busy = _is_busy(s)
    return ReplayOptionsOut(
        override_keys=sorted(REPLAY_OVERRIDE_KEYS),
        max_sessions=settings.replay_max_sessions,
        latest_allowed=latest_allowed(core.calendar, now),
        questrade_from=et_date(now) - timedelta(days=settings.replay_questrade_window_days),
        archive_from=et_date(first_bar) if first_bar is not None else None,
        snapshots_from=first_snapshot,
        busy=busy,
        offline_now=offline_now(core.calendar, now),
    )


@router.get("/replays")
def list_replays(
    _user: CurrentUser, services: Services, limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 50
) -> Items[ReplaySummaryOut]:
    core = services.core
    reconcile_abandoned(core.factory, core.clock)
    with core.factory() as s:
        rows = list(
            s.scalars(
                select(m.Run)
                .where(m.Run.mode == "replay")
                .order_by(m.Run.started_at.desc(), m.Run.id.desc())
                .limit(limit)
            )
        )
        totals = _trade_totals(s, (r.id for r in rows))
    summaries = (_summary(r, totals.get(r.id)) for r in rows)
    return Items[ReplaySummaryOut](items=[x for x in summaries if x is not None])


@router.get("/replays/{replay_id}")
def get_replay(replay_id: ReplayId, _user: CurrentUser, services: Services) -> ReplayOut:
    return _replay_out_by_id(services, replay_id)


def _mark_not_started(services: ApiServices, run_id: int, error_type: str) -> None:
    core = services.core
    now = core.clock.now()
    with session_scope(core.factory) as s:
        s.execute(
            update(m.Run)
            .where(m.Run.id == run_id, m.Run.mode == "replay", m.Run.status == "queued")
            .values(status="failed", error=f"could not start: {error_type}", finished_at=now, updated_at=now)
        )


def _cancelled_before_launch(services: ApiServices, run_id: int) -> bool:
    """True when the run was cancelled between `create_replay` and the launch (the launcher then refuses a
    run that is no longer `queued`). Never raises: an unreadable row counts as not cancelled."""
    try:
        with services.core.factory() as s:
            status = s.scalar(select(m.Run.status).where(m.Run.id == run_id, m.Run.mode == "replay"))
    except Exception as exc:
        log.error("api.replay_status_unreadable", run_id=run_id, error_type=type(exc).__name__)
        return False
    return status == "cancelled"


@router.post("/replays", status_code=202)
async def start_replay(body: ReplayIn, user: CsrfUser, services: Services) -> ReplayOut:
    launcher = services.replays
    if launcher is None:
        raise ApiError(503, "unavailable", "Replays are not available")
    core = services.core
    create = partial(
        create_replay,
        core.factory,
        core.clock,
        core.calendar,
        core.settings,
        services.registry,
        to_request(body),
        actor(user),
        app_version=core.env.app_version,
    )
    try:
        run_id = await anyio.to_thread.run_sync(create)
    except ReplayInvalid as exc:
        raise invalid(exc, body) from None
    except ReplayBusy:
        raise ApiError(409, "conflict", "A replay is already running.") from None
    try:
        await launcher.launch(run_id)
    except Exception as exc:
        error_type = type(exc).__name__
        if await anyio.to_thread.run_sync(_cancelled_before_launch, services, run_id):
            log.info("api.replay_cancelled_before_launch", run_id=run_id)
            raise ApiError(409, "conflict", "The replay was cancelled before it started.") from None
        log.error("api.replay_launch_failed", run_id=run_id, error_type=error_type)
        try:
            await anyio.to_thread.run_sync(_mark_not_started, services, run_id, error_type)
        except Exception as mark_exc:
            log.error("api.replay_mark_failed", run_id=run_id, error_type=type(mark_exc).__name__)
        raise ApiError(500, "internal", "The replay could not be started") from None
    log.info("api.replay_started", run_id=run_id)
    return await anyio.to_thread.run_sync(_replay_out_by_id, services, run_id)


@router.post("/replays/{replay_id}/cancel")
def cancel_replay(replay_id: ReplayId, user: CsrfUser, services: Services) -> ReplayOut:
    core = services.core
    _load(services, replay_id)  # 404 for an unknown id or a live run
    if not request_cancel(core.factory, core.clock, replay_id, actor(user)):
        raise ApiError(409, "conflict", "The replay is not running.")
    return _replay_out_by_id(services, replay_id)
