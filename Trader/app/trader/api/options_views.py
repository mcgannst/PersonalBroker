"""View builders for the options routes (OPTSIM task plan T14): option value types -> API schemas, and the
few database reads the routes need beside `OptionApiServices` (the run's start and starting cash, the
options worker's heartbeat, each leg's fill, every prompt of the run, the activity feed).

Conventions:
- A position's `mark` is its liquidation price, as the broker values the account: a long option at the bid,
  a short option at the ask, shares at the last trade. No such price: `mark` and `unrealized_pnl` are null.
- A position's `delta` is in share equivalents: quote delta x signed quantity x multiplier for an option,
  the signed quantity for shares.
- A structure's `unrealized_pnl` is null while one of its open positions has no mark. An open structure
  lists its open positions only; a closed one lists every position it had (all at quantity 0).
- Everything here is a pure function of its arguments, except the `*_blocking` readers, which open one
  session and are called from a worker thread.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from trader.api.forms import model_fields_out
from trader.api.schemas import (
    FieldOut,
    OptAccountOut,
    OptActivityKind,
    OptActivityOut,
    OptBenchmarkOut,
    OptChainOut,
    OptChainQuotesOut,
    OptChainRowOut,
    OptContractOut,
    OptExpiryOut,
    OptKeyValueOut,
    OptLegOut,
    OptOrderOut,
    OptPanelActionOut,
    OptPanelColumnOut,
    OptPanelOut,
    OptPanelRowOut,
    OptPanelTableOut,
    OptPositionOut,
    OptPreviewOut,
    OptPromptChoiceOut,
    OptPromptOut,
    OptQuoteOut,
    OptSourceResultOut,
    OptStrategyOut,
    OptStructureOut,
    SettingOut,
)
from trader.db import models as m
from trader.market.types import Candle
from trader.option_strategies.base import OptionStrategyConfigView, StrategyPanel
from trader.options.settings import OPTION_SETTING_KEYS, SETTINGS_GROUP, OptionSettings
from trader.options.types import (
    SOURCE_MANUAL,
    ZERO,
    ChainStrike,
    CollateralDecision,
    ExpiryInfo,
    OptionAccountState,
    OptionContract,
    OptionQuote,
    OptOrderView,
    OptPositionView,
    PromptView,
    StructureView,
    contract_label,
    dte,
)

WORKER_PROCESS = "options-worker"  # worker_heartbeats.process of the options worker (task plan §3.7)
EVENT_SOURCE_PREFIX = "options."  # event_log.source of every options event
STRATEGY_EVENT_PREFIX = "options.strategy."  # ... of a plug-in's notes and alerts
ALERT_LEVELS = frozenset({"warning", "error", "critical"})
ORDER_EVENT_PARTS = ("broker", "order")  # `options.<part>` sources whose info rows are order events
MAX_PROMPTS = 200  # the most prompts `status=all` lists
Q4 = Decimal("0.0001")
Q6 = Decimal("0.000001")
HUNDRED = Decimal(100)


def num(value: Decimal) -> str:
    """A price or amount as text: at least two decimals, at most four."""
    text = format(value.quantize(Q4), "f")
    return text[:-2] if text.endswith("00") else text


def net_words(net: Decimal) -> str:
    """A signed net price in words: `credit 0.45`, `debit 1.20`."""
    return f"{'credit' if net >= 0 else 'debit'} {num(abs(net))}"


# --- contracts, quotes, chains ------------------------------------------------------------------------------


def contract_out(contract: OptionContract, today: date) -> OptContractOut:
    return OptContractOut(
        id=contract.id,
        underlying=contract.underlying,
        expiry=contract.expiry,
        strike=contract.strike,
        right=contract.right,
        multiplier=contract.multiplier,
        is_monthly=contract.is_monthly,
        dte=dte(contract.expiry, today),
        label=contract_label(contract),
    )


def quote_out(quote: OptionQuote, *, now: datetime, market_open: bool, stale_seconds: int) -> OptQuoteOut:
    """`stale`: the market is closed, or the quote was fetched more than `stale_seconds` ago."""
    return OptQuoteOut(
        contract_id=quote.contract_id,
        bid=quote.bid,
        ask=quote.ask,
        last=quote.last,
        bid_size=quote.bid_size,
        ask_size=quote.ask_size,
        volume=quote.volume,
        open_interest=quote.open_interest,
        iv=quote.iv,
        delta=quote.delta,
        gamma=quote.gamma,
        theta=quote.theta,
        vega=quote.vega,
        fetched_at=quote.fetched_at,
        stale=not market_open or now - quote.fetched_at > timedelta(seconds=stale_seconds),
    )


def chain_out(
    underlying: str,
    expiries: Sequence[ExpiryInfo],
    *,
    underlying_price: Decimal | None,
    price_time: datetime | None,
    market_open: bool,
) -> OptChainOut:
    return OptChainOut(
        underlying=underlying,
        underlying_price=underlying_price,
        price_time=price_time,
        market_open=market_open,
        expiries=[
            OptExpiryOut(expiry=e.expiry, dte=e.dte, is_monthly=e.is_monthly, strikes=e.strikes)
            for e in expiries
        ],
    )


@dataclass(frozen=True, slots=True)
class ChainSnapshot:
    """One expiry's strikes and quotes as they were fetched at `fetched_at` (what the route caches)."""

    underlying: str
    expiry: date
    underlying_price: Decimal | None
    fetched_at: datetime
    strikes: tuple[ChainStrike, ...]
    quotes: Mapping[int, OptionQuote]  # by contract id


