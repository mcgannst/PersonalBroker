"""Builders for option tests (OPTSIM task plan §3.10).

Row builders (`add_*`) flush and return the new primary key; the caller commits. Value builders (`make_*`)
touch no database. Symbols come from `tests.factories.add_symbol` (re-exported here as `add_underlying`'s
base)."""

import itertools
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from tests.factories import add_symbol
from trader.db import models as m
from trader.options.account import OPTIONS_CURRENCY, OPTIONS_LABEL, OPTIONS_MODE, SETTINGS_PARAM
from trader.options.settings import OptionSettings
from trader.options.types import (
    SOURCE_MANUAL,
    Effect,
    Instrument,
    OptionContract,
    OptionQuote,
    OptOrderType,
    OrderIntent,
    OrderLeg,
    OrderRequest,
    Right,
    Side,
    Tif,
)

T0 = datetime(2026, 10, 6, 14, 0, tzinfo=UTC)  # Tuesday 10:00 ET, inside the session
SESSION = date(2026, 10, 6)
EXPIRY = date(2026, 11, 20)  # the November monthly (third Friday), 45 days after SESSION
_QT_IDS = itertools.count(50_000_001)  # Questrade ids for contracts that were not given one


# --- rows ---------------------------------------------------------------------------------------------------


def add_options_run(
    s: Session,
    cash: Decimal | int | str = 5000,
    *,
    status: str = "active",
    started_at: datetime = T0,
) -> int:
    """An options run with its USD account and its one deposit (what `start_options_run` creates)."""
    amount = Decimal(str(cash))
    run = m.Run(
        mode=OPTIONS_MODE,
        started_at=started_at,
        params={SETTINGS_PARAM: OptionSettings().model_dump(mode="json", by_alias=True)},
        status=status,
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
        created_at=started_at,
    )
    s.add(acct)
    s.flush()
    s.add(
        m.CashLedger(
            run_id=run.id,
            ts=started_at,
            trade_date=started_at.date(),
            settle_date=started_at.date(),
            currency=OPTIONS_CURRENCY,
            amount=amount,
            kind="deposit",
            ref=f"sim_account:{acct.id}",
        )
    )
    s.flush()
    return run.id


def add_underlying(s: Session, ticker: str = "F", *, questrade_id: int | None = None) -> int:
    """A `symbols` row for an underlying; returns its id."""
    return add_symbol(s, ticker, questrade_id=questrade_id, exchange="NYSE")


def add_contract(
    s: Session,
    symbol_id: int,
    *,
    underlying: str = "F",
    expiry: date = EXPIRY,
    strike: Decimal | str = "14.50",
    right: Right = "put",
    qt_symbol_id: int | None = None,
    root: str | None = None,
    multiplier: int = 100,
    is_monthly: bool = True,
    adjusted: bool = False,
    first_seen_at: datetime = T0,
) -> int:
    """A contract-master row. Without `qt_symbol_id` it gets the next id of a process-wide counter."""
    row = m.OptionContract(
        underlying_symbol_id=symbol_id,
        underlying=underlying,
        qt_symbol_id=qt_symbol_id if qt_symbol_id is not None else next(_QT_IDS),
        root=root or underlying,
        expiry=expiry,
        strike=Decimal(str(strike)),
        right=right,
        multiplier=multiplier,
        is_monthly=is_monthly,
        adjusted=adjusted,
        first_seen_at=first_seen_at,
    )
    s.add(row)
    s.flush()
    return row.id


def add_structure(
    s: Session,
    run_id: int,
    symbol_id: int,
    *,
    underlying: str = "F",
    kind: str = "csp",
    source: str = SOURCE_MANUAL,
    strategy_config_id: int | None = None,
    state: str = "open",
    close_reason: str | None = None,
    frozen: bool = False,
    qty: int = 1,
    entry_net: Decimal | str = "0.45",
    reserved_cash: Decimal | str = "0",
    take_profit_net: Decimal | str | None = None,
    cover_structure_id: int | None = None,
    parent_structure_id: int | None = None,
    opened_at: datetime = T0,
    closed_at: datetime | None = None,
    meta: Mapping[str, Any] | None = None,
) -> int:
    row = m.OptStructure(
        run_id=run_id,
        source=source,
        strategy_config_id=strategy_config_id,
        kind=kind,
        underlying_symbol_id=symbol_id,
        underlying=underlying,
        state=state,
        close_reason=close_reason,
        frozen=frozen,
        qty=qty,
        entry_net=Decimal(str(entry_net)),
        reserved_cash=Decimal(str(reserved_cash)),
        take_profit_net=None if take_profit_net is None else Decimal(str(take_profit_net)),
        cover_structure_id=cover_structure_id,
        parent_structure_id=parent_structure_id,
        opened_at=opened_at,
        closed_at=closed_at,
        meta=dict(meta or {}),
    )
    s.add(row)
    s.flush()
    return row.id


def add_position(
    s: Session,
    run_id: int,
    structure_id: int,
    symbol_id: int,
    *,
    contract_id: int | None = None,
    qty: int = -1,
    avg_price: Decimal | str = "0.45",
    opened_at: datetime = T0,
    closed_at: datetime | None = None,
) -> int:
    """An option position (`contract_id` given) or a shares position (None)."""
    row = m.OptPosition(
        run_id=run_id,
        structure_id=structure_id,
        instrument="shares" if contract_id is None else "option",
        contract_id=contract_id,
        underlying_symbol_id=symbol_id,
        qty=qty,
        avg_price=Decimal(str(avg_price)),
        opened_at=opened_at,
        closed_at=closed_at,
    )
    s.add(row)
    s.flush()
    return row.id


