"""The live run and its simulated account (SPEC §3a, §7.3, BR-22)."""

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from trader.db import models as m
from trader.db.session import session_scope
from trader.market.clock import Clock, et_date
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