def chain_quotes_out(
    snap: ChainSnapshot, *, now: datetime, market_open: bool, stale_seconds: int
) -> OptChainQuotesOut:
    def side(contract_id: int | None) -> OptQuoteOut | None:
        quote = snap.quotes.get(contract_id) if contract_id is not None else None
        if quote is None:
            return None
        return quote_out(quote, now=now, market_open=market_open, stale_seconds=stale_seconds)

    return OptChainQuotesOut(
        underlying=snap.underlying,
        expiry=snap.expiry,
        underlying_price=snap.underlying_price,
        fetched_at=snap.fetched_at,
        market_open=market_open,
        rows=[
            OptChainRowOut(
                strike=row.strike,
                call_contract_id=row.call_id,
                put_contract_id=row.put_id,
                call=side(row.call_id),
                put=side(row.put_id),
            )
            for row in snap.strikes
        ],
    )


# --- orders -------------------------------------------------------------------------------------------------


def preview_out(decision: CollateralDecision) -> OptPreviewOut:
    return OptPreviewOut(
        accepted=decision.accepted,
        reject_reason=decision.reject_reason,
        detail=decision.detail,
        kind=decision.kind,
        net_at_market=decision.net_at_market,
        max_loss=decision.max_loss,
        max_profit=decision.max_profit,
        breakevens=list(decision.breakevens),
        fees=decision.fees,
        reserve_cash=decision.reserve_cash,
        cash_after=decision.cash_after,
        free_cash_after=decision.free_cash_after,
        exposure_after=decision.exposure_after,
        cap_limit=decision.cap_limit,
    )


@dataclass(frozen=True, slots=True)
class LegFillInfo:
    price: Decimal
    quote: dict[str, Any]


LegFills = Mapping[tuple[int, int], LegFillInfo]  # (order id, leg_no) -> that leg's fill


def leg_fills_blocking(
    factory: sessionmaker[Session], order_ids: Iterable[int]
) -> dict[tuple[int, int], LegFillInfo]:
    """Each filled leg's price and the quote it was priced from, for those orders."""
    ids = sorted(set(order_ids))
    if not ids:
        return {}
    with factory() as s:
        rows = s.execute(
            select(m.OptFill.order_id, m.OptOrderLeg.leg_no, m.OptFill.price, m.OptFill.quote)
            .join(m.OptOrderLeg, m.OptOrderLeg.id == m.OptFill.leg_id)
            .where(m.OptFill.order_id.in_(ids))
        ).all()
    return {
        (order_id, leg_no): LegFillInfo(price, dict(quote) if isinstance(quote, Mapping) else {})
        for order_id, leg_no, price, quote in rows
    }


