"""Kill switches (SPEC §6.3, BR-41): checked before every entry proposal and after every fill.

daily_loss_pct blocks entries until the next session (it resets by itself); max_drawdown_pct and expectancy
need a manual reset with a reason; manual_pause is set and lifted by /pause and /resume. None of them ever
blocks an exit, a stop or a cancel (the risk manager and the proposal service's entry guard only consult
them for entries).

A manual reset holds (orchestrator ruling, fix round 1):
- after a max_drawdown_pct reset the drawdown is measured from the equity at the reset (or any new high
  since), not from the old all-time peak, so it re-trips only on a further max_drawdown_pct fall;
- after an expectancy reset the switch re-arms only once `killswitch_expectancy_min_trades` more trades
  with an R multiple have closed, and the expectancy is then taken over the trades since the reset.
Both are derived from the reset row's `reset_at` and the reset event's data (`equity_at_reset`), so no
schema change is needed.

evaluate, pause, resume and reset serialise per run on a transaction-level advisory lock and re-read the
active switches inside that transaction, so two racing evaluations write one trip row and one alert.
"""

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker

from trader.broker.types import AccountState
from trader.db import models as m
from trader.db.session import session_scope
from trader.events import log_event
from trader.market.clock import Clock, et_date
from trader.settings_store import RuntimeSettings

SWITCHES = ("daily_loss_pct", "max_drawdown_pct", "expectancy", "manual_pause")
Q4 = Decimal("0.0001")
Q6 = Decimal("0.000001")
SOURCE = "killswitch"
LOCK_NAMESPACE = "kill_switches"


def _trip_message(switch: str) -> str:
    return f"kill switch {switch} tripped: entries blocked"


def _reset_message(switch: str) -> str:
    return f"kill switch {switch} reset"


@dataclass(frozen=True, slots=True)
class KillSwitchInputs:
    start_equity: Decimal
    equity: Decimal
    peak_equity: Decimal
    closed_trades: int
    expectancy_r: Decimal | None

    @property
    def daily_pnl_pct(self) -> Decimal:
        if self.start_equity <= 0:
            return Decimal(0)
        return ((self.equity - self.start_equity) / self.start_equity).quantize(Q6, ROUND_HALF_UP)

    @property
    def drawdown_pct(self) -> Decimal:
        if self.peak_equity <= 0:
            return Decimal(0)
        return ((self.peak_equity - self.equity) / self.peak_equity).quantize(Q6, ROUND_HALF_UP)


@dataclass(frozen=True, slots=True)
class ActiveSwitch:
    switch: str
    event_id: int
    tripped_at: datetime
    value: Decimal | None


@dataclass(frozen=True, slots=True)
class _DrawdownReset:
    reset_at: datetime
    baseline: Decimal | None  # max(equity at the reset, highest snapshot equity since); None if unknown


