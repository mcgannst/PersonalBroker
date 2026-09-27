"""Idempotent writes of market data (every write is an upsert)."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from trader.adapters.questrade.models import QtSymbol
from trader.db.models import DailyCandle, IntradayCandle, OpenBarStat, Symbol, UniverseSnapshot
from trader.market.clock import et_date
from trader.market.types import Candle

_OHLCV = ("open", "high", "low", "close", "volume", "vwap")


@dataclass(frozen=True, slots=True)
class UniverseSnapshotRow:
    symbol_id: int
    price: Decimal | None
    avg_volume: int | None
    atr14: Decimal | None
    source: str


def upsert_symbols(session: Session, symbols: Iterable[QtSymbol]) -> dict[str, int]:
    """Questrade ticker -> symbols.id, keyed on the Questrade symbol id."""
    ids: dict[str, int] = {}
    for sym in symbols:
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
        for r in rows
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
        for sid, avg, a in stats
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
