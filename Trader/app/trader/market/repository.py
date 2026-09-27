"""Idempotent writes of market data (every write is an upsert)."""

from collections.abc import Collection, Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

import structlog
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from trader.adapters.questrade.models import QtSymbol
from trader.db.models import DailyCandle, IntradayCandle, OpenBarStat, Symbol, UniverseSnapshot
from trader.events import log_event
from trader.market.clock import Clock, et_date
from trader.market.types import Candle

log = structlog.get_logger("market.repository")

_OHLCV = ("open", "high", "low", "close", "volume", "vwap")
_EXCHANGE_LEN = 20  # symbols.exchange is String(20)


@dataclass(frozen=True, slots=True)
class UniverseSnapshotRow:
    symbol_id: int
    price: Decimal | None
    avg_volume: int | None
    atr14: Decimal | None
    source: str


def _stale_exchange(old: Symbol) -> str:
    tag = str(old.questrade_id) if old.questrade_id is not None else f"id{old.id}"
    return f"STALE-{tag}"[:_EXCHANGE_LEN]


def upsert_symbols(
    session: Session, symbols: Iterable[QtSymbol], clock: Clock | None = None
) -> dict[str, int]:
    """Questrade ticker -> symbols.id, keyed on the Questrade symbol id.

    Tickers get reused (FB became META, taking a ticker another security had held). If (ticker,
    exchange) is held by a row with a DIFFERENT Questrade id, that old row is first moved out of the
    way (its exchange becomes "STALE-<old questrade id>"), keeping its history and foreign keys, and
    the move is logged (to event_log when `clock` is given)."""
    ids: dict[str, int] = {}
    for sym in symbols:
        holder = session.execute(
            select(Symbol).where(
                Symbol.ticker == sym.symbol,
                Symbol.exchange == sym.listing_exchange,
                Symbol.questrade_id.is_distinct_from(sym.symbol_id),
            )
        ).scalar_one_or_none()
        if holder is not None:
            stale = _stale_exchange(holder)
            data: dict[str, Any] = {
                "ticker": sym.symbol,
                "exchange": sym.listing_exchange,
                "old_questrade_id": holder.questrade_id,
                "new_questrade_id": sym.symbol_id,
                "old_symbol_id": holder.id,
                "moved_to_exchange": stale,
            }
            holder.exchange = stale
            session.flush()
            message = f"ticker {sym.symbol} reused; old symbol row marked stale"
            if clock is not None:
                log_event(session, clock, "warning", "market.symbols", message, data)
            else:
                log.warning("symbols.ticker_reused", **data)
        stmt = (
            insert(Symbol)
            .values(
                ticker=sym.symbol,
                exchange=sym.listing_exchange,
                questrade_id=sym.symbol_id,
                currency=sym.currency,
                name=sym.description,
            )
            .on_conflict_do_update(
                index_elements=[Symbol.questrade_id],
                set_={
                    "ticker": sym.symbol,
                    "exchange": sym.listing_exchange,
                    "currency": sym.currency,
                    "name": sym.description,
                },
            )
            .returning(Symbol.id)
        )
        ids[sym.symbol] = int(session.execute(stmt).scalar_one())
    return ids


def _ohlcv(c: Candle) -> dict[str, object]:
    return {
        "open": c.open,
        "high": c.high,
        "low": c.low,
        "close": c.close,
        "volume": c.volume,
        "vwap": c.vwap,
    }


def upsert_daily_candles(session: Session, symbol_id: int, candles: Iterable[Candle]) -> int:
    # A daily bar starts at 00:00 ET, so its trading date is the ET date of its start.
    rows = [{"symbol_id": symbol_id, "date": et_date(c.start), **_ohlcv(c)} for c in candles]
    if not rows:
        return 0
    stmt = insert(DailyCandle).values(rows)
    session.execute(
        stmt.on_conflict_do_update(
            index_elements=[DailyCandle.symbol_id, DailyCandle.date],
            set_={k: stmt.excluded[k] for k in _OHLCV},
        )
    )
    return len(rows)


def upsert_intraday_candles(
    session: Session, symbol_id: int, interval_code: str, candles: Iterable[Candle]
) -> int:
    rows = [{"symbol_id": symbol_id, "interval": interval_code, "ts": c.start, **_ohlcv(c)} for c in candles]
    if not rows:
        return 0
    stmt = insert(IntradayCandle).values(rows)
    session.execute(
        stmt.on_conflict_do_update(
            index_elements=[IntradayCandle.symbol_id, IntradayCandle.interval, IntradayCandle.ts],
            set_={k: stmt.excluded[k] for k in _OHLCV},
        )
    )
    return len(rows)


