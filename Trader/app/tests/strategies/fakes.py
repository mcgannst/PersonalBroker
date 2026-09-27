"""Shared strategy-test fakes: market data and catalysts held in plain dicts (no DB, no network)."""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from pydantic import BaseModel

from trader.adapters.questrade.models import QtQuote
from trader.broker.types import AccountState, OrderView, PositionView
from trader.market.calendar import SessionCalendar
from trader.market.clock import FixedClock
from trader.market.types import Candle, Interval, OpenBarStats, OpeningBars, UniverseMember, UniverseStatus
from trader.strategies.base import StrategyContext

CAL = SessionCalendar()
SESSION = date(2026, 10, 6)  # a Tuesday
NOW = datetime(2026, 10, 6, 13, 35, 5, tzinfo=UTC)  # 09:35:05 ET


def bar(o: str, h: str, low: str, c: str, volume: int, start: datetime | None = None) -> Candle:
    start = start or CAL.session_open(SESSION)
    return Candle(
        start, start + timedelta(minutes=5), Decimal(o), Decimal(h), Decimal(low), Decimal(c), volume, None
    )


def quote(symbol_id: int, bid: str, ask: str, last: str, at: datetime = NOW, age: float = 1.0) -> QtQuote:
    return QtQuote(
        symbol_id=symbol_id,
        symbol=f"S{symbol_id}",
        bid=Decimal(bid),
        ask=Decimal(ask),
        last=Decimal(last),
        last_regular=None,
        volume=100_000,
        last_trade_time=at - timedelta(seconds=age),
        delay=0,
        is_halted=False,
        vwap=None,
    )


@dataclass
class FakeCatalyst:
    catalyst_type: str = "earnings_beat"
    direction: str = "bullish"
    quality: int | None = 80
    classified: bool = True


class FakeCatalysts:
    def __init__(self, by_symbol: dict[int, FakeCatalyst] | None = None) -> None:
        self.by_symbol = by_symbol or {}
        self.requested: list[list[int]] = []

    async def get(self, symbol_ids: Sequence[int], session_date: date) -> dict[int, FakeCatalyst]:
        self.requested.append(list(symbol_ids))
        return {s: self.by_symbol[s] for s in symbol_ids if s in self.by_symbol}


@dataclass
class FakeData:
    members: list[UniverseMember] = field(default_factory=list)
    stats: dict[int, OpenBarStats] = field(default_factory=dict)
    bars: dict[int, Candle] = field(default_factory=dict)
    missing: dict[int, str] = field(default_factory=dict)
    quote_map: dict[int, QtQuote] = field(default_factory=dict)
    closes: dict[int, Decimal] = field(default_factory=dict)
    ids: dict[str, int] = field(default_factory=dict)
    status: UniverseStatus = field(default_factory=lambda: UniverseStatus("finviz", None, False, None))

    def add(
        self,
        symbol_id: int,
        ticker: str,
        opening: Candle | None,
        *,
        avg_open_vol: str | None = "1000",
        atr: str | None = "1.00",
        price: str = "20",
        avg_volume: int | None = 2_000_000,
    ) -> None:
        self.members.append(
            UniverseMember(
                symbol_id,
                ticker,
                f"{ticker} Inc",
                Decimal(price),
                avg_volume,
                Decimal(atr) if atr else None,
                "finviz",
            )
        )
        self.stats[symbol_id] = OpenBarStats(
            symbol_id, Decimal(avg_open_vol) if avg_open_vol else None, Decimal(atr) if atr else None
        )
        self.ids[ticker] = symbol_id
        if opening is not None:
            self.bars[symbol_id] = opening
        else:
            self.missing[symbol_id] = "no_bar_at_open"

    async def universe(self, session_date: date) -> list[UniverseMember]:
        return list(self.members)

    async def universe_status(self, session_date: date) -> UniverseStatus:
        return self.status

    async def open_bar_stats(self, session_date: date) -> dict[int, OpenBarStats]:
        return dict(self.stats)

    async def opening_bars(self, session_date: date, symbol_ids: Sequence[int] | None = None) -> OpeningBars:
        ids = list(symbol_ids) if symbol_ids is not None else [m.symbol_id for m in self.members]
        return OpeningBars(
            {i: self.bars[i] for i in ids if i in self.bars},
            {i: self.missing[i] for i in ids if i in self.missing},
        )

    async def quotes(self, symbol_ids: Sequence[int]) -> dict[int, QtQuote]:
        return {i: self.quote_map[i] for i in symbol_ids if i in self.quote_map}

    async def candles(
        self, symbol_id: int, start: datetime, end: datetime, interval: Interval
    ) -> list[Candle]:
        return []

    async def prior_close(self, symbol_id: int, session_date: date) -> Decimal | None:
        return self.closes.get(symbol_id)

    async def symbol_ids(self, tickers: Sequence[str]) -> dict[str, int]:
        return {t: self.ids[t] for t in tickers if t in self.ids}


ACCOUNT = AccountState(Decimal("720"), Decimal("720"), Decimal("720"), Decimal("0"), Decimal("720"))


def make_ctx(
    data: FakeData,
    params: BaseModel,
    catalysts: FakeCatalysts | None = None,
    *,
    positions: Sequence[PositionView] = (),
    orders: Sequence[OrderView] = (),
    entries_today: int = 0,
    now: datetime = NOW,
    session: date = SESSION,
    config_id: int = 1,
) -> StrategyContext:
    return StrategyContext(
        clock=FixedClock(now),
        calendar=CAL,
        session_date=session,
        data=data,
        catalysts=catalysts or FakeCatalysts(),
        params=params,
        strategy_config_id=config_id,
        positions=list(positions),
        working_orders=list(orders),
        account=ACCOUNT,
        entries_today=entries_today,
    )


def position(
    pid: int, symbol_id: int, *, qty: int = 10, stop_loss: str = "19.90", config_id: int = 1
) -> PositionView:
    return PositionView(
        id=pid,
        symbol_id=symbol_id,
        strategy_config_id=config_id,
        qty=qty,
        avg_price=Decimal("20.00"),
        stop_loss=Decimal(stop_loss),
        opened_at=NOW,
        session_date=SESSION,
        stop_order_id=None,
        unprotected_since=NOW,
        unprotected_seconds=0,
    )


def working_entry(oid: int, symbol_id: int, *, config_id: int = 1) -> OrderView:
    return OrderView(
        id=oid,
        symbol_id=symbol_id,
        side="buy",
        order_type="stop",
        purpose="entry",
        qty=10,
        stop=Decimal("20.01"),
        limit=None,
        status="working",
        position_id=None,
        strategy_config_id=config_id,
        proposal_id=None,
        submitted_at=NOW,
    )