def order_out(
    order: OptOrderView, contracts: Mapping[int, OptionContract], fills: LegFills, today: date
) -> OptOrderOut:
    """`contracts` by contract id (an unknown one leaves the leg's contract null)."""
    legs: list[OptLegOut] = []
    for leg in order.legs:
        contract = contracts.get(leg.contract_id) if leg.contract_id is not None else None
        fill = fills.get((order.id, leg.leg_no))
        legs.append(
            OptLegOut(
                leg_no=leg.leg_no,
                instrument=leg.instrument,
                contract=None if contract is None else contract_out(contract, today),
                side=leg.side,
                effect=leg.effect,
                ratio=leg.ratio,
                fill_price=None if fill is None else fill.price,
                fill_quote=None if fill is None else fill.quote,
            )
        )
    return OptOrderOut(
        id=order.id,
        source=order.source,
        intent=order.intent,
        structure_id=order.structure_id,
        underlying=order.underlying,
        legs=legs,
        qty=order.qty,
        order_type=order.order_type,
        net_limit=order.net_limit,
        tif=order.tif,
        status=order.status,
        walk=order.walk,
        reject_reason=order.reject_reason,
        reject_detail=order.reject_detail,
        reason=order.reason,
        reserved_cash=order.reserved_cash,
        submitted_at=order.submitted_at,
        closed_at=order.closed_at,
        fill_net=order.fill_net,
        fees=order.fees,
    )


# --- positions and structures -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Marks:
    """The prices open positions are valued at: option quotes by contract id, share prices by ticker."""

    quotes: Mapping[int, OptionQuote] = field(default_factory=dict)
    shares: Mapping[str, Decimal] = field(default_factory=dict)


def open_positions(structure: StructureView) -> list[OptPositionView]:
    return [p for p in structure.positions if p.qty != 0]


def position_out(position: OptPositionView, marks: Marks, today: date) -> OptPositionOut:
    mark: Decimal | None = None
    unrealized: Decimal | None = None
    delta: Decimal | None = None
    contract = position.contract
    if position.qty != 0:
        if contract is None:
            mark = marks.shares.get(position.underlying)
            multiplier = 1
            delta = Decimal(position.qty)
        else:
            quote = marks.quotes.get(contract.id)
            multiplier = contract.multiplier
            if quote is not None:
                mark = quote.bid if position.qty > 0 else quote.ask
                if quote.delta is not None:
                    delta = quote.delta * position.qty * multiplier
        if mark is not None:
            unrealized = (mark - position.avg_price) * position.qty * multiplier
    return OptPositionOut(
        id=position.id,
        instrument=position.instrument,
        contract=None if contract is None else contract_out(contract, today),
        qty=position.qty,
        avg_price=position.avg_price,
        mark=mark,
        unrealized_pnl=unrealized,
        delta=delta,
    )


def structure_out(structure: StructureView, marks: Marks, today: date) -> OptStructureOut:
    is_open = structure.state == "open"
    shown = open_positions(structure) if is_open else list(structure.positions)
    positions = [position_out(p, marks, today) for p in shown]
    unrealized: Decimal | None = None
    if is_open and all(p.unrealized_pnl is not None for p in positions):
        unrealized = sum((p.unrealized_pnl for p in positions if p.unrealized_pnl is not None), ZERO)
    days = [dte(p.contract.expiry, today) for p in shown if p.contract is not None] if is_open else []
    return OptStructureOut(
        id=structure.id,
        source=structure.source,
        kind=structure.kind,
        underlying=structure.underlying,
        state=structure.state,
        close_reason=structure.close_reason,
        frozen=structure.frozen,
        qty=structure.qty,
        entry_net=structure.entry_net,
        reserved_cash=structure.reserved_cash,
        take_profit_net=structure.take_profit_net,
        realized_pnl=structure.realized_pnl,
        unrealized_pnl=unrealized,
        fees_total=structure.fees_total,
        opened_at=structure.opened_at,
        closed_at=structure.closed_at,
        dte=min(days) if days else None,
        positions=positions,
    )


# --- the account --------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RunFacts:
    """What the account view reads from the database beside the broker."""

    started_at: datetime | None
    starting_cash: Decimal | None
    worker_beat_at: datetime | None


def run_facts_blocking(factory: sessionmaker[Session], run_id: int) -> RunFacts:
    with factory() as s:
        started_at = s.execute(select(m.Run.started_at).where(m.Run.id == run_id)).scalar_one_or_none()
        starting_cash = s.execute(
            select(m.SimAccount.starting_cash).where(m.SimAccount.run_id == run_id)
        ).scalar_one_or_none()
        beat_at = s.execute(
            select(m.WorkerHeartbeat.beat_at).where(m.WorkerHeartbeat.process == WORKER_PROCESS)
        ).scalar_one_or_none()
    return RunFacts(started_at, starting_cash, beat_at)


