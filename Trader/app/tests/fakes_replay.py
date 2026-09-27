"""Phase 5 test fakes for the replay contracts (P5-T1). No network, no wall clock, no randomness.

- `candle(start, o, h, l, c, v)` and `minute_series(start, closes)`: Candle builders (prices as str, int
  or Decimal, never float).
- `FakeReplayMarket`: a `ReplayMarket` over in-memory universes, stats, opening bars, 1-minute bars and prior
  closes, filtered like the real one (never a bar ending after its clock), with synthetic quotes from the last
  complete 1-minute bar. Records `prepare_day` and `load_minute_bars` calls.
- `FakeReplayBroker` / `FakeReplayEngine`: a `ReplayBroker` and a `ReplayEngine` that record every call in
  order with the clock time; `FakeReplayEngine(hook=...)` lets a test act at each call (add or remove working
  orders, raise, flip `cancel_requested`).
- `seed_replay_world(factory, ...)`: symbols, the live run, live strategy configs, universe snapshots,
  open-bar stats, candle-archive, daily-candle and catalyst rows (used by T5, T6 and T18).
"""

import inspect
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from trader.adapters.questrade.models import QtQuote
from trader.broker.types import FillEvent, OrderSpec, OrderView, PositionView
from trader.db import models as m
from trader.db.session import session_scope
from trader.engine.orchestrator import EventResult
from trader.engine.runs import get_live_run
from trader.market.clock import Clock, FixedClock, et_date
from trader.market.types import (
    INTERVAL_CODES,
    Candle,
    Interval,
    OpenBarStats,
    OpeningBars,
    UniverseMember,
    UniverseStatus,
)
from trader.settings_store import RuntimeSettings
from trader.strategies.registry import StrategyRegistry

Price = Decimal | str | int
ONE_MINUTE = timedelta(minutes=1)
Q4 = Decimal("0.0001")
BPS = Decimal("0.0001")
WORLD_T0 = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)  # before the golden week; the seeded rows' created_at


def _d(x: Price) -> Decimal:
    if isinstance(x, float):  # money is never a float (Global Constraints)
        raise TypeError("pass prices as str, int or Decimal, never float")
    return x if isinstance(x, Decimal) else Decimal(str(x))


def candle(
    start: datetime,
    o: Price,
    h: Price,
    l: Price,  # noqa: E741  (open, high, low, close)
    c: Price,
    v: int = 1000,
    *,
    minutes: int = 1,
    vwap: Price | None = None,
) -> Candle:
    """One bar starting at `start` (UTC-aware), `minutes` long (1 by default, 5 for an opening bar)."""
    if start.tzinfo is None:
        raise ValueError("candle start must be timezone-aware")
    return Candle(
        start=start,
        end=start + timedelta(minutes=minutes),
        open=_d(o),
        high=_d(h),
        low=_d(l),
        close=_d(c),
        volume=v,
        vwap=_d(vwap) if vwap is not None else None,
    )


def minute_series(
    start: datetime, closes: Sequence[Price], *, first_open: Price | None = None, volume: int = 1000
) -> list[Candle]:
    """Consecutive 1-minute bars from `start`: each opens at the previous close (the first at `first_open`,
    else its own close), high/low are the max/min of open and close."""
    bars: list[Candle] = []
    prev = _d(first_open) if first_open is not None else None
    for i, close in enumerate(closes):
        c = _d(close)
        o = prev if prev is not None else c
        bars.append(candle(start + i * ONE_MINUTE, o, max(o, c), min(o, c), c, volume))
        prev = c
    return bars


# --- the market ---------------------------------------------------------------------------------------------