def save_universe_snapshot(session: Session, session_date: date, rows: Iterable[UniverseSnapshotRow]) -> int:
    values = [
        {
            "session_date": session_date,
            "symbol_id": r.symbol_id,
            "price": r.price,
            "avg_volume": r.avg_volume,
            "atr14": r.atr14,
            "source": r.source,
        }
        for r in {r.symbol_id: r for r in rows}.values()  # ON CONFLICT can't touch one row twice
    ]
    if not values:
        return 0
    stmt = insert(UniverseSnapshot).values(values)
    session.execute(
        stmt.on_conflict_do_update(
            index_elements=[UniverseSnapshot.session_date, UniverseSnapshot.symbol_id],
            set_={k: stmt.excluded[k] for k in ("price", "avg_volume", "atr14", "source")},
        )
    )
    return len(values)


def save_open_bar_stats(
    session: Session, session_date: date, stats: Iterable[tuple[int, Decimal | None, Decimal | None]]
) -> int:
    values = [
        {"symbol_id": sid, "session_date": session_date, "avg_open_vol_14d": avg, "atr14": a}
        for sid, (avg, a) in {sid: (avg, a) for sid, avg, a in stats}.items()  # one row per symbol
    ]
    if not values:
        return 0
    stmt = insert(OpenBarStat).values(values)
    session.execute(
        stmt.on_conflict_do_update(
            index_elements=[OpenBarStat.symbol_id, OpenBarStat.session_date],
            set_={"avg_open_vol_14d": stmt.excluded.avg_open_vol_14d, "atr14": stmt.excluded.atr14},
        )
    )
    return len(values)


def latest_universe_tickers(session: Session, before: date) -> tuple[date, list[str]] | None:
    """The most recent stored universe strictly before `before`: (its session date, tickers)."""
    last = session.execute(
        select(func.max(UniverseSnapshot.session_date)).where(UniverseSnapshot.session_date < before)
    ).scalar_one_or_none()
    if last is None:
        return None
    tickers = (
        session.execute(
            select(Symbol.ticker)
            .join(UniverseSnapshot, UniverseSnapshot.symbol_id == Symbol.id)
            .where(UniverseSnapshot.session_date == last)
            .order_by(Symbol.ticker)
        )
        .scalars()
        .all()
    )
    return last, list(tickers)


def has_finviz_universe(session: Session, session_date: date) -> bool:
    """True when `session_date` already has a universe snapshot that came from FinViz."""
    row = session.execute(
        select(UniverseSnapshot.symbol_id)
        .where(UniverseSnapshot.session_date == session_date, UniverseSnapshot.source == "finviz")
        .limit(1)
    ).scalar_one_or_none()
    return row is not None


def delete_session_rows_except(session: Session, session_date: date, keep: Collection[int]) -> int:
    """Remove universe_snapshots and open_bar_stats rows of `session_date` whose symbol isn't in
    `keep`, so a re-run replaces the day instead of leaving the previous run's extra symbols."""
    ids = list(keep)
    stmts = (
        delete(UniverseSnapshot).where(
            UniverseSnapshot.session_date == session_date, UniverseSnapshot.symbol_id.not_in(ids)
        ),
        delete(OpenBarStat).where(
            OpenBarStat.session_date == session_date, OpenBarStat.symbol_id.not_in(ids)
        ),
    )
    return sum(int(getattr(session.execute(stmt), "rowcount", 0) or 0) for stmt in stmts)


def universe_origin(session: Session, snapshot_date: date) -> date:
    """The FinViz date a stored universe really comes from. A fallback snapshot copies an older
    universe, so its origin is the latest FinViz snapshot before it (itself if it isn't a fallback,
    or if no FinViz snapshot exists)."""
    is_fallback = session.execute(
        select(UniverseSnapshot.symbol_id)
        .where(UniverseSnapshot.session_date == snapshot_date, UniverseSnapshot.source == "fallback")
        .limit(1)
    ).scalar_one_or_none()
    if is_fallback is None:
        return snapshot_date
    origin = session.execute(
        select(func.max(UniverseSnapshot.session_date)).where(
            UniverseSnapshot.session_date < snapshot_date, UniverseSnapshot.source == "finviz"
        )
    ).scalar_one_or_none()
    return origin or snapshot_date
