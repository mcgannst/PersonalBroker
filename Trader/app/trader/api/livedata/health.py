"""The Control page's health and soak parts (live dashboard plan S10, design §4.5–4.6). Questrade numbers come
from the worker's heartbeat detail only (never a Questrade call).

- `questrade_stats`, `opening_bars` and `marks_health` parse the heartbeat's `questrade`, `candle_batches` and
  `marks` keys (S10). Anything unknown or malformed (a string for a count, NaN, a list for a mapping, a
  timestamp without a zone) is ignored and gives None, never an error: the heartbeat comes from another
  process and another version of the code.
- `health_panel`: the heartbeat, the token chain, a database check, Telegram, the parsed worker numbers, the
  undelivered notifications and the tzdata release, as the System page had them.
- `soak_summary`: `trader.jobs.soak.load_report` built exactly as `trader soak-report` builds it (read-only:
  `soak.readonly_plan`, never `runtime.plan_builder`, so no run is created, no strategy default ensured and no
  event written; a `NullNotifier`, never sent), summarised and cached in-process for `SOAK_CACHE_SECONDS` per
  database and ET date.
"""

import math
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from typing import Any

import structlog
from sqlalchemy.orm import Session, sessionmaker

from trader.api import views
from trader.api.deps import ApiServices, _settings
from trader.api.livedata.types import HEARTBEAT_BADGE_SECONDS, SOAK_CACHE_SECONDS
from trader.api.routers.meta import db_check, tz_iana_version
from trader.api.routers.system import _rate_limit, _undelivered
from trader.api.schemas import (
    HealthPanelOut,
    MarksHealthOut,
    OpeningBarsOut,
    QuestradeCountsOut,
    QuestradeStatsOut,
    SoakSummaryOut,
    SoakTodayOut,
)
from trader.jobs import soak
from trader.market.calendar import SessionCalendar
from trader.market.clock import ET, et_date
from trader.notify.notifier import NullNotifier
from trader.settings_store import RuntimeSettings

log = structlog.get_logger("api.livedata.health")

OPENING_FETCH_FROM = time(9, 35)  # the 9:35 opening-bar batch starts in [09:35, 09:40) ET
OPENING_FETCH_UNTIL = time(9, 40)
COUNT_KEYS = ("requests", "http_429", "pause_s", "http_5xx", "transport_errors")


# --- parsing helpers (never raise) --------------------------------------------------------------------------


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _ts(value: Any) -> datetime | None:
    """An ISO timestamp with a zone, as UTC; None for anything else."""
    if not isinstance(value, str):
        return None
    try:
        at = datetime.fromisoformat(value)
    except ValueError:
        return None
    if at.tzinfo is None or at.utcoffset() is None:
        return None
    return at.astimezone(UTC)


def _date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _mapping(detail: Mapping[str, Any] | None, key: str) -> Mapping[str, Any] | None:
    if not isinstance(detail, Mapping):
        return None
    value = detail.get(key)
    return value if isinstance(value, Mapping) else None


def _counts(value: Any) -> QuestradeCountsOut | None:
    if not isinstance(value, Mapping):
        return None
    ints = {k: _int(value.get(k)) for k in ("requests", "http_429", "http_5xx", "transport_errors")}
    pause = _float(value.get("pause_s"))
    if pause is None or any(v is None for v in ints.values()):
        return None
    return QuestradeCountsOut(
        requests=ints["requests"] or 0,
        http_429=ints["http_429"] or 0,
        pause_s=pause,
        http_5xx=ints["http_5xx"] or 0,
        transport_errors=ints["transport_errors"] or 0,
    )


# --- the heartbeat's keys -----------------------------------------------------------------------------------


def questrade_stats(detail: Mapping[str, Any] | None, now: datetime) -> QuestradeStatsOut | None:
    """Today's Questrade request counts (S10 `questrade`) plus the existing `rate_limit` numbers; None when
    absent, malformed, or not of today's ET date (a worker that has not made a call yet today)."""
    q = _mapping(detail, "questrade")
    if q is None:
        return None
    day, since = _date(q.get("day")), _ts(q.get("since"))
    market, account = _counts(q.get("market")), _counts(q.get("account"))
    if day is None or since is None or market is None or account is None or day != et_date(now):
        return None
    return QuestradeStatsOut(
        day=day,
        since=since,
        market=market,
        account=account,
        rate_limit=_rate_limit(dict(detail)) if detail is not None else None,
    )


def _batch(entry: Any) -> OpeningBarsOut | None:
    if not isinstance(entry, Mapping):
        return None
    started = _ts(entry.get("started_at"))
    ints = {k: _int(entry.get(k)) for k in ("symbols", "completed", "errors", "outstanding", "http_429")}
    elapsed, pause = _float(entry.get("elapsed_s")), _float(entry.get("pause_s"))
    raw_deadline, raised = entry.get("deadline_s"), entry.get("raised")
    deadline = _float(raw_deadline)
    if started is None or elapsed is None or pause is None or any(v is None for v in ints.values()):
        return None
    if (raw_deadline is not None and deadline is None) or (
        raised is not None and not isinstance(raised, str)
    ):
        return None
    errors, outstanding = ints["errors"] or 0, ints["outstanding"] or 0
    return OpeningBarsOut(
        session_date=et_date(started),
        started_at=started,
        symbols=ints["symbols"] or 0,
        completed=ints["completed"] or 0,
        errors=errors,
        outstanding=outstanding,
        elapsed_s=elapsed,
        deadline_s=deadline,
        http_429=ints["http_429"] or 0,
        pause_s=pause,
        complete=outstanding == 0 and errors == 0 and raised is None,
        raised=raised,
    )