class FakeReplayMarket:
    """An in-memory `ReplayMarket`. Every strategy-facing read ignores bars ending after `clock.now()`."""

    def __init__(self, clock: Clock, *, half_spread_bps: Decimal = Decimal("5")) -> None:
        self.clock = clock
        self.half_spread_bps = half_spread_bps
        self.tickers: dict[str, int] = {}
        self.universes: dict[date, list[UniverseMember]] = {}
        self.biased: set[date] = set()
        self.stats: dict[date, dict[int, OpenBarStats]] = {}
        self.opening: dict[date, dict[int, Candle]] = {}
        self.minute: dict[tuple[int, date], list[Candle]] = {}
        self.five_minute: dict[tuple[int, date], list[Candle]] = {}
        self.closes: dict[tuple[int, date], Decimal] = {}  # daily close per (symbol, session)
        self.loaded: set[tuple[int, date]] = set()  # (symbol, day) minute bars loaded by load_minute_bars
        self.prepared: list[date] = []
        self.load_calls: list[tuple[tuple[int, ...], date]] = []
        self.counts: dict[str, int] = {
            "missing_opening_bars": 0,
            "missing_minute_bars": 0,
            "questrade_requests": 0,
        }

    # --- building the world ---------------------------------------------------------------------------------
    def add_symbol(self, ticker: str, symbol_id: int) -> None:
        self.tickers[ticker] = symbol_id

    def set_universe(self, day: date, members: Iterable[UniverseMember], *, biased: bool = False) -> None:
        self.universes[day] = sorted(members, key=lambda u: u.symbol_id)
        if biased:
            self.biased.add(day)

    def set_stats(self, day: date, stats: Iterable[OpenBarStats]) -> None:
        self.stats[day] = {s.symbol_id: s for s in stats}

    def set_opening_bar(self, day: date, symbol_id: int, bar: Candle) -> None:
        self.opening.setdefault(day, {})[symbol_id] = bar

    def set_minute_bars(self, symbol_id: int, day: date, bars: Iterable[Candle]) -> None:
        self.minute[(symbol_id, day)] = sorted(bars, key=lambda b: b.start)

    def set_close(self, symbol_id: int, day: date, close: Price) -> None:
        self.closes[(symbol_id, day)] = _d(close)

    # --- MarketDataView ------------------------------------------------------------------------------------
    def _now(self) -> datetime:
        return self.clock.now()

    async def universe(self, session_date: date) -> list[UniverseMember]:
        return list(self.universes.get(session_date, []))

    async def universe_status(self, session_date: date) -> UniverseStatus:
        if session_date in self.biased:
            return UniverseStatus(source="biased", fallback_from=None, stale=False, age_sessions=None)
        if session_date in self.universes:
            return UniverseStatus(source="finviz", fallback_from=None, stale=False, age_sessions=0)
        return UniverseStatus(source=None, fallback_from=None, stale=False, age_sessions=None)

    async def open_bar_stats(self, session_date: date) -> dict[int, OpenBarStats]:
        return dict(sorted(self.stats.get(session_date, {}).items()))

    async def opening_bars(self, session_date: date, symbol_ids: Sequence[int] | None = None) -> OpeningBars:
        day = self.opening.get(session_date, {})
        ids = (
            sorted(symbol_ids)
            if symbol_ids is not None
            else sorted(u.symbol_id for u in self.universes.get(session_date, []))
        )
        bars: dict[int, Candle] = {}
        missing: dict[int, str] = {}
        for sid in ids:
            bar = day.get(sid)
            if bar is None:
                missing[sid] = "no_archived_bar"
            elif bar.end > self._now():
                missing[sid] = "bar_not_complete"
            else:
                bars[sid] = bar
        return OpeningBars(bars=bars, missing=missing)

    def _complete_minutes(self, symbol_id: int, day: date, at: datetime) -> list[Candle]:
        return [b for b in self.minute.get((symbol_id, day), []) if b.end <= at]

    async def quotes(self, symbol_ids: Sequence[int]) -> dict[int, QtQuote]:
        now = self._now()
        day = et_date(now)
        out: dict[int, QtQuote] = {}
        for sid in sorted(symbol_ids):
            if (sid, day) not in self.loaded:
                await self.load_minute_bars([sid], day)
            done = self._complete_minutes(sid, day, now)
            if not done:
                continue
            bar = done[-1]
            hs = (bar.close * self.half_spread_bps * BPS).quantize(Q4)
            out[sid] = QtQuote(
                symbol_id=sid,
                symbol=next((t for t, i in self.tickers.items() if i == sid), f"S{sid}"),
                bid=bar.close - hs,
                ask=bar.close + hs,
                last=bar.close,
                last_regular=bar.close,
                volume=bar.volume,
                last_trade_time=bar.end,
                delay=0,
                is_halted=False,
                vwap=None,
            )
        return out

    async def candles(
        self, symbol_id: int, start: datetime, end: datetime, interval: Interval
    ) -> list[Candle]:
        now = self._now()
        source = self.minute if INTERVAL_CODES[interval] == "1m" else self.five_minute
        bars = [b for (sid, _), day in source.items() if sid == symbol_id for b in day]
        return sorted(
            (b for b in bars if start <= b.start and b.end <= end and b.end <= now), key=lambda b: b.start
        )

    async def prior_close(self, symbol_id: int, session_date: date) -> Decimal | None:
        earlier = sorted(d for (sid, d) in self.closes if sid == symbol_id and d < session_date)
        return self.closes[(symbol_id, earlier[-1])] if earlier else None

    async def prior_closes(self, symbol_ids: Sequence[int], session_date: date) -> dict[int, Decimal]:
        out: dict[int, Decimal] = {}
        for sid in sorted(symbol_ids):
            close = await self.prior_close(sid, session_date)
            if close is not None:
                out[sid] = close
        return out

    async def symbol_ids(self, tickers: Sequence[str]) -> dict[str, int]:
        return {t: self.tickers[t] for t in sorted(tickers) if t in self.tickers}

    # --- runner-only helpers ------------------------------------------------------------------------------
    async def prepare_day(self, session_date: date) -> None:
        self.prepared.append(session_date)
        self.loaded = {(sid, d) for (sid, d) in self.loaded if d == session_date}

    async def load_minute_bars(self, symbol_ids: Sequence[int], session_date: date) -> None:
        ids = tuple(sorted(symbol_ids))
        self.load_calls.append((ids, session_date))
        for sid in ids:
            if (sid, session_date) in self.loaded:
                continue
            self.loaded.add((sid, session_date))
            if not self.minute.get((sid, session_date)):
                self.counts["missing_minute_bars"] += 1

    def bar_ending_at(self, symbol_id: int, at: datetime) -> Candle | None:
        for bar in self.minute.get((symbol_id, et_date(at - ONE_MINUTE)), []):
            if bar.end == at:
                return bar
        return None

    def last_close(self, symbol_id: int, at: datetime) -> Decimal | None:
        done = self._complete_minutes(symbol_id, et_date(at - ONE_MINUTE), at)
        return done[-1].close if done else None

    def progress_counts(self) -> Mapping[str, int]:
        return dict(self.counts)

    @property
    def biased_days(self) -> frozenset[date]:
        return frozenset(self.biased)


