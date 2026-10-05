"""Underlying facts (OPTSIM T8): the wheel rules spec §3.1 fields of one ticker on one day, each with its
source and fetch time (table `underlying_facts`, one row per symbol and ET date).

- source `questrade`, symbol details: `DETAIL_FIELDS`; and the quote's last trade: `price`.
- source `finviz`, the quote page's snapshot table: `FINVIZ_FIELDS` (and `market_cap_usd` when Questrade
  has none).
- source `candles`, completed daily bars of 260 sessions: `TREND_FIELDS`.

A fact stays None when its source has no value for it; the wheel's tests then answer CAUTION MISSING_DATA.
`sources` names every field of a source that answered, a None one too (the source was asked and had no
value). A source that fails leaves its fields as they were: None on the day's first refresh, the earlier
values (with their earlier fetch times) on a later one.

The owner's security-type override is not applied here: it lives in the wheel plug-in's own table and the
core never reads a plug-in's table (task plan §1 rule 10).

**ASSUMPTIONS** (task plan risk R11, checked live at T17):
- Questrade's `securityType` has no value for an exchange-traded fund, so a fund is also recognised by its
  ticker (`BROAD_INDEX_ETFS`) or a word of its description (`FUND_WORDS`). See `classify_security`.
- Questrade's `dividend` is stored as it arrives (taken to be the amount per payment, per share).
- Questrade's `marketCap` is in the symbol's currency; it is kept only for a USD symbol.
- `sector` is Questrade's `industrySector` text, unmapped.
"""

import asyncio
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from trader.adapters.finviz.fundamentals import to_fundamentals
from trader.adapters.questrade.option_types import OptionQuoteClient, QtSymbolDetails
from trader.db import models as m
from trader.db.session import session_scope
from trader.market.calendar import SessionCalendar
from trader.market.clock import Clock, et_date
from trader.market.repository import upsert_symbols
from trader.market.types import Candle
from trader.options.settings import OptionSettings
from trader.options.types import UnderlyingFacts

log = structlog.get_logger("options.facts")

STOCK = "STOCK"
BROAD_INDEX_ETF = "BROAD_INDEX_ETF"
SECTOR_ETF = "SECTOR_ETF"
LEVERAGED_OR_INVERSE_ETF = "LEVERAGED_OR_INVERSE_ETF"
BROAD_INDEX_ETFS = frozenset({"SPY", "IVV", "VOO", "VTI", "QQQ", "IWM", "DIA"})
# Whole words of a description that mark a fund when Questrade calls the security a "Stock".
FUND_WORDS = frozenset({"etf", "etn", "fund", "ishares", "spdr", "proshares", "direxion", "vanguard"})
LEVERAGED_WORDS = frozenset({"2x", "3x", "short", "bear"})  # whole words
LEVERAGED_PARTS = ("ultra", "leveraged", "inverse")  # anywhere in a word ("UltraPro", "UltraShort")

SRC_QUESTRADE = "questrade"
SRC_FINVIZ = "finviz"
SRC_CANDLES = "candles"
DETAIL_FIELDS = (
    "security_type",
    "sector",
    "eps_ttm",
    "market_cap_usd",
    "next_ex_dividend_date",
    "dividend_per_share",
    "dividend_yield",
    "has_options",
)
FINVIZ_FIELDS = (
    "eps_growth_yoy",
    "debt_to_equity",
    "book_value_per_share",
    "payout_ratio",
    "short_float",
    "next_earnings_date",
    "rsi14",
)
TREND_FIELDS = ("sma50", "sma50_prior", "low_52w", "sessions_since_52w_low")
FACT_FIELDS = (*DETAIL_FIELDS, "price", *FINVIZ_FIELDS, *TREND_FIELDS)
BAR_SESSIONS = 260
FOUR = Decimal("0.0001")

type Snapshots = Callable[[str, date], dict[str, str]]
type Bars = Callable[[str, date, date], Awaitable[list[Candle]]]


@dataclass(frozen=True, slots=True)
class TrendInputs:
    sma50: Decimal | None = None
    sma50_prior: Decimal | None = None  # the average `slope_lookback` sessions earlier
    low_52w: Decimal | None = None
    sessions_since_52w_low: int | None = None  # 0: the newest bar made the low


def trend_inputs(
    bars: Sequence[Candle],
    *,
    sma_period: int = 50,
    slope_lookback: int = 20,
    low_lookback_sessions: int = 252,
) -> TrendInputs:
    """The trend inputs from completed daily bars (any order; the newest bar is "now"). Averages are of
    closes, 4 decimals half up. The low is the lowest bar low of the last `low_lookback_sessions` bars;
    when two bars share it, the newer counts. Each value is None with fewer bars than it needs."""
    ordered = sorted(bars, key=lambda c: c.start)
    closes = [c.close for c in ordered]

    def average(window: list[Decimal]) -> Decimal | None:
        if len(window) < sma_period:
            return None
        return (sum(window, Decimal(0)) / sma_period).quantize(FOUR, ROUND_HALF_UP)

    sma = average(closes[-sma_period:])
    prior = average(closes[-(sma_period + slope_lookback) : -slope_lookback]) if slope_lookback > 0 else sma
    low: Decimal | None = None
    since: int | None = None
    if len(ordered) >= low_lookback_sessions:
        lows = [c.low for c in ordered[-low_lookback_sessions:]]
        low = min(lows)
        since = lows[::-1].index(low)
    return TrendInputs(sma, prior, low, since)