def opening_bars(
    detail: Mapping[str, Any] | None, calendar: SessionCalendar, now: datetime
) -> OpeningBarsOut | None:
    """The first `candle_batches` entry of today that started in [09:35, 09:40) ET, or None."""
    if not isinstance(detail, Mapping):
        return None
    entries = detail.get("candle_batches")
    if not isinstance(entries, list):
        return None
    today = et_date(now)
    try:
        if not calendar.is_session(today):
            return None
    except ValueError:  # outside the calendar's range
        return None
    found: list[OpeningBarsOut] = []
    for entry in entries:
        out = _batch(entry)
        if out is None or out.session_date != today:
            continue
        if OPENING_FETCH_FROM <= out.started_at.astimezone(ET).time() < OPENING_FETCH_UNTIL:
            found.append(out)
    return min(found, key=lambda b: b.started_at) if found else None


def marks_health(detail: Mapping[str, Any] | None) -> MarksHealthOut | None:
    """The mark publisher's health (S10 `marks`), or None when absent or malformed."""
    marks = _mapping(detail, "marks")
    if marks is None:
        return None
    raw_written, symbols, failing = marks.get("written_at"), _int(marks.get("symbols")), marks.get("failing")
    written = _ts(raw_written)
    if (raw_written is not None and written is None) or symbols is None or not isinstance(failing, bool):
        return None
    return MarksHealthOut(written_at=written, symbols=symbols, failing=failing)


# --- the panel ----------------------------------------------------------------------------------------------


def health_panel(services: ApiServices, now: datetime) -> HealthPanelOut:
    core = services.core
    worker = views.worker_out(core.factory, now, _settings(services).worker_heartbeat_stale_seconds)
    detail = worker.detail
    try:
        latency: int | None = db_check(core.factory)
        db_ok = True
    except Exception as exc:
        log.warning("api.control_db_check_failed", error_type=type(exc).__name__)
        latency, db_ok = None, False
    with core.factory() as s:
        undelivered = _undelivered(s)
    return HealthPanelOut(
        worker=worker,
        worker_stale=worker.age_seconds is None or worker.age_seconds > HEARTBEAT_BADGE_SECONDS,
        token=views.token_out(services.credentials.health, now),
        db_ok=db_ok,
        db_latency_ms=latency,
        telegram_configured=services.telegram_configured,
        questrade=questrade_stats(detail, now),
        opening_bars=opening_bars(detail, core.calendar, now),
        marks=marks_health(detail),
        notifications_failed=undelivered,
        tz_iana_version=tz_iana_version(core.env.tz_display),
    )


# --- the soak summary ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Cached:
    at: datetime
    summary: SoakSummaryOut


_soak_lock = threading.Lock()
_soak_cache: dict[tuple[int, date], _Cached] = {}


def clear_soak_cache() -> None:
    """Forget every cached soak summary (tests)."""
    with _soak_lock:
        _soak_cache.clear()


def _soak_deps(services: ApiServices) -> soak.SoakDeps:
    """The deps `trader soak-report` builds (cli._soak_deps), read-only, with a null notifier."""
    from trader.runtime import build_renderer  # the composition root; imported late, never at load

    core = services.core

    def load() -> RuntimeSettings:
        return _settings(services)

    return soak.SoakDeps(
        factory=core.factory,
        clock=core.clock,
        calendar=core.calendar,
        settings=load,
        plan=soak.readonly_plan(core.factory, core.clock, core.calendar, load),
        orb_enabled=soak.orb_enabled_reader(core.factory, core.clock),
        notifier=NullNotifier(),
        render=build_renderer(core),
        env=core.env.app_env,
    )


def _summary(report: soak.SoakReport, now: datetime) -> SoakSummaryOut:
    day_one: date | None = None
    if report.consecutive_clean > 0 and report.last_final is not None:
        final = [d for d in report.days if d.session_date <= report.last_final]
        run = final[-report.consecutive_clean :]
        day_one = run[0].session_date if run else None
    today = next((d for d in report.days if d.session_date == et_date(now)), None)
    return SoakSummaryOut(
        target=report.target,
        consecutive_clean=report.consecutive_clean,
        total_clean=report.total_clean,
        day_one=day_one,
        earliest_finish=report.earliest_finish,
        last_final=report.last_final,
        today=(
            SoakTodayOut(
                session_date=today.session_date,
                verdict=today.verdict,
                failed=list(today.failed),
                provisional=today.provisional,
            )
            if today is not None
            else None
        ),
        generated_at=report.generated_at,
    )


def _cache_key(factory: sessionmaker[Session], now: datetime) -> tuple[int, date]:
    return id(factory), et_date(now)


def soak_summary(services: ApiServices, now: datetime) -> SoakSummaryOut:
    """The soak report's headline (the defaults: 20 sessions, target 10), cached for SOAK_CACHE_SECONDS."""
    key = _cache_key(services.core.factory, now)
    with _soak_lock:
        hit = _soak_cache.get(key)
        if hit is not None and 0 <= (now - hit.at).total_seconds() < SOAK_CACHE_SECONDS:
            return hit.summary
        summary = _summary(soak.load_report(_soak_deps(services)), now)
        _soak_cache.clear()  # one entry: today's (older days are never read again)
        _soak_cache[key] = _Cached(now, summary)
        return summary
