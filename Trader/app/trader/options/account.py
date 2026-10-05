"""The options run and its simulated account (OPTSIM task plan §3.6).

The options run is a `runs` row with mode `options`, status `active`, label `options`; at most one is active
(index `uq_runs_one_active_options`), beside the stock live run, which `trader.engine.runs` keeps finding by
mode `live`. Its account is a `sim_accounts` row in USD with one `deposit` ledger row. Option cash is the
cash ledger's TOTAL for the run (premium is usable at once: a documented simulation limit).
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker

from trader.broker.ledger import Ledger
from trader.db import models as m
from trader.db.session import session_scope
from trader.engine.runs import RESET_BLOCK_END, RESET_BLOCK_START, SimAccountInfo, sim_account
from trader.market.calendar import SessionCalendar
from trader.market.clock import ET, Clock, et_date
from trader.options.settings import OptionSettings

Q4 = Decimal("0.0001")
OPTIONS_MODE = "options"
OPTIONS_LABEL = "options"
OPTIONS_CURRENCY = "USD"
OPTIONS_WHERE = text("mode = 'options' AND status = 'active'")
SETTINGS_PARAM = "options_settings"  # runs.params key holding the OptionSettings dump


@dataclass(frozen=True, slots=True)
class OptionsRun:
    run_id: int
    account_id: int
    retired_run_id: int | None


class OptionsRunRefused(Exception):
    """A new options run can't start now; the message says why (nothing was changed)."""


def lock_book(session: Session, run_id: int) -> None:
    """Global rule 6, the one book lock: every transaction that changes the options run's cash, orders,
    structures or positions takes this first. It is held until the transaction ends."""
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext('options_book:' || :run_id))"), {"run_id": str(run_id)}
    )


def active_options_run_id(factory: sessionmaker[Session]) -> int | None:
    with factory() as s:
        return s.execute(
            select(m.Run.id).where(m.Run.mode == OPTIONS_MODE, m.Run.status == "active")
        ).scalar_one_or_none()


def options_account(factory: sessionmaker[Session], run_id: int) -> SimAccountInfo | None:
    return sim_account(factory, run_id)


def options_block_reason(calendar: SessionCalendar, now: datetime) -> str | None:
    """Why a new options run is refused at `now` (09:15-16:30 ET on a session day), else None."""
    local = now.astimezone(ET)
    try:
        session = calendar.is_session(local.date())
    except ValueError:  # outside the calendar's range: not a known session
        session = False
    if session and RESET_BLOCK_START <= local.time() < RESET_BLOCK_END:
        return (
            f"{local:%Y-%m-%d %H:%M} ET is inside the trading day: a new options run can only start "
            "before 09:15 or from 16:30 ET on a session day"
        )
    return None


def _blockers(s: Session, run_id: int) -> list[str]:
    counts = (
        (
            select(func.count())
            .select_from(m.OptStructure)
            .where(m.OptStructure.run_id == run_id, m.OptStructure.state == "open"),
            "open structure",
        ),
        (
            select(func.count())
            .select_from(m.OptOrder)
            .where(m.OptOrder.run_id == run_id, m.OptOrder.status == "working"),
            "working order",
        ),
    )
    out: list[str] = []
    for stmt, what in counts:
        n = int(s.execute(stmt).scalar_one())
        if n:
            out.append(f"{n} {what}{'' if n == 1 else 's'}")
    return out


def start_options_run(
    factory: sessionmaker[Session],
    clock: Clock,
    calendar: SessionCalendar,
    settings: OptionSettings,
    *,
    cash: Decimal,
    actor: str,
) -> OptionsRun:
    """Retire the active options run (status `completed`, every row kept) and start a new active one with a
    USD account holding `cash` and its one deposit, in one transaction with one `options_run.new` audit row.
    Refused (OptionsRunRefused, nothing changed) from 09:15 to 16:30 ET on a session day, or while the
    active run has an open structure or a working order."""
    if not isinstance(cash, Decimal):
        raise TypeError(f"cash must be a Decimal, not {type(cash).__name__} ({cash!r})")
    amount = cash.quantize(Q4, ROUND_HALF_UP)
    if amount <= 0:
        raise ValueError("cash must be positive")
    now = clock.now()
    reason = options_block_reason(calendar, now)
    if reason is not None:
        raise OptionsRunRefused(reason)
    with session_scope(factory) as s:
        old = s.execute(
            select(m.Run).where(m.Run.mode == OPTIONS_MODE, m.Run.status == "active").with_for_update()
        ).scalar_one_or_none()
        before: dict[str, Any] | None = None
        if old is not None:
            lock_book(s, old.id)  # no fill or submit interleaves with the retirement
            blockers = _blockers(s, old.id)
            if blockers:
                raise OptionsRunRefused(f"options run {old.id} has {', '.join(blockers)}")
            before = {"run_id": old.id, "status": old.status}
            old.status, old.finished_at, old.updated_at = "completed", now, now
            s.flush()  # the one-active-options-run index sees the retirement before the insert
        run = m.Run(
            mode=OPTIONS_MODE,
            started_at=now,
            params={SETTINGS_PARAM: settings.model_dump(mode="json", by_alias=True)},
            status="active",
            label=OPTIONS_LABEL,
        )
        s.add(run)
        s.flush()
        acct = m.SimAccount(
            run_id=run.id,
            currency=OPTIONS_CURRENCY,
            starting_cash=amount,
            source_amount=amount,
            source_currency=OPTIONS_CURRENCY,
            fx_rate=None,
            fx_fee=None,
            created_at=now,
        )
        s.add(acct)
        s.flush()
        Ledger(calendar).record(
            s,
            run_id=run.id,
            ts=now,
            trade_date=et_date(now),
            amount=amount,
            kind="deposit",  # settled the same day
            ref=f"sim_account:{acct.id}",
            currency=OPTIONS_CURRENCY,
        )
        retired = old.id if old is not None else None
        s.add(
            m.AuditLog(
                ts=now,
                actor=actor,
                action="options_run.new",
                before=before,
                after={
                    "run_id": run.id,
                    "retired_run_id": retired,
                    "currency": OPTIONS_CURRENCY,
                    "starting_cash": str(amount),
                },
            )
        )
        return OptionsRun(run.id, acct.id, retired)