def classify_security(details: QtSymbolDetails) -> str:
    """The spec's `security_type`. A fund is anything Questrade does not call a "Stock", a ticker of
    `BROAD_INDEX_ETFS`, or a "Stock" whose description has a word of `FUND_WORDS`. A fund with a leveraged
    or inverse word in its description is LEVERAGED_OR_INVERSE_ETF; else a ticker of the broad list is
    BROAD_INDEX_ETF; any other fund is SECTOR_ETF. **ASSUMPTION** (no feed states this; the owner can
    override the result per ticker in the wheel panel)."""
    words = set(re.findall(r"[a-z0-9]+", details.description.lower()))
    symbol = details.symbol.upper()
    is_fund = (
        details.security_type.strip().lower() != "stock"
        or symbol in BROAD_INDEX_ETFS
        or bool(words & FUND_WORDS)
    )
    if not is_fund:
        return STOCK
    if words & LEVERAGED_WORDS or any(part in word for word in words for part in LEVERAGED_PARTS):
        return LEVERAGED_OR_INVERSE_ETF
    return BROAD_INDEX_ETF if symbol in BROAD_INDEX_ETFS else SECTOR_ETF


def _error(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:200]


def _to_value(row: m.UnderlyingFacts) -> UnderlyingFacts:
    sources = {
        name: (str(pair[0]), datetime.fromisoformat(pair[1])) for name, pair in (row.sources or {}).items()
    }
    return UnderlyingFacts(
        symbol_id=row.symbol_id,
        as_of=row.as_of,
        ticker=row.ticker,
        sources=sources,
        **{name: getattr(row, name) for name in FACT_FIELDS},
    )


