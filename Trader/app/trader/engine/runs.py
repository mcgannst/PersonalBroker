"""The live run and its simulated account (SPEC §3a, §7.3, BR-22)."""

from dataclasses import dataclass
from datetime import datetime, time
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from trader.db import models as m
from trader.db.session import session_scope
from trader.market.calendar import SessionCalendar
from trader.market.clock import ET, Clock, et_date
from trader.settings_store import RuntimeSettings

Q4 = Decimal("0.0001")
Q6 = Decimal("0.000001")
LIVE_WHERE = text("mode = 'live' AND status = 'active'")


@dataclass(frozen=True, slots=True)
class RunInfo:
    id: int
    mode: str
    started_at: datetime
    label: str | None


@dataclass(frozen=True, slots=True)
class SimAccountInfo:
    id: int
    run_id: int
    currency: str
    starting_cash: Decimal
    source_amount: Decimal
    source_currency: str
    fx_rate: Decimal | None
    fx_fee: Decimal | None


@dataclass(frozen=True, slots=True)
class StartingBalance:
    amount: Decimal
    fx_rate: Decimal | None
    fx_fee: Decimal | None


def starting_balance(settings: RuntimeSettings) -> StartingBalance:
    """`starting_cash` converted once into the account currency, less the conversion fee (SPEC §7.3)."""
    src, dst = settings.starting_cash_currency, settings.account_currency
    if src == dst:
        return StartingBalance(settings.starting_cash.quantize(Q4, ROUND_HALF_UP), None, None)
    rate = settings.fx_cad_usd_rate if (src, dst) == ("CAD", "USD") else Decimal(1) / settings.fx_cad_usd_rate
    amount = (settings.starting_cash * rate * (1 - settings.fx_fee_pct)).quantize(Q4, ROUND_HALF_UP)
    return StartingBalance(amount, rate.quantize(Q6, ROUND_HALF_UP), settings.fx_fee_pct)


def _info(acct: m.SimAccount) -> SimAccountInfo:
    return SimAccountInfo(
        id=acct.id,
        run_id=acct.run_id,
        currency=acct.currency,
        starting_cash=acct.starting_cash,
        source_amount=acct.source_amount,
        source_currency=acct.source_currency,
        fx_rate=acct.fx_rate,
        fx_fee=acct.fx_fee,
    )


def ensure_sim_account(
    session: Session, run_id: int, settings: RuntimeSettings, now: datetime
) -> SimAccountInfo:
    """Create the run's account and its deposit exactly once. A concurrent creator makes this wait for its
    commit and then do nothing, so the FX conversion and fee are applied once."""
    bal = starting_balance(settings)
    new_id = session.execute(
        pg_insert(m.SimAccount)
        .values(
            run_id=run_id,
            currency=settings.account_currency,
            starting_cash=bal.amount,
            source_amount=settings.starting_cash,
            source_currency=settings.starting_cash_currency,
            fx_rate=bal.fx_rate,
            fx_fee=bal.fx_fee,
            created_at=now,
        )
        .on_conflict_do_nothing(index_elements=[m.SimAccount.run_id])
        .returning(m.SimAccount.id)
    ).scalar_one_or_none()
    if new_id is not None:
        day = et_date(now)
        session.add(
            m.CashLedger(
                run_id=run_id,
                ts=now,
                trade_date=day,
                settle_date=day,  # a deposit is settled cash at once
                currency=settings.account_currency,
                amount=bal.amount,
                kind="deposit",
                ref=f"sim_account:{new_id}",
            )
        )
        session.flush()
    acct = session.execute(select(m.SimAccount).where(m.SimAccount.run_id == run_id)).scalar_one()
    return _info(acct)


def get_live_run(factory: sessionmaker[Session], clock: Clock, settings: RuntimeSettings) -> RunInfo:
    """The one active live run, created with its account on first use (prod starts a fresh one, §15.1)."""
    now = clock.now()
    with session_scope(factory) as s:
        s.execute(
            pg_insert(m.Run)
            .values(
                mode="live",
                started_at=now,
                params={"settings": settings.model_dump(mode="json", by_alias=True)},
                status="active",
                label="live",
            )
            .on_conflict_do_nothing(index_elements=[m.Run.mode], index_where=LIVE_WHERE)
        )
        run = s.execute(select(m.Run).where(m.Run.mode == "live", m.Run.status == "active")).scalar_one()
        ensure_sim_account(s, run.id, settings, now)
        return RunInfo(run.id, run.mode, run.started_at, run.label)


def sim_account(factory: sessionmaker[Session], run_id: int) -> SimAccountInfo | None:
    with factory() as s:
        acct = s.execute(select(m.SimAccount).where(m.SimAccount.run_id == run_id)).scalar_one_or_none()
        return _info(acct) if acct is not None else None