def _known_unrealized(structure: StructureView, marks: Marks, today: date) -> Decimal:
    """The unrealized P&L of the positions that have a mark (the others count 0)."""
    values = (position_out(p, marks, today).unrealized_pnl for p in open_positions(structure))
    return sum((v for v in values if v is not None), ZERO)


def by_source_out(structures: Sequence[StructureView], marks: Marks, today: date) -> list[OptSourceResultOut]:
    """One row per source (`manual` first, then the strategy keys in order), over every structure of the
    run. `premium_collected` is the entry credit of the source's credit structures (entry net x 100 x
    quantity)."""
    sources = sorted({s.source for s in structures}, key=lambda src: (src != SOURCE_MANUAL, src))
    out: list[OptSourceResultOut] = []
    for source in sources:
        own = [s for s in structures if s.source == source]
        live = [s for s in own if s.state == "open"]
        out.append(
            OptSourceResultOut(
                source=source,
                open_structures=len(live),
                reserved=sum((s.reserved_cash for s in live), ZERO),
                realized_pnl=sum((s.realized_pnl for s in own), ZERO),
                unrealized_pnl=sum((_known_unrealized(s, marks, today) for s in live), ZERO),
                premium_collected=sum((s.entry_net * HUNDRED * s.qty for s in own if s.entry_net > 0), ZERO),
            )
        )
    return out


def benchmark_out(
    ticker: str,
    since: date,
    bars: Sequence[Candle],
    *,
    equity: Decimal,
    starting_cash: Decimal | None,
) -> OptBenchmarkOut:
    """The benchmark's return from its first daily close on or after `since` to its last, beside the
    account's return on its starting cash. A return is null when it can't be computed."""
    benchmark: Decimal | None = None
    ordered = sorted(bars, key=lambda c: c.start)
    if ordered and ordered[0].close > 0:
        benchmark = (ordered[-1].close / ordered[0].close - 1).quantize(Q6)
    account: Decimal | None = None
    if starting_cash is not None and starting_cash > 0:
        account = (equity / starting_cash - 1).quantize(Q6)
    return OptBenchmarkOut(ticker=ticker, since=since, benchmark_return=benchmark, account_return=account)


def account_out(
    *,
    run_id: int,
    state: OptionAccountState,
    structures: Sequence[StructureView],
    marks: Marks,
    facts: RunFacts,
    settings: OptionSettings,
    benchmark: OptBenchmarkOut | None,
    today: date,
) -> OptAccountOut:
    """`structures` is every structure of the run, open and closed."""
    by_source = by_source_out(structures, marks, today)
    return OptAccountOut(
        run_id=run_id,
        started_at=facts.started_at,
        starting_cash=facts.starting_cash if facts.starting_cash is not None else ZERO,
        cash=state.cash,
        reserved=state.reserved,
        free_cash=state.free_cash,
        positions_value=state.positions_value,
        account_value=state.equity,
        premium_collected=state.premium_collected,
        realized_pnl=sum((s.realized_pnl for s in structures), ZERO),
        unrealized_pnl=sum((row.unrealized_pnl for row in by_source), ZERO),
        fees_total=sum((s.fees_total for s in structures), ZERO),
        max_position_pct=settings.max_position_pct,
        marks_as_of=state.as_of,
        marks_complete=state.marks_complete,
        worker_beat_at=facts.worker_beat_at,
        by_source=by_source,
        benchmark=benchmark,
    )


# --- prompts ------------------------------------------------------------------------------------------------


def prompt_out(prompt: PromptView) -> OptPromptOut:
    return OptPromptOut(
        id=prompt.id,
        source=prompt.source,
        kind=prompt.kind,
        scope_key=prompt.scope_key,
        title=prompt.title,
        body=prompt.body,
        choices=[OptPromptChoiceOut(code=c.code, label=c.label) for c in prompt.choices],
        needs_text=prompt.needs_text,
        status=prompt.status,
        asked_at=prompt.asked_at,
        answered_at=prompt.answered_at,
        answer=prompt.answer,
        answer_text=prompt.answer_text,
        answered_via=prompt.answered_via,
        data=dict(prompt.data),
    )


def _choices(stored: Any) -> list[OptPromptChoiceOut]:
    out: list[OptPromptChoiceOut] = []
    for item in stored if isinstance(stored, list) else []:
        if isinstance(item, Mapping) and "code" in item:
            out.append(OptPromptChoiceOut(code=str(item["code"]), label=str(item.get("label", ""))))
    return out