class KillSwitches:
    def __init__(self, factory: sessionmaker[Session], clock: Clock) -> None:
        self._factory = factory
        self._clock = clock

    # --- helpers -------------------------------------------------------------------------------------------
    @staticmethod
    def _lock(s: Session, run_id: int) -> None:
        """Serialise every kill-switch write for one run until this transaction ends."""
        s.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"{LOCK_NAMESPACE}:{run_id}"})

    @staticmethod
    def _open_rows(s: Session, run_id: int, switch: str | None = None) -> list[m.KillSwitchEvent]:
        q = select(m.KillSwitchEvent).where(
            m.KillSwitchEvent.run_id == run_id, m.KillSwitchEvent.reset_at.is_(None)
        )
        if switch is not None:
            q = q.where(m.KillSwitchEvent.switch == switch)
        return list(s.execute(q.order_by(m.KillSwitchEvent.id).with_for_update()).scalars())

    @staticmethod
    def _active(s: Session, run_id: int, session_date: date) -> list[ActiveSwitch]:
        rows = s.execute(
            select(m.KillSwitchEvent)
            .where(m.KillSwitchEvent.run_id == run_id, m.KillSwitchEvent.reset_at.is_(None))
            .order_by(m.KillSwitchEvent.id)
        ).scalars()
        return [
            ActiveSwitch(r.switch, r.id, r.tripped_at, r.value)
            for r in rows
            if r.switch != "daily_loss_pct" or r.session_date == session_date
        ]

    @staticmethod
    def _last_reset_at(s: Session, run_id: int, switch: str) -> datetime | None:
        return s.execute(
            select(func.max(m.KillSwitchEvent.reset_at)).where(
                m.KillSwitchEvent.run_id == run_id,
                m.KillSwitchEvent.switch == switch,
                m.KillSwitchEvent.reset_at.is_not(None),
            )
        ).scalar_one()

    @staticmethod
    def _last_event_data(s: Session, run_id: int, message: str) -> tuple[datetime, dict[str, Any]] | None:
        row = s.execute(
            select(m.EventLog.ts, m.EventLog.data)
            .where(m.EventLog.run_id == run_id, m.EventLog.source == SOURCE, m.EventLog.message == message)
            .order_by(m.EventLog.id.desc())
            .limit(1)
        ).one_or_none()
        if row is None:
            return None
        return row[0], dict(row[1] or {})

    def _drawdown_reset(self, s: Session, run_id: int) -> _DrawdownReset | None:
        reset_at = self._last_reset_at(s, run_id, "max_drawdown_pct")
        if reset_at is None:
            return None
        known: list[Decimal] = []
        ev = self._last_event_data(s, run_id, _reset_message("max_drawdown_pct"))
        if ev is not None and ev[1].get("equity_at_reset") is not None:
            known.append(Decimal(str(ev[1]["equity_at_reset"])))
        high = s.execute(
            select(func.max(m.EquitySnapshot.equity)).where(
                m.EquitySnapshot.run_id == run_id, m.EquitySnapshot.ts > reset_at
            )
        ).scalar_one()
        if high is not None:
            known.append(high)
        return _DrawdownReset(reset_at, max(known) if known else None)

    @staticmethod
    def _trades_since(s: Session, run_id: int, since: datetime | None) -> tuple[int, Decimal | None]:
        """Closed trades with an R multiple (count(pnl_r)) and their average, after `since` if given."""
        q = select(func.count(m.Trade.pnl_r), func.avg(m.Trade.pnl_r)).where(m.Trade.run_id == run_id)
        if since is not None:
            q = q.where(m.Trade.closed_at > since)
        count, avg_r = s.execute(q).one()
        return int(count), (Decimal(avg_r).quantize(Q4, ROUND_HALF_UP) if avg_r is not None else None)

    def _last_known_equity(self, s: Session, run_id: int, switch: str) -> Decimal | None:
        """The most recent equity on record: the latest snapshot or the equity at the latest trip."""
        candidates: list[tuple[datetime, Decimal]] = []
        snap = s.execute(
            select(m.EquitySnapshot.ts, m.EquitySnapshot.equity)
            .where(m.EquitySnapshot.run_id == run_id)
            .order_by(m.EquitySnapshot.ts.desc())
            .limit(1)
        ).one_or_none()
        if snap is not None:
            candidates.append((snap[0], snap[1]))
        trip = self._last_event_data(s, run_id, _trip_message(switch))
        if trip is not None and trip[1].get("equity") is not None:
            candidates.append((trip[0], Decimal(str(trip[1]["equity"]))))
        return max(candidates, key=lambda c: c[0])[1] if candidates else None

    # --- queries -------------------------------------------------------------------------------------------
    def active(self, run_id: int, session_date: date) -> list[ActiveSwitch]:
        with self._factory() as s:
            return self._active(s, run_id, session_date)

    def blocking(self, run_id: int, session_date: date) -> str | None:
        active = self.active(run_id, session_date)
        return active[0].switch if active else None

    def entry_guard(self) -> Callable[[int], str | None]:
        """For ProposalService(entry_blocked=...): the reason entries are blocked right now, or None."""

        def guard(run_id: int) -> str | None:
            switch = self.blocking(run_id, et_date(self._clock.now()))
            return f"kill switch {switch} is tripped" if switch is not None else None

        return guard

    def inputs(
        self, run_id: int, session_date: date, account: AccountState, session_open: datetime
    ) -> KillSwitchInputs:
        with self._factory() as s:
            start = s.execute(
                select(m.EquitySnapshot.equity)
                .where(m.EquitySnapshot.run_id == run_id, m.EquitySnapshot.ts < session_open)
                .order_by(m.EquitySnapshot.ts.desc())
                .limit(1)
            ).scalar_one_or_none()
            starting_cash = s.execute(
                select(m.SimAccount.starting_cash).where(m.SimAccount.run_id == run_id)
            ).scalar_one_or_none()
            stored_peak = s.execute(
                select(func.max(m.EquitySnapshot.peak_equity)).where(m.EquitySnapshot.run_id == run_id)
            ).scalar_one()
            dd_reset = self._drawdown_reset(s, run_id)
            count, avg_r = self._trades_since(s, run_id, self._last_reset_at(s, run_id, "expectancy"))
        start_equity = (
            start if start is not None else (starting_cash if starting_cash is not None else account.equity)
        )
        if dd_reset is None:
            peak = max(v for v in (stored_peak, starting_cash, account.equity) if v is not None)
        else:  # measured from the equity at the reset (or a new high since), not the old peak
            peak = max(dd_reset.baseline or account.equity, account.equity)
        return KillSwitchInputs(
            start_equity=start_equity,
            equity=account.equity,
            peak_equity=peak,
            closed_trades=count,
            expectancy_r=avg_r,
        )

    # --- writes ---------------------------------------------------------------------------------------------
    def evaluate(
        self, run_id: int, session_date: date, inputs: KillSwitchInputs, settings: RuntimeSettings
    ) -> list[str]:
        with session_scope(self._factory) as s:
            self._lock(s, run_id)
            active = {a.switch for a in self._active(s, run_id, session_date)}  # re-read under the lock
            crossed: list[tuple[str, Decimal, Decimal]] = []
            loss = -inputs.daily_pnl_pct
            if "daily_loss_pct" not in active and loss >= settings.killswitch_daily_loss_pct:
                crossed.append(("daily_loss_pct", loss, settings.killswitch_daily_loss_pct))
            if "max_drawdown_pct" not in active:
                drawdown = inputs.drawdown_pct
                dd_reset = self._drawdown_reset(s, run_id)
                if dd_reset is not None:
                    base = max(dd_reset.baseline or inputs.equity, inputs.equity)
                    drawdown = replace(inputs, peak_equity=min(inputs.peak_equity, base)).drawdown_pct
                if drawdown >= settings.killswitch_max_drawdown_pct:
                    crossed.append(("max_drawdown_pct", drawdown, settings.killswitch_max_drawdown_pct))
            if (
                "expectancy" not in active
                and inputs.closed_trades >= settings.killswitch_expectancy_min_trades
                and inputs.expectancy_r is not None
                and inputs.expectancy_r <= settings.killswitch_expectancy_threshold_r
            ):
                reset_at = self._last_reset_at(s, run_id, "expectancy")
                rearmed = (
                    reset_at is None
                    or self._trades_since(s, run_id, reset_at)[0] >= settings.killswitch_expectancy_min_trades
                )
                if rearmed:
                    crossed.append(
                        ("expectancy", inputs.expectancy_r, settings.killswitch_expectancy_threshold_r)
                    )
            if not crossed:
                return []
            now = self._clock.now()
            for switch, value, threshold in crossed:
                s.add(
                    m.KillSwitchEvent(
                        run_id=run_id,
                        switch=switch,
                        session_date=session_date,
                        tripped_at=now,
                        value=value,
                        threshold=threshold,
                    )
                )
                data: dict[str, Any] = {
                    "switch": switch,
                    "value": str(value),
                    "threshold": str(threshold),
                    "equity": str(inputs.equity),
                    "closed_trades": inputs.closed_trades,
                }
                log_event(s, self._clock, "error", SOURCE, _trip_message(switch), data, run_id)
            return [c[0] for c in crossed]

    def pause(self, run_id: int, session_date: date, actor: str) -> bool:
        with session_scope(self._factory) as s:
            self._lock(s, run_id)
            if self._open_rows(s, run_id, "manual_pause"):
                return False
            now = self._clock.now()
            s.add(
                m.KillSwitchEvent(
                    run_id=run_id, switch="manual_pause", session_date=session_date, tripped_at=now
                )
            )
            s.add(
                m.AuditLog(
                    ts=now, actor=actor, action="killswitch.pause", before=None, after={"run_id": run_id}
                )
            )
            log_event(
                s, self._clock, "warning", SOURCE, "manual pause: entries blocked", {"actor": actor}, run_id
            )
            return True

    def resume(self, run_id: int, actor: str) -> bool:
        with session_scope(self._factory) as s:
            self._lock(s, run_id)
            rows = self._open_rows(s, run_id, "manual_pause")
            if not rows:
                return False
            now = self._clock.now()
            for r in rows:
                r.reset_at, r.reset_reason, r.reset_by = now, "resume", actor
            s.add(
                m.AuditLog(
                    ts=now, actor=actor, action="killswitch.resume", before={"run_id": run_id}, after=None
                )
            )
            log_event(s, self._clock, "info", SOURCE, "manual pause lifted", {"actor": actor}, run_id)
            return True

    def reset(
        self, run_id: int, switch: str, reason: str, actor: str, *, equity: Decimal | None = None
    ) -> None:
        """Re-enable an automatic switch. `equity` is the account equity now (the new drawdown baseline);
        when the caller doesn't know it, the most recent equity on record is used."""
        if switch == "manual_pause":
            raise ValueError("a manual pause is lifted with resume, not reset")
        if switch not in SWITCHES:
            raise ValueError(f"unknown switch {switch!r}")
        if not reason.strip():
            raise ValueError("a kill-switch reset needs a reason")
        with session_scope(self._factory) as s:
            self._lock(s, run_id)
            rows = self._open_rows(s, run_id, switch)
            if not rows:
                raise ValueError(f"kill switch {switch} is not tripped")
            now = self._clock.now()
            at_reset = equity if equity is not None else self._last_known_equity(s, run_id, switch)
            before = [{"id": r.id, "tripped_at": r.tripped_at.isoformat()} for r in rows]
            for r in rows:
                r.reset_at, r.reset_reason, r.reset_by = now, reason.strip(), actor
            s.add(
                m.AuditLog(
                    ts=now,
                    actor=actor,
                    action=f"killswitch.reset:{switch}",
                    before={"trips": before},
                    after={"reason": reason.strip()},
                )
            )
            log_event(
                s,
                self._clock,
                "warning",
                SOURCE,
                _reset_message(switch),
                {
                    "switch": switch,
                    "reason": reason.strip(),
                    "equity_at_reset": str(at_reset) if at_reset is not None else None,
                },
                run_id,
            )
