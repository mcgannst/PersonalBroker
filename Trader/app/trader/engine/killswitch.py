"""Kill switches (SPEC §6.3, BR-41): checked before every entry proposal and after every fill.

daily_loss_pct blocks entries until the next session (it resets by itself); max_drawdown_pct and expectancy
need a manual reset with a reason; manual_pause is set and lifted by /pause and /resume. None of them ever
blocks an exit, a stop or a cancel (the risk manager only consults them for entries).
"""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from trader.broker.types import AccountState
from trader.db import models as m
from trader.db.session import session_scope
from trader.events import log_event
from trader.market.clock import Clock
from trader.settings_store import RuntimeSettings

SWITCHES = ("daily_loss_pct", "max_drawdown_pct", "expectancy", "manual_pause")
Q4 = Decimal("0.0001")
Q6 = Decimal("0.000001")
SOURCE = "killswitch"


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


class KillSwitches:
    def __init__(self, factory: sessionmaker[Session], clock: Clock) -> None:
        self._factory = factory
        self._clock = clock

    @staticmethod
    def _open_rows(s: Session, run_id: int, switch: str | None = None) -> list[m.KillSwitchEvent]:
        q = select(m.KillSwitchEvent).where(
            m.KillSwitchEvent.run_id == run_id, m.KillSwitchEvent.reset_at.is_(None)
        )
        if switch is not None:
            q = q.where(m.KillSwitchEvent.switch == switch)
        return list(s.execute(q.order_by(m.KillSwitchEvent.id).with_for_update()).scalars())

    def active(self, run_id: int, session_date: date) -> list[ActiveSwitch]:
        with self._factory() as s:
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

    def blocking(self, run_id: int, session_date: date) -> str | None:
        active = self.active(run_id, session_date)
        return active[0].switch if active else None

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
            count, avg_r = s.execute(
                select(func.count(m.Trade.id), func.avg(m.Trade.pnl_r)).where(m.Trade.run_id == run_id)
            ).one()
        start_equity = (
            start if start is not None else (starting_cash if starting_cash is not None else account.equity)
        )
        peak = max(v for v in (stored_peak, starting_cash, account.equity) if v is not None)
        return KillSwitchInputs(
            start_equity=start_equity,
            equity=account.equity,
            peak_equity=peak,
            closed_trades=int(count),
            expectancy_r=Decimal(avg_r).quantize(Q4, ROUND_HALF_UP) if avg_r is not None else None,
        )

    def evaluate(
        self, run_id: int, session_date: date, inputs: KillSwitchInputs, settings: RuntimeSettings
    ) -> list[str]:
        active = {a.switch for a in self.active(run_id, session_date)}
        crossed: list[tuple[str, Decimal, Decimal]] = []
        loss = -inputs.daily_pnl_pct
        if "daily_loss_pct" not in active and loss >= settings.killswitch_daily_loss_pct:
            crossed.append(("daily_loss_pct", loss, settings.killswitch_daily_loss_pct))
        if "max_drawdown_pct" not in active and inputs.drawdown_pct >= settings.killswitch_max_drawdown_pct:
            crossed.append(("max_drawdown_pct", inputs.drawdown_pct, settings.killswitch_max_drawdown_pct))
        if (
            "expectancy" not in active
            and inputs.closed_trades >= settings.killswitch_expectancy_min_trades
            and inputs.expectancy_r is not None
            and inputs.expectancy_r <= settings.killswitch_expectancy_threshold_r
        ):
            crossed.append(("expectancy", inputs.expectancy_r, settings.killswitch_expectancy_threshold_r))
        if not crossed:
            return []
        now = self._clock.now()
        with session_scope(self._factory) as s:
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
                log_event(
                    s,
                    self._clock,
                    "error",
                    SOURCE,
                    f"kill switch {switch} tripped: entries blocked",
                    data,
                    run_id,
                )
        return [c[0] for c in crossed]

    def pause(self, run_id: int, session_date: date, actor: str) -> bool:
        now = self._clock.now()
        with session_scope(self._factory) as s:
            if self._open_rows(s, run_id, "manual_pause"):
                return False
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
        now = self._clock.now()
        with session_scope(self._factory) as s:
            rows = self._open_rows(s, run_id, "manual_pause")
            if not rows:
                return False
            for r in rows:
                r.reset_at, r.reset_reason, r.reset_by = now, "resume", actor
            s.add(
                m.AuditLog(
                    ts=now, actor=actor, action="killswitch.resume", before={"run_id": run_id}, after=None
                )
            )
            log_event(s, self._clock, "info", SOURCE, "manual pause lifted", {"actor": actor}, run_id)
            return True

    def reset(self, run_id: int, switch: str, reason: str, actor: str) -> None:
        if switch == "manual_pause":
            raise ValueError("a manual pause is lifted with resume, not reset")
        if switch not in SWITCHES:
            raise ValueError(f"unknown switch {switch!r}")
        if not reason.strip():
            raise ValueError("a kill-switch reset needs a reason")
        now = self._clock.now()
        with session_scope(self._factory) as s:
            rows = self._open_rows(s, run_id, switch)
            if not rows:
                raise ValueError(f"kill switch {switch} is not tripped")
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
                f"kill switch {switch} reset",
                {"reason": reason.strip()},
                run_id,
            )