def add_order(
    s: Session,
    run_id: int,
    symbol_id: int,
    *,
    legs: Sequence[OrderLeg] = (),
    source: str = SOURCE_MANUAL,
    strategy_config_id: int | None = None,
    intent: str = "open",
    structure_id: int | None = None,
    order_type: str = "limit",
    net_limit: Decimal | str | None = "0.45",
    tif: str = "day",
    qty: int = 1,
    status: str = "working",
    walk: bool = False,
    reason: str = "test",
    reserved_cash: Decimal | str = "0",
    session_date: date = SESSION,
    submitted_at: datetime = T0,
    submitted_by: str = "test",
) -> int:
    """An order row and one `opt_order_legs` row per leg."""
    row = m.OptOrder(
        run_id=run_id,
        source=source,
        strategy_config_id=strategy_config_id,
        intent=intent,
        structure_id=structure_id,
        underlying_symbol_id=symbol_id,
        order_type=order_type,
        net_limit=None if net_limit is None else Decimal(str(net_limit)),
        tif=tif,
        qty=qty,
        status=status,
        walk=walk,
        reason=reason,
        evidence={},
        reserved_cash=Decimal(str(reserved_cash)),
        session_date=session_date,
        submitted_at=submitted_at,
        submitted_by=submitted_by,
        updated_at=submitted_at,
    )
    s.add(row)
    s.flush()
    for leg in legs:
        s.add(
            m.OptOrderLeg(
                order_id=row.id,
                leg_no=leg.leg_no,
                instrument=leg.instrument,
                contract_id=leg.contract_id,
                underlying_symbol_id=symbol_id,
                side=leg.side,
                effect=leg.effect,
                ratio=leg.ratio,
            )
        )
    s.flush()
    return row.id


# --- values -------------------------------------------------------------------------------------------------


def make_contract(
    id: int = 1,
    *,
    underlying: str = "F",
    expiry: date = EXPIRY,
    strike: Decimal | str = "14.50",
    right: Right = "put",
    underlying_symbol_id: int = 1,
    qt_symbol_id: int | None = None,
    root: str | None = None,
    multiplier: int = 100,
    is_monthly: bool = True,
    adjusted: bool = False,
) -> OptionContract:
    return OptionContract(
        id=id,
        underlying=underlying,
        underlying_symbol_id=underlying_symbol_id,
        qt_symbol_id=qt_symbol_id if qt_symbol_id is not None else 900_000 + id,
        root=root or underlying,
        expiry=expiry,
        strike=Decimal(str(strike)),
        right=right,
        multiplier=multiplier,
        is_monthly=is_monthly,
        adjusted=adjusted,
    )


def _dec(value: Decimal | str | int | None) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def make_quote(
    contract_id: int = 1,
    bid: Decimal | str | None = "0.45",
    ask: Decimal | str | None = "0.50",
    *,
    at: datetime = T0,
    last: Decimal | str | None = None,
    bid_size: int | None = 10,
    ask_size: int | None = 10,
    volume: int | None = 100,
    open_interest: int | None = 1000,
    iv: Decimal | str | None = "0.35",
    delta: Decimal | str | None = "-0.25",
    gamma: Decimal | str | None = None,
    theta: Decimal | str | None = None,
    vega: Decimal | str | None = None,
    last_trade_time: datetime | None = None,
    delay: int | None = 0,
    is_halted: bool = False,
) -> OptionQuote:
    """A real-time quote fetched at `at`."""
    return OptionQuote(
        contract_id=contract_id,
        bid=_dec(bid),
        ask=_dec(ask),
        last=_dec(last),
        bid_size=bid_size,
        ask_size=ask_size,
        volume=volume,
        open_interest=open_interest,
        iv=_dec(iv),
        delta=_dec(delta),
        gamma=_dec(gamma),
        theta=_dec(theta),
        vega=_dec(vega),
        last_trade_time=last_trade_time,
        delay=delay,
        is_halted=is_halted,
        fetched_at=at,
    )


def make_leg(
    leg_no: int = 1,
    *,
    instrument: Instrument = "option",
    side: Side = "sell",
    effect: Effect = "open",
    ratio: int = 1,
    underlying: str = "F",
    contract_id: int | None = 1,
) -> OrderLeg:
    """An option leg by default; `instrument="shares"` drops the contract."""
    return OrderLeg(
        leg_no=leg_no,
        instrument=instrument,
        side=side,
        effect=effect,
        ratio=ratio,
        underlying=underlying,
        contract_id=None if instrument == "shares" else contract_id,
    )


def make_request(
    legs: Sequence[OrderLeg] | None = None,
    *,
    source: str = SOURCE_MANUAL,
    strategy_config_id: int | None = None,
    intent: OrderIntent = "open",
    structure_id: int | None = None,
    underlying: str = "F",
    qty: int = 1,
    order_type: OptOrderType = "limit",
    net_limit: Decimal | None = Decimal("0.45"),
    tif: Tif = "day",
    walk: bool = False,
    take_profit_pct: Decimal | None = None,
    reason: str = "test",
    evidence: Mapping[str, Any] | None = None,
    submitted_by: str = "test",
) -> OrderRequest:
    """By default: sell to open one put (contract 1) on F at a 0.45 credit limit."""
    return OrderRequest(
        source=source,
        strategy_config_id=strategy_config_id,
        intent=intent,
        structure_id=structure_id,
        underlying=underlying,
        legs=tuple(legs) if legs is not None else (make_leg(underlying=underlying),),
        qty=qty,
        order_type=order_type,
        net_limit=None if order_type == "market" else net_limit,
        tif=tif,
        walk=walk,
        take_profit_pct=take_profit_pct,
        reason=reason,
        evidence=dict(evidence or {}),
        submitted_by=submitted_by,
    )