class FactsService:
    """The `FactsProvider`. `snapshots` is `FinvizScraper.snapshot` (blocking and self-paced: it runs in a
    worker thread, one ticker at a time); `bars` is `OptionMarketView.daily_bars`; `settings` is the
    option settings or a callable that loads them. Database work runs in worker threads."""

    def __init__(
        self,
        factory: sessionmaker[Session],
        clock: Clock,
        calendar: SessionCalendar,
        client: OptionQuoteClient,
        snapshots: Snapshots,
        bars: Bars,
        settings: OptionSettings | Callable[[], OptionSettings],
    ) -> None:
        self._factory = factory
        self._clock = clock
        self._calendar = calendar
        self._client = client
        self._snapshots = snapshots
        self._bars = bars
        self._settings = settings

    # --- reading ---

    async def get(self, underlying: str) -> UnderlyingFacts | None:
        """The newest stored facts of the ticker, or None when there are none or they were fetched more
        than `options.facts_max_age_hours` ago."""
        return await asyncio.to_thread(self._get, underlying)

    def _get(self, ticker: str) -> UnderlyingFacts | None:
        settings = self._settings() if callable(self._settings) else self._settings
        oldest = self._clock.now() - timedelta(hours=settings.facts_max_age_hours)
        with self._factory() as session:
            row = session.execute(
                select(m.UnderlyingFacts)
                .where(m.UnderlyingFacts.ticker == ticker)
                .order_by(m.UnderlyingFacts.as_of.desc(), m.UnderlyingFacts.fetched_at.desc())
                .limit(1)
            ).scalar_one_or_none()
            if row is None or row.fetched_at < oldest:
                return None
            return _to_value(row)

    # --- refreshing ---

    async def refresh(self, underlyings: Sequence[str]) -> dict[str, UnderlyingFacts | str]:
        """Fetch and store today's facts of each ticker. A ticker maps to its stored facts, or to the
        error text when its symbol is unknown or every source failed (nothing is stored then)."""
        out: dict[str, UnderlyingFacts | str] = {}
        for ticker in dict.fromkeys(underlyings):
            try:
                out[ticker] = await self._refresh_one(ticker)
            except Exception as exc:
                log.warning("options.facts.refresh_failed", ticker=ticker, error=_error(exc))
                out[ticker] = _error(exc)
        return out

    async def _refresh_one(self, ticker: str) -> UnderlyingFacts | str:
        ids = await self._symbol(ticker)
        if ids is None:
            return f"unknown symbol {ticker}"
        symbol_id, questrade_id = ids
        now = self._clock.now()
        today = et_date(now)
        values: dict[str, Any] = {}
        sources: dict[str, list[str]] = {}
        errors: dict[str, str] = {}

        def took(source: str, fields: dict[str, Any]) -> None:
            values.update(fields)
            sources.update(dict.fromkeys(fields, [source, self._clock.now().isoformat()]))

        try:
            details = (await self._client.symbol_details([questrade_id])).get(questrade_id)
            if details is None:
                raise LookupError("Questrade returned no details")
            took(SRC_QUESTRADE, _detail_fields(details, today))
        except Exception as exc:
            errors["questrade details"] = _error(exc)
        try:
            quotes = [q for q in await self._client.quotes([questrade_id]) if q.symbol_id == questrade_id]
            if not quotes:
                raise LookupError("Questrade returned no quote")
            took(SRC_QUESTRADE, {"price": quotes[0].last})
        except Exception as exc:
            errors["questrade quote"] = _error(exc)
        try:
            raw = await asyncio.to_thread(self._snapshots, ticker, today)
            finviz = to_fundamentals(raw, today)
            took(SRC_FINVIZ, {name: getattr(finviz, name) for name in FINVIZ_FIELDS})
            if values.get("market_cap_usd") is None and finviz.market_cap_usd is not None:
                took(SRC_FINVIZ, {"market_cap_usd": finviz.market_cap_usd})
        except Exception as exc:
            errors[SRC_FINVIZ] = _error(exc)
        try:
            start, end = self._bar_window(now, today)
            bars = [c for c in await self._bars(ticker, start, end) if et_date(c.start) <= end]
            if not bars:
                raise LookupError("no daily bars")
            trend = trend_inputs(bars)
            took(SRC_CANDLES, {name: getattr(trend, name) for name in TREND_FIELDS})
        except Exception as exc:
            errors[SRC_CANDLES] = _error(exc)

        if errors:
            log.warning("options.facts.source_failed", ticker=ticker, errors=errors)
        if not values:
            return "; ".join(f"{source}: {text}" for source, text in errors.items())
        return await asyncio.to_thread(self._store, symbol_id, ticker, today, values, sources)

    def _bar_window(self, now: datetime, today: date) -> tuple[date, date]:
        """The 260 sessions that end with the last completed one (today only once it has closed)."""
        before = self._calendar.sessions_before(today, BAR_SESSIONS)
        closed = self._calendar.is_session(today) and now >= self._calendar.session_close(today)
        return (before[1], today) if closed else (before[0], before[-1])

    async def _symbol(self, ticker: str) -> tuple[int, int] | None:
        """(symbols.id, Questrade id) of the ticker; a ticker the database lacks is asked of Questrade and
        stored. None: neither knows it."""
        known = await asyncio.to_thread(self._known_symbol, ticker)
        if known is not None:
            return known
        found = (await self._client.symbols_by_names([ticker])).get(ticker)
        if found is None:
            return None

        def store() -> int:
            with session_scope(self._factory) as session:
                return upsert_symbols(session, [found], self._clock)[found.symbol]

        return await asyncio.to_thread(store), found.symbol_id

    def _known_symbol(self, ticker: str) -> tuple[int, int] | None:
        with self._factory() as session:
            row = session.execute(
                select(m.Symbol.id, m.Symbol.questrade_id)
                .where(
                    m.Symbol.ticker == ticker,
                    m.Symbol.questrade_id.is_not(None),
                    m.Symbol.exchange.not_like("STALE-%"),
                )
                .order_by(m.Symbol.id.desc())
                .limit(1)
            ).one_or_none()
        return None if row is None else (int(row[0]), int(row[1]))

    def _store(
        self, symbol_id: int, ticker: str, today: date, values: dict[str, Any], sources: dict[str, list[str]]
    ) -> UnderlyingFacts:
        """Upsert the day's row: only the fields in `values` change, and `sources` is merged by field."""
        table = m.UnderlyingFacts
        stmt = insert(table).values(
            symbol_id=symbol_id,
            as_of=today,
            ticker=ticker,
            sources=sources,
            fetched_at=self._clock.now(),
            **values,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[table.symbol_id, table.as_of],
            set_={
                "ticker": stmt.excluded.ticker,
                "fetched_at": stmt.excluded.fetched_at,
                "sources": table.sources.op("||")(stmt.excluded.sources),
                **{name: stmt.excluded[name] for name in values},
            },
        )
        with session_scope(self._factory) as session:
            session.execute(stmt)
            return _to_value(session.get_one(table, (symbol_id, today)))


def _detail_fields(details: QtSymbolDetails, today: date) -> dict[str, Any]:
    ex_date = details.ex_date
    return {
        "security_type": classify_security(details),
        "sector": (details.industry_sector or "").strip()[:60] or None,
        "eps_ttm": details.eps,
        "market_cap_usd": details.market_cap if details.currency == "USD" else None,
        "next_ex_dividend_date": ex_date if ex_date is not None and ex_date >= today else None,
        "dividend_per_share": details.dividend,
        "dividend_yield": None if details.yield_pct is None else details.yield_pct.scaleb(-2),
        "has_options": details.has_options,
    }