def prompt_row_out(row: m.OwnerPrompt) -> OptPromptOut:
    return OptPromptOut(
        id=row.id,
        source=row.source,
        kind=row.kind,
        scope_key=row.scope_key,
        title=row.title,
        body=row.body,
        choices=_choices(row.choices),
        needs_text=row.needs_text,
        status=cast(Any, row.status),
        asked_at=row.asked_at,
        answered_at=row.answered_at,
        answer=row.answer,
        answer_text=row.answer_text,
        answered_via=cast(Any, row.answered_via),
        data=dict(row.data) if isinstance(row.data, Mapping) else {},
    )


def all_prompts_blocking(factory: sessionmaker[Session], run_id: int) -> list[OptPromptOut]:
    """Every prompt of the run in any status, newest first (at most MAX_PROMPTS)."""
    with factory() as s:
        rows = s.execute(
            select(m.OwnerPrompt)
            .where(m.OwnerPrompt.run_id == run_id)
            .order_by(m.OwnerPrompt.asked_at.desc(), m.OwnerPrompt.id.desc())
            .limit(MAX_PROMPTS)
        ).scalars()
        return [prompt_row_out(row) for row in rows]


# --- strategies and panels ----------------------------------------------------------------------------------


def strategy_out(
    cfg: OptionStrategyConfigView,
    *,
    params_model: type[BaseModel],
    schema: dict[str, Any],
    manual_events: Sequence[str],
    open_structures: int,
) -> OptStrategyOut:
    return OptStrategyOut(
        key=cfg.strategy_key,
        version=cfg.version,
        enabled=cfg.enabled,
        revision=cfg.revision,
        params=cfg.params,
        schema=schema,
        fields=model_fields_out(params_model, by_alias=False),
        updated_at=cfg.created_at,
        updated_by=cfg.created_by,
        open_structures=open_structures,
        manual_events=list(manual_events),
    )


def panel_out(strategy_key: str, panel: StrategyPanel) -> OptPanelOut:
    def pairs(items: Iterable[Any]) -> list[OptKeyValueOut]:
        return [OptKeyValueOut(label=kv.label, value=kv.value, tone=kv.tone) for kv in items]

    return OptPanelOut(
        strategy_key=strategy_key,
        summary=pairs(panel.summary),
        tables=[
            OptPanelTableOut(
                key=table.key,
                title=table.title,
                columns=[OptPanelColumnOut(key=c.key, label=c.label, kind=c.kind) for c in table.columns],
                rows=[
                    OptPanelRowOut(
                        id=row.id, cells=dict(row.cells), actions=list(row.actions), detail=pairs(row.detail)
                    )
                    for row in table.rows
                ],
                empty_text=table.empty_text,
            )
            for table in panel.tables
        ],
        actions=[
            OptPanelActionOut(
                key=a.key, label=a.label, kind=a.kind, confirm=a.confirm, choices=list(a.choices)
            )
            for a in panel.actions
        ],
    )


# --- settings -----------------------------------------------------------------------------------------------

SETTING_FIELDS: dict[str, FieldOut] = {f.name: f for f in model_fields_out(OptionSettings, by_alias=True)}
SETTING_DEFAULTS: dict[str, Any] = OptionSettings().model_dump(mode="json", by_alias=True)


def _setting_value(key: str, row: m.Setting | None) -> Any:
    """The value in force: the stored row validated (JSON mode), else the default. A stored row that no
    longer validates is shown as it is stored, so the page can show and repair it."""
    if row is None:
        return SETTING_DEFAULTS[key]
    try:
        return OptionSettings.model_validate({key: row.value}).model_dump(mode="json", by_alias=True)[key]
    except ValidationError:
        return row.value


def setting_out(key: str, row: m.Setting | None) -> SettingOut:
    value = _setting_value(key, row)
    default = SETTING_DEFAULTS[key]
    return SettingOut(
        key=key,
        value=value,
        default=default,
        is_default=value == default and type(value) is type(default),
        group=SETTINGS_GROUP,
        field=SETTING_FIELDS[key],
        updated_at=row.updated_at if row is not None else None,
        updated_by=row.updated_by if row is not None else None,
    )