# --- SIZECAP: a fresh live run (`trader live-run new --confirm`) --------------------------------------------
RESET_BLOCK_START = time(9, 15)  # ET: no reset from shortly before the open ...
RESET_BLOCK_END = time(16, 30)  # ... until after the close and the post-close job


class LiveRunRefused(Exception):
    """A new live run can't start now; the message says why (nothing was changed)."""


@dataclass(frozen=True, slots=True)
class NewLiveRun:
    retired_id: int | None
    run: RunInfo
    account: SimAccountInfo


def reset_block_reason(calendar: SessionCalendar, now: datetime) -> str | None:
    """Why a live-run reset is refused at `now` (09:15-16:30 ET on a session day), else None."""
    local = now.astimezone(ET)
    try:
        session = calendar.is_session(local.date())
    except ValueError:  # outside the calendar's range: not a known session
        session = False
    if session and RESET_BLOCK_START <= local.time() < RESET_BLOCK_END:
        return (
            f"{local:%Y-%m-%d %H:%M} ET is inside the trading day: a new live run can only start "
            "before 09:15 or from 16:30 ET on a session day"
        )
    return None


def _live_blockers(s: Session, run_id: int) -> list[str]:
    counts = (
        (
            select(func.count())
            .select_from(m.Position)
            .where(m.Position.run_id == run_id, m.Position.closed_at.is_(None)),
            "open position",
        ),
        (
            select(func.count())
            .select_from(m.Order)
            .where(m.Order.run_id == run_id, m.Order.status == "working"),
            "working order",
        ),
        (
            select(func.count())
            .select_from(m.Proposal)
            .where(m.Proposal.run_id == run_id, m.Proposal.status == "pending"),
            "pending proposal",
        ),
    )
    out: list[str] = []
    for stmt, what in counts:
        n = int(s.execute(stmt).scalar_one())
        if n:
            out.append(f"{n} {what}{'' if n == 1 else 's'}")
    return out


def check_new_live_run(factory: sessionmaker[Session], clock: Clock, calendar: SessionCalendar) -> None:
    """Raise LiveRunRefused when `start_new_live_run` would refuse now (read only)."""
    reason = reset_block_reason(calendar, clock.now())
    if reason is not None:
        raise LiveRunRefused(reason)
    with factory() as s:
        run_id = s.execute(
            select(m.Run.id).where(m.Run.mode == "live", m.Run.status == "active")
        ).scalar_one_or_none()
        blockers = _live_blockers(s, run_id) if run_id is not None else []
    if blockers:
        raise LiveRunRefused(f"live run {run_id} has {', '.join(blockers)}")


def start_new_live_run(
    factory: sessionmaker[Session],
    clock: Clock,
    calendar: SessionCalendar,
    settings: RuntimeSettings,
    *,
    actor: str,
) -> NewLiveRun:
    """Retire the active live run (status `completed`, every row kept) and start a new active live run with
    its sim account and deposit from `settings`, in one transaction with one `live_run.new` audit row.
    Refused (LiveRunRefused, nothing changed) from 09:15 to 16:30 ET on a session day, or while the active
    run has an open position, a working order or a pending proposal. A running worker notices the change
    (LiveRunWatch) and restarts on the new run."""
    now = clock.now()
    reason = reset_block_reason(calendar, now)
    if reason is not None:
        raise LiveRunRefused(reason)
    with session_scope(factory) as s:
        old = s.execute(
            select(m.Run).where(m.Run.mode == "live", m.Run.status == "active").with_for_update()
        ).scalar_one_or_none()
        before: dict[str, Any] | None = None
        if old is not None:
            blockers = _live_blockers(s, old.id)
            if blockers:
                raise LiveRunRefused(f"live run {old.id} has {', '.join(blockers)}")
            before = {"run_id": old.id, "status": old.status}
            old.status, old.finished_at, old.updated_at = "completed", now, now
            s.flush()  # the one-active-live-run index sees the retirement before the insert
        run = m.Run(
            mode="live",
            started_at=now,
            params={"settings": settings.model_dump(mode="json", by_alias=True)},
            status="active",
            label="live",
        )
        s.add(run)
        s.flush()
        acct = ensure_sim_account(s, run.id, settings, now)
        s.add(
            m.AuditLog(
                ts=now,
                actor=actor,
                action="live_run.new",
                before=before,
                after={
                    "run_id": run.id,
                    "retired_run_id": old.id if old is not None else None,
                    "currency": acct.currency,
                    "starting_cash": str(acct.starting_cash),
                    "source_amount": str(acct.source_amount),
                    "source_currency": acct.source_currency,
                    "fx_rate": str(acct.fx_rate) if acct.fx_rate is not None else None,
                    "fx_fee": str(acct.fx_fee) if acct.fx_fee is not None else None,
                },
            )
        )
        info = RunInfo(run.id, run.mode, run.started_at, run.label)
        return NewLiveRun(old.id if old is not None else None, info, acct)
