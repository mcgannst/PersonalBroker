"""Small response builders shared by the dashboard (T5), decisions (T6) and system (T9) routes, so the pages
can't disagree with each other or with Telegram (they reuse `trader.notify.views`).

Read-only; every text that came from a log or an error is passed through `redact_text`.
"""

from collections.abc import Callable, Mapping
from datetime import date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from trader.adapters.questrade.auth import TokenHealth
from trader.adapters.telegram.commands import _reset_hint
from trader.api.schemas import EventOut, HistogramBinOut, KillSwitchOut, MetricsOut, TokenOut, WorkerOut
from trader.db import models as m
from trader.engine.killswitch import SWITCHES, KillSwitches
from trader.logging_setup import REDACTED, is_secret_key, redact_text
from trader.notify.views import STOPPED_PHASES, WORKER_PROCESS, token_state
from trader.reports.metrics import Metrics

KILLSWITCH_LABELS: Mapping[str, str] = {
    "daily_loss_pct": "Daily loss",
    "max_drawdown_pct": "Max drawdown",
    "expectancy": "Expectancy",
    "manual_pause": "Manual pause",
}
WEB_RESET_SWITCHES = frozenset({"max_drawdown_pct", "expectancy"})  # cleared only by a reset with a reason
MANUAL_PAUSE_CLEARS = "lift it with Resume (web) or /resume"


def token_out(token_health: Callable[[], TokenHealth], now: datetime) -> TokenOut:
    """The token chain's state (the `/status` rules of `notify.views.token_state`). Reads no token value; a
    failing health check is shown as not OK, never raised."""
    seen: list[TokenHealth] = []

    def once() -> TokenHealth:
        health = token_health()
        seen.append(health)
        return health

    ok, age, error = token_state(once, now)
    health = seen[0] if seen else None
    return TokenOut(
        ok=ok,
        seeded=health.seeded if health is not None else False,
        age_hours=age,
        expires_at=health.expires_at if health is not None else None,
        last_refresh_at=health.last_refresh_at if health is not None else None,
        error=redact_text(error) if error is not None else None,
    )


def worker_out(factory: sessionmaker[Session], now: datetime, stale_seconds: int) -> WorkerOut:
    """The worker's heartbeat. OK when the row exists, the worker is not going away (`stopping`/`stopped`)
    and the last beat is at most `stale_seconds` old."""
    with factory() as s:
        row = s.execute(
            select(m.WorkerHeartbeat).where(m.WorkerHeartbeat.process == WORKER_PROCESS)
        ).scalar_one_or_none()
        if row is None:
            return WorkerOut(ok=False)
        age = (now - row.beat_at).total_seconds()
        return WorkerOut(
            ok=row.phase not in STOPPED_PHASES and age <= stale_seconds,
            phase=row.phase,
            beat_at=row.beat_at,
            age_seconds=age,
            session_date=row.session_date,
            pid=row.pid,
            host=row.host,
            detail=row.detail if isinstance(row.detail, dict) else None,
        )


def _redacted(value: Any) -> Any:
    """A copy with every string masked by pattern and every secret-named key's value replaced."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        return {k: REDACTED if is_secret_key(k) and v is not None else _redacted(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_redacted(v) for v in value]
    return value


def event_out(row: m.EventLog) -> EventOut:
    """One `event_log` row, its message and data masked (a bot token in a URL, a password=...)."""
    data: dict[str, Any] | None
    if row.data is None:
        data = None
    elif isinstance(row.data, Mapping):
        data = _redacted(row.data)
    else:
        data = {"value": _redacted(row.data)}
    return EventOut(
        id=row.id,
        ts=row.ts,
        level=row.level,
        source=row.source,
        message=redact_text(row.message),
        data=data,
    )


def killswitch_states(
    killswitches: KillSwitches, factory: sessionmaker[Session], run_id: int, session_date: date
) -> list[KillSwitchOut]:
    """All four switches in `SWITCHES` order for the run and session: tripped or not (`KillSwitches.active`:
    `daily_loss_pct` counts for its own session only), with the open event's value and threshold, whether
    only a web reset clears it, and how it clears (the Telegram wording)."""
    active = {a.switch: a for a in killswitches.active(run_id, session_date)}
    events: dict[int, m.KillSwitchEvent] = {}
    if active:
        with factory() as s:
            ids = [a.event_id for a in active.values()]
            events = {
                e.id: e
                for e in s.execute(select(m.KillSwitchEvent).where(m.KillSwitchEvent.id.in_(ids))).scalars()
            }
    out: list[KillSwitchOut] = []
    for switch in SWITCHES:
        automatic = switch != "manual_pause"
        a = active.get(switch)
        ev = events.get(a.event_id) if a is not None else None
        if a is None:
            clears = ""
        elif automatic:
            clears = _reset_hint(switch)
        else:
            clears = MANUAL_PAUSE_CLEARS
        out.append(
            KillSwitchOut(
                switch=switch,
                label=KILLSWITCH_LABELS.get(switch, switch),
                tripped=a is not None,
                tripped_at=a.tripped_at if a is not None else None,
                value=ev.value if ev is not None else None,
                threshold=ev.threshold if ev is not None else None,
                automatic=automatic,
                needs_web_reset=a is not None and switch in WEB_RESET_SWITCHES,
                clears=clears,
            )
        )
    return out


def metrics_out(mt: Metrics) -> MetricsOut:
    """The `/api/metrics` response (and a replay's comparison) from `trader.reports.metrics`, field by field.
    The histogram's open ends stay the `-Infinity` / `Infinity` sentinels of the P4-T7 wire format."""
    return MetricsOut(
        run_id=mt.run_id,
        from_date=mt.date_from,
        to_date=mt.date_to,
        trades=mt.trades,
        wins=mt.wins,
        win_rate=mt.win_rate,
        avg_win_r=mt.avg_win_r,
        avg_loss_r=mt.avg_loss_r,
        expectancy_r=mt.expectancy_r,
        profit_factor=mt.profit_factor,
        avg_slippage=mt.avg_slippage,
        max_drawdown_pct=mt.max_drawdown_pct,
        adherence_pct=mt.adherence_pct,
        total_pnl=mt.total_pnl,
        r_histogram=[HistogramBinOut(lo=b.lo, hi=b.hi, count=b.count) for b in mt.r_histogram],
        losses=mt.losses,
        total_fees=mt.total_fees,
        avg_slippage_per_share=mt.avg_slippage_per_share,
        trades_without_r=mt.trades_without_r,
    )