def settings_blocking(factory: sessionmaker[Session]) -> list[SettingOut]:
    """Every option setting, in declaration order."""
    with factory() as s:
        rows = {
            row.key: row
            for row in s.execute(select(m.Setting).where(m.Setting.key.in_(OPTION_SETTING_KEYS))).scalars()
        }
    return [setting_out(key, rows.get(key)) for key in SETTING_FIELDS]


def setting_blocking(factory: sessionmaker[Session], key: str) -> SettingOut:
    with factory() as s:
        return setting_out(key, s.get(m.Setting, key))


# --- the activity feed --------------------------------------------------------------------------------------


def _contract_of(row: m.OptionContract) -> OptionContract:
    return OptionContract(
        id=row.id,
        underlying=row.underlying,
        underlying_symbol_id=row.underlying_symbol_id,
        qt_symbol_id=row.qt_symbol_id,
        root=row.root,
        expiry=row.expiry,
        strike=row.strike,
        right=cast(Any, row.right),
        multiplier=row.multiplier,
        is_monthly=row.is_monthly,
        adjusted=row.adjusted,
    )


def _labels(s: Session, contract_ids: Iterable[int | None]) -> dict[int, str]:
    ids = sorted({i for i in contract_ids if i is not None})
    if not ids:
        return {}
    rows = s.execute(select(m.OptionContract).where(m.OptionContract.id.in_(ids))).scalars()
    return {row.id: contract_label(_contract_of(row)) for row in rows}


def _fill_items(s: Session, run_id: int, before: datetime | None, limit: int) -> list[OptActivityOut]:
    """One item per filled order (its legs fill together), id `fill:<order id>`."""
    q = (
        select(m.OptFill, m.OptOrderLeg, m.OptOrder, m.Symbol.ticker)
        .join(m.OptOrderLeg, m.OptOrderLeg.id == m.OptFill.leg_id)
        .join(m.OptOrder, m.OptOrder.id == m.OptFill.order_id)
        .outerjoin(m.Symbol, m.Symbol.id == m.OptOrder.underlying_symbol_id)
        .where(m.OptFill.run_id == run_id)
    )
    if before is not None:
        q = q.where(m.OptFill.ts < before)
    # At most 4 legs an order, so 4 x limit rows hold at least `limit` orders.
    rows = s.execute(q.order_by(m.OptFill.ts.desc(), m.OptFill.id.desc()).limit(limit * 4)).all()
    labels = _labels(s, (leg.contract_id for _, leg, _, _ in rows))
    grouped: dict[int, list[tuple[m.OptFill, m.OptOrderLeg]]] = {}
    orders: dict[int, tuple[m.OptOrder, str | None]] = {}
    for fill, leg, order, ticker in rows:
        grouped.setdefault(order.id, []).append((fill, leg))
        orders[order.id] = (order, ticker)
    out: list[OptActivityOut] = []
    for order_id, legs in grouped.items():
        order, ticker = orders[order_id]
        name = ticker or "?"
        parts = [
            f"{fill.side} {fill.qty} "
            f"{labels.get(leg.contract_id, name) if leg.contract_id is not None else f'{name} shares'} "
            f"at {num(fill.price)}"
            for fill, leg in sorted(legs, key=lambda pair: pair[1].leg_no)
        ]
        if order.fill_net is not None:
            parts.append(f"net {net_words(order.fill_net)}")
        out.append(
            OptActivityOut(
                id=f"fill:{order_id}",
                ts=max(fill.ts for fill, _ in legs),
                kind="fill",
                source=order.source,
                underlying=ticker,
                title=f"Filled: {order.intent} {name} x{order.qty}",
                detail="; ".join(parts),
                level="info",
                structure_id=legs[0][0].structure_id,
            )
        )
    return out