# --- the broker and the engine ------------------------------------------------------------------------------


class FakeReplayBroker:
    """A `ReplayBroker` over in-memory working orders and open positions. `submit` adds a working order (ids
    from 1), `cancel` removes one; both are recorded."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.orders: dict[int, OrderView] = {}
        self.positions: dict[int, PositionView] = {}
        self.submitted: list[tuple[int, OrderSpec]] = []
        self.cancelled: list[tuple[int, str]] = []
        self._next_id = 1

    def add_order(
        self,
        symbol_id: int,
        *,
        purpose: str = "entry",
        order_type: str = "stop",
        qty: int = 10,
        stop: Price | None = "10.00",
        position_id: int | None = None,
        submitted_at: datetime | None = None,
    ) -> int:
        order_id = self._next_id
        self._next_id += 1
        self.orders[order_id] = OrderView(
            id=order_id,
            symbol_id=symbol_id,
            side="buy" if purpose == "entry" else "sell",
            order_type=order_type,  # type: ignore[arg-type]
            purpose=purpose,  # type: ignore[arg-type]
            qty=qty,
            stop=_d(stop) if stop is not None else None,
            limit=None,
            status="working",
            position_id=position_id,
            strategy_config_id=None,
            proposal_id=None,
            submitted_at=submitted_at or self.clock.now(),
        )
        return order_id

    def remove_order(self, order_id: int) -> None:
        self.orders.pop(order_id, None)

    def add_position(
        self, symbol_id: int, *, qty: int = 10, avg_price: Price = "10.00", position_id: int | None = None
    ) -> int:
        pid = position_id if position_id is not None else len(self.positions) + 1
        now = self.clock.now()
        self.positions[pid] = PositionView(
            id=pid,
            symbol_id=symbol_id,
            strategy_config_id=None,
            qty=qty,
            avg_price=_d(avg_price),
            stop_loss=None,
            opened_at=now,
            session_date=et_date(now),
            stop_order_id=None,
            unprotected_since=None,
            unprotected_seconds=0,
        )
        return pid

    # --- ReplayBroker ------------------------------------------------------------------------------------
    def working_symbol_ids(self) -> list[int]:
        return sorted({o.symbol_id for o in self.orders.values()})

    def working_orders(self) -> list[OrderView]:
        return [self.orders[i] for i in sorted(self.orders)]

    def open_positions(self) -> list[PositionView]:
        return [self.positions[i] for i in sorted(self.positions)]

    def submit(self, spec: OrderSpec, session: Session | None = None) -> int:
        order_id = self.add_order(
            spec.symbol_id,
            purpose=spec.purpose,
            order_type=spec.order_type,
            qty=spec.qty,
            stop=spec.stop,
            position_id=spec.position_id,
        )
        self.submitted.append((order_id, spec))
        return order_id

    def cancel(self, order_id: int, reason: str, session: Session | None = None) -> bool:
        self.cancelled.append((order_id, reason))
        return self.orders.pop(order_id, None) is not None


@dataclass(frozen=True, slots=True)
class EngineCall:
    method: str  # run_event | on_candles | tick | end_of_session
    at: datetime  # the clock's time when the call was made
    args: tuple[Any, ...]


Hook = Callable[["FakeReplayEngine", EngineCall], Awaitable[None] | None]


class FakeReplayEngine:
    """A `ReplayEngine` that records every call (with the clock time) and then runs `hook(engine, call)`,
    which may change the broker, raise, or return fills through `next_fills`."""

    def __init__(
        self, clock: Clock, broker: FakeReplayBroker | None = None, *, hook: Hook | None = None
    ) -> None:
        self.clock = clock
        self._broker = broker if broker is not None else FakeReplayBroker(clock)
        self.hook = hook
        self.calls: list[EngineCall] = []
        self.next_fills: list[FillEvent] = []

    @property
    def broker(self) -> FakeReplayBroker:
        return self._broker

    async def _record(self, method: str, *args: Any) -> None:
        call = EngineCall(method, self.clock.now(), args)
        self.calls.append(call)
        if self.hook is not None:
            result = self.hook(self, call)
            if inspect.isawaitable(result):
                await result

    async def run_event(self, event_key: str, session_date: date) -> EventResult:
        await self._record("run_event", event_key, session_date)
        return EventResult(event_key, session_date)

    async def on_candles(self, candles: Mapping[int, Candle], now: datetime) -> list[FillEvent]:
        await self._record("on_candles", dict(sorted(candles.items())), now)
        fills, self.next_fills = self.next_fills, []
        return fills

    async def tick(self, now: datetime) -> None:
        await self._record("tick", now)

    async def end_of_session(self, session_date: date) -> list[PositionView]:
        await self._record("end_of_session", session_date)
        return self._broker.open_positions()


# --- the database world -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ReplayWorld:
    live_run_id: int
    symbols: dict[str, int]  # ticker -> symbols.id
    configs: dict[str, int] = field(default_factory=dict)  # strategy key -> its live strategy_configs.id


def seed_replay_world(
    factory: sessionmaker[Session],
    *,
    clock: Clock | None = None,
    tickers: Sequence[str] = ("SPY", "AAA", "BBB"),
    universe_days: Sequence[date] = (),
    universe_tickers: Sequence[str] | None = None,
    stats: Mapping[tuple[str, date], tuple[Price | None, Price | None]] | None = None,
    archive: Mapping[tuple[str, str], Sequence[Candle]] | None = None,
    daily: Mapping[str, Sequence[tuple[date, Candle]]] | None = None,
    catalysts: Mapping[tuple[str, date], str] | None = None,
    settings: RuntimeSettings | None = None,
    strategies: bool = True,
    plugins: Mapping[str, type[Any]] | None = None,
) -> ReplayWorld:
    """Seed what a replay reads, all committed:
    - `symbols` for `tickers` (Questrade ids 1000 + position), the live run with its account;
    - live strategy configs through `StrategyRegistry.ensure_defaults` (the installed plug-ins, or `plugins`);
    - a `universe_snapshots` row per day in `universe_days` for each of `universe_tickers` (default: all);
    - `open_bar_stats` rows from `stats[(ticker, day)] = (avg_open_vol_14d, atr14)`;
    - `candle_archive` rows from `archive[(ticker, "1m" | "5m")]`;
    - `daily_candles` rows from `daily[ticker] = [(date, candle), ...]`;
    - `catalysts` rows from `catalysts[(ticker, day)] = type` (direction `positive`, quality 4, classified).
    """
    clock = clock or FixedClock(WORLD_T0)
    now = clock.now()
    run = get_live_run(factory, clock, settings or RuntimeSettings())
    symbols: dict[str, int] = {}
    with session_scope(factory) as s:
        for i, ticker in enumerate(tickers):
            sym = m.Symbol(
                ticker=ticker, exchange="NASDAQ", questrade_id=1000 + i, currency="USD", name=f"{ticker} Inc"
            )
            s.add(sym)
            s.flush()
            symbols[ticker] = sym.id
        members = list(universe_tickers) if universe_tickers is not None else list(tickers)
        for day in universe_days:
            for ticker in members:
                s.add(
                    m.UniverseSnapshot(
                        session_date=day,
                        symbol_id=symbols[ticker],
                        price=Decimal("20"),
                        avg_volume=2_000_000,
                        atr14=Decimal("1.0"),
                        source="finviz",
                    )
                )
        for (ticker, day), (avg_vol, atr) in (stats or {}).items():
            s.add(
                m.OpenBarStat(
                    symbol_id=symbols[ticker],
                    session_date=day,
                    avg_open_vol_14d=_d(avg_vol) if avg_vol is not None else None,
                    atr14=_d(atr) if atr is not None else None,
                )
            )
        for (ticker, interval), bars in (archive or {}).items():
            for bar in bars:
                s.add(
                    m.CandleArchive(
                        symbol_id=symbols[ticker],
                        interval=interval,
                        start_ts=bar.start,
                        open=bar.open,
                        high=bar.high,
                        low=bar.low,
                        close=bar.close,
                        volume=bar.volume,
                        vwap=bar.vwap,
                    )
                )
        for ticker, rows in (daily or {}).items():
            for day, bar in rows:
                s.add(
                    m.DailyCandle(
                        symbol_id=symbols[ticker],
                        date=day,
                        open=bar.open,
                        high=bar.high,
                        low=bar.low,
                        close=bar.close,
                        volume=bar.volume,
                        vwap=bar.vwap,
                    )
                )
        for (ticker, day), kind in (catalysts or {}).items():
            s.add(
                m.Catalyst(
                    symbol_id=symbols[ticker],
                    session_date=day,
                    headlines=[],
                    catalyst_type=kind,
                    direction="positive",
                    quality=4,
                    confirmed=True,
                    reason="seeded",
                    model="claude-sonnet-5",
                    cost_usd=Decimal(0),
                    classified_at=now,
                    created_at=now,
                )
            )
    configs: dict[str, int] = {}
    if strategies:
        registry = StrategyRegistry(factory, clock, plugins)
        registry.ensure_defaults(actor="test")
        configs = {key: registry.current(key).id for key in registry.keys()}
    return ReplayWorld(live_run_id=run.id, symbols=symbols, configs=configs)


def shifted(bar: Candle, delta: timedelta) -> Candle:
    """The same bar `delta` later (handy for building several days from one template)."""
    return replace(bar, start=bar.start + delta, end=bar.end + delta)