def _lifecycle_items(s: Session, run_id: int, before: datetime | None, limit: int) -> list[OptActivityOut]:
    q = (
        select(m.OptLifecycleEvent, m.OptStructure.source, m.OptStructure.underlying)
        .join(m.OptStructure, m.OptStructure.id == m.OptLifecycleEvent.structure_id)
        .where(m.OptLifecycleEvent.run_id == run_id)
    )
    if before is not None:
        q = q.where(m.OptLifecycleEvent.ts < before)
    rows = s.execute(
        q.order_by(m.OptLifecycleEvent.ts.desc(), m.OptLifecycleEvent.id.desc()).limit(limit)
    ).all()
    labels = _labels(s, (event.contract_id for event, _, _ in rows))
    out: list[OptActivityOut] = []
    for event, source, underlying in rows:
        what = labels.get(event.contract_id, underlying) if event.contract_id is not None else underlying
        parts = [f"{event.qty} x {what}"]
        if event.underlying_close is not None:
            parts.append(f"close {num(event.underlying_close)}")
        if event.shares_delta:
            parts.append(f"shares {event.shares_delta:+d}")
        if event.cash_delta:
            parts.append(f"cash {'+' if event.cash_delta > 0 else '-'}{num(abs(event.cash_delta))}")
        out.append(
            OptActivityOut(
                id=f"lifecycle:{event.id}",
                ts=event.ts,
                kind="lifecycle",
                source=source,
                underlying=underlying,
                title=f"{event.kind.replace('_', ' ').capitalize()}: {underlying}",
                detail="; ".join(parts),
                level="warning" if event.kind == "frozen" else "info",
                structure_id=event.structure_id,
            )
        )
    return out


def _prompt_items(s: Session, run_id: int, before: datetime | None, limit: int) -> list[OptActivityOut]:
    q = select(m.OwnerPrompt).where(m.OwnerPrompt.run_id == run_id)
    if before is not None:
        q = q.where(m.OwnerPrompt.asked_at < before)
    rows = s.execute(q.order_by(m.OwnerPrompt.asked_at.desc(), m.OwnerPrompt.id.desc()).limit(limit))
    out: list[OptActivityOut] = []
    for row in rows.scalars():
        answer = next((c.label for c in _choices(row.choices) if c.code == row.answer), row.answer)
        out.append(
            OptActivityOut(
                id=f"prompt:{row.id}",
                ts=row.asked_at,
                kind="prompt",
                source=row.source,
                underlying=None,
                title=row.title,
                detail=f"answered: {answer}" if row.status == "answered" else row.status,
                level="info",
                structure_id=None,
            )
        )
    return out


def event_kind(source: str, level: str) -> OptActivityKind:
    """The feed kind of an `event_log` row: a warning or worse is an `alert`; an info row of the broker or
    the orders is an `order`; anything else (a strategy's notes, the jobs) is a `decision`."""
    if level in ALERT_LEVELS:
        return "alert"
    part = source.removeprefix(EVENT_SOURCE_PREFIX)
    return "order" if part.startswith(ORDER_EVENT_PARTS) else "decision"


def _event_items(s: Session, run_id: int, before: datetime | None, limit: int) -> list[OptActivityOut]:
    q = select(m.EventLog).where(
        m.EventLog.run_id == run_id, m.EventLog.source.startswith(EVENT_SOURCE_PREFIX, autoescape=True)
    )
    if before is not None:
        q = q.where(m.EventLog.ts < before)
    rows = s.execute(q.order_by(m.EventLog.ts.desc(), m.EventLog.id.desc()).limit(limit)).scalars()
    out: list[OptActivityOut] = []
    for row in rows:
        data = row.data if isinstance(row.data, Mapping) else {}
        underlying = data.get("underlying")
        structure_id = data.get("structure_id")
        if row.source.startswith(STRATEGY_EVENT_PREFIX):
            source = row.source.removeprefix(STRATEGY_EVENT_PREFIX)
        else:
            source = row.source.removeprefix(EVENT_SOURCE_PREFIX)
        out.append(
            OptActivityOut(
                id=f"event:{row.id}",
                ts=row.ts,
                kind=event_kind(row.source, row.level),
                source=source,
                underlying=underlying if isinstance(underlying, str) else None,
                title=row.message,
                detail="",
                level=row.level,
                structure_id=structure_id
                if isinstance(structure_id, int) and not isinstance(structure_id, bool)
                else None,
            )
        )
    return out


def activity_blocking(
    factory: sessionmaker[Session], run_id: int, *, before: datetime | None, limit: int
) -> list[OptActivityOut]:
    """The run's fills, lifecycle events, prompts and `options.*` event-log rows, merged, newest first (a
    tie is broken by the item id, so the order is stable). `before`: only items older than it."""
    with factory() as s:
        items = [
            *_fill_items(s, run_id, before, limit),
            *_lifecycle_items(s, run_id, before, limit),
            *_prompt_items(s, run_id, before, limit),
            *_event_items(s, run_id, before, limit),
        ]
    items.sort(key=lambda item: (item.ts, item.id), reverse=True)
    return items[:limit]
