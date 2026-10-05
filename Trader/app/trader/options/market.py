"""Option market data behind `OptionMarketView` (OPTSIM task plan T3): the contract master, the chain cache,
live option quotes, the stored marks and the calendar maths.

- Ids: a contract id is `option_contracts.id`, an underlying is a ticker. Questrade ids stay inside this
  module (`symbols.questrade_id`, `option_contracts.qt_symbol_id`).
- What hits Questrade: `quotes`, `quotes_for_expiry`, `underlying_quote` and `record_marks` on every call; the
  chain only when its cache is older than `options.chain_cache_hours` (or `force`); `daily_bars` only for
  sessions missing from `daily_candles`; `resolve_underlying` only the first time a ticker is seen.
- Nothing here judges how old a quote is: every quote and mark carries `fetched_at` and the fill model (or
  whoever reads a mark) decides.
"""

import asyncio
import dataclasses
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from trader.adapters.questrade.models import QtQuote, QtSymbol
from trader.adapters.questrade.option_types import OptionQuoteClient, QtChainExpiry, QtOptionQuote
from trader.db import models as m
from trader.db.session import session_scope
from trader.market import repository as repo
from trader.market.calendar import SessionCalendar
from trader.market.clock import ET, Clock, et_date
from trader.market.types import Candle
from trader.options.protocols import FactsProvider, UnknownContract, UnknownUnderlying
from trader.options.settings import OptionSettings
from trader.options.types import (
    HUNDRED,
    SHARES_PER_CONTRACT,
    ChainStrike,
    ContractKey,
    ExpiryInfo,
    OptionContract,
    OptionQuote,
    Right,
    UnderlyingFacts,
    dte,
)

log = structlog.get_logger("options.market")

ONE_DAY = timedelta(days=1)
STALE_EXCHANGE = "STALE-%"  # a symbols row moved aside by `repository.upsert_symbols` (a reused ticker)
INSERT_CHUNK = 1000  # contracts per INSERT (11 bind parameters each)
# A session Questrade had no daily bar for is asked for again only after this long (a suspended stock, or
# today's bar just after the close), so `daily_bars` does not fetch on every call.
GAP_RETRY = timedelta(minutes=15)
_CONTRACT_FIELDS = ("underlying", "qt_symbol_id", "root", "expiry", "strike", "right", "multiplier")


def monthly_expiry(year: int, month: int, calendar: SessionCalendar) -> date:
    """The standard monthly expiry: the third Friday of the month, or the Thursday before when that Friday
    is not a session (Good Friday, Juneteenth). Outside the calendar's range the Friday is assumed."""
    first = date(year, month, 1)
    friday = first + timedelta(days=(4 - first.weekday()) % 7 + 14)
    try:
        open_day = calendar.is_session(friday)
    except ValueError:  # a far LEAPS beyond the calendar
        return friday
    return friday if open_day else friday - ONE_DAY


@dataclass(frozen=True, slots=True)
class _Underlying:
    ticker: str
    symbol_id: int  # symbols.id
    qid: int  # symbols.questrade_id


def _contract(row: m.OptionContract) -> OptionContract:
    return OptionContract(
        id=row.id,
        underlying=row.underlying,
        underlying_symbol_id=row.underlying_symbol_id,
        qt_symbol_id=row.qt_symbol_id,
        root=row.root,
        expiry=row.expiry,
        strike=row.strike,
        right="call" if row.right == "call" else "put",
        multiplier=row.multiplier,
        is_monthly=row.is_monthly,
        adjusted=row.adjusted,
    )


def _quote(contract_id: int, q: QtOptionQuote) -> OptionQuote:
    """Questrade sends the implied volatility as a percentage; everything here holds a decimal fraction."""
    return OptionQuote(
        contract_id=contract_id,
        bid=q.bid,
        ask=q.ask,
        last=q.last,
        bid_size=q.bid_size,
        ask_size=q.ask_size,
        volume=q.volume,
        open_interest=q.open_interest,
        iv=None if q.iv_pct is None else q.iv_pct / HUNDRED,
        delta=q.delta,
        gamma=q.gamma,
        theta=q.theta,
        vega=q.vega,
        last_trade_time=q.last_trade_time,
        delay=q.delay,
        is_halted=q.is_halted,
        fetched_at=q.fetched_at,
    )


def _chain_json(chain: Sequence[QtChainExpiry]) -> list[dict[str, Any]]:
    """The `option_chain_cache.chain` shape: one entry per expiry and root, with Questrade ids."""
    return [
        {
            "expiry": expiry.expiry.isoformat(),
            "root": root.root,
            "multiplier": root.multiplier,
            "strikes": [
                {"strike": str(k.strike), "call_id": k.call_id, "put_id": k.put_id} for k in root.strikes
            ],
        }
        for expiry in chain
        for root in expiry.roots
    ]


class OptionMarketService:
    """`OptionMarketView` over the database and Questrade. One instance per process: it remembers the
    underlyings it has resolved and the contracts it has read."""

    def __init__(
        self,
        factory: sessionmaker[Session],
        clock: Clock,
        calendar: SessionCalendar,
        client: OptionQuoteClient,
        settings: Callable[[], OptionSettings],
        facts: FactsProvider | None = None,
    ) -> None:
        self._factory = factory
        self._clock = clock
        self._cal = calendar
        self._client = client
        self._settings = settings
        self._facts = facts
        self._known: dict[str, _Underlying] = {}
        self._contracts: dict[int, OptionContract] = {}
        self._chain_locks: dict[int, asyncio.Lock] = {}
        self._bars_tried: dict[int, dict[date, datetime]] = {}

    async def _db[T](self, step: Callable[..., T], *args: Any) -> T:
        """Runs one synchronous database step. Inline here; a subclass may run it in a worker thread (as
        `trader.api.services.OffLoopMarketData` does for the stock market data)."""
        return step(*args)

    # --- underlyings --------------------------------------------------------------------------------------
    async def resolve_underlying(self, ticker: str) -> int:
        return (await self._resolve(ticker)).symbol_id

    async def _resolve(self, ticker: str) -> _Underlying:
        ticker = ticker.strip().upper()
        known = self._known.get(ticker)
        if known is not None:
            return known
        if not ticker:
            raise UnknownUnderlying(ticker)
        found = await self._db(self._find_symbol, ticker)
        new: QtSymbol | None = None
        if found is None:
            new = (await self._client.symbols_by_names([ticker])).get(ticker)
            if new is None:
                raise UnknownUnderlying(ticker)
            qid: int | None = new.symbol_id
            verified = False
        else:
            symbol_id, qid, verified = found
        if qid is None:
            raise UnknownUnderlying(f"{ticker}: no Questrade id")
        if not verified:  # a stored chain already proves it has options
            details = (await self._client.symbol_details([qid])).get(qid)
            if details is None or not details.has_options:
                raise UnknownUnderlying(f"{ticker}: no options")
        if new is not None:
            symbol_id = await self._db(self._store_symbol, new)
        underlying = _Underlying(ticker, symbol_id, qid)
        self._known[ticker] = underlying
        return underlying

    def _find_symbol(self, ticker: str) -> tuple[int, int | None, bool] | None:
        """(symbols.id, questrade id, has a stored chain) of the ticker's row, preferring one with a
        Questrade id; None when there is no row."""
        with self._factory() as s:
            row = s.execute(
                select(m.Symbol.id, m.Symbol.questrade_id)
                .where(m.Symbol.ticker == ticker, m.Symbol.exchange.not_like(STALE_EXCHANGE))
                .order_by(m.Symbol.questrade_id.is_(None), m.Symbol.id)
                .limit(1)
            ).first()
            if row is None:
                return None
            chain = s.execute(
                select(m.OptionChainCache.underlying_symbol_id).where(
                    m.OptionChainCache.underlying_symbol_id == row[0]
                )
            ).first()
        return int(row[0]), (int(row[1]) if row[1] is not None else None), chain is not None

    def _store_symbol(self, symbol: QtSymbol) -> int:
        with session_scope(self._factory) as s:
            return repo.upsert_symbols(s, [symbol], self._clock)[symbol.symbol]

    # --- the chain ----------------------------------------------------------------------------------------
    async def refresh_chain(self, underlying: str, *, force: bool = False) -> int:
        """Makes the underlying's chain current: fetched and stored when the cache is older than
        `options.chain_cache_hours` (always with `force`). Returns the contracts upserted (0: the cache was
        fresh)."""
        return (await self._chain(await self._resolve(underlying), force=force))[1]

    async def _chain(self, u: _Underlying, *, force: bool = False) -> tuple[list[dict[str, Any]], int]:
        """(the chain as cached, contracts upserted by this call). A failed fetch falls back to the old
        cache when there is one, unless `force`."""
        async with self._chain_locks.setdefault(u.symbol_id, asyncio.Lock()):
            cached = await self._db(self._cached_chain, u.symbol_id)
            now = self._clock.now()
            ttl = timedelta(hours=self._settings().chain_cache_hours)
            if cached is not None and not force and now - cached[0] < ttl:
                return cached[1], 0
            try:
                fetched = await self._client.option_chain(u.qid)
            except Exception as exc:
                if cached is None or force:
                    raise
                log.warning("options.chain_fetch_failed", underlying=u.ticker, error=type(exc).__name__)
                return cached[1], 0
            chain = _chain_json(fetched)
            if not any(entry["strikes"] for entry in chain):  # never replace a chain with an empty answer
                log.warning("options.chain_empty", underlying=u.ticker)
                return (cached[1] if cached is not None else []), 0
            count = await self._db(self._store_chain, u, fetched, chain, now)
            for cid in [cid for cid, c in self._contracts.items() if c.underlying_symbol_id == u.symbol_id]:
                del self._contracts[cid]
            return chain, count

    def _cached_chain(self, symbol_id: int) -> tuple[datetime, list[dict[str, Any]]] | None:
        with self._factory() as s:
            row = s.get(m.OptionChainCache, symbol_id)
            return None if row is None else (row.fetched_at, list(row.chain))

    def _store_chain(
        self, u: _Underlying, fetched: Sequence[QtChainExpiry], chain: list[dict[str, Any]], now: datetime
    ) -> int:
        """Writes the cache row and upserts every contract of the chain into the master (an existing
        contract keeps its id and `first_seen_at`)."""
        wanted: dict[int, dict[str, Any]] = {}
        for expiry in fetched:
            monthly = self.is_monthly(expiry.expiry)
            for root in expiry.roots:
                adjusted = root.multiplier != SHARES_PER_CONTRACT or root.root != u.ticker.replace(".", "")
                for k in root.strikes:
                    for right, qt_id in (("call", k.call_id), ("put", k.put_id)):
                        if not qt_id:
                            continue
                        wanted[qt_id] = {
                            "underlying_symbol_id": u.symbol_id,
                            "underlying": u.ticker,
                            "qt_symbol_id": qt_id,
                            "root": root.root,
                            "expiry": expiry.expiry,
                            "strike": k.strike,
                            "right": right,
                            "multiplier": root.multiplier,
                            "is_monthly": monthly,
                            "adjusted": adjusted,
                            "first_seen_at": now,
                        }
        with session_scope(self._factory) as s:
            cache = pg_insert(m.OptionChainCache).values(
                underlying_symbol_id=u.symbol_id, fetched_at=now, chain=chain, expiries=len(fetched)
            )
            s.execute(
                cache.on_conflict_do_update(
                    index_elements=[m.OptionChainCache.underlying_symbol_id],
                    set_={k: cache.excluded[k] for k in ("fetched_at", "chain", "expiries")},
                )
            )
            rows = (
                s.execute(
                    select(m.OptionContract).where(m.OptionContract.underlying_symbol_id == u.symbol_id)
                )
                .scalars()
                .all()
            )
            by_qt = {r.qt_symbol_id: r for r in rows}
            by_key = {(r.expiry, r.strike, r.right, r.root): r for r in rows}
            new: list[dict[str, Any]] = []
            for qt_id, values in wanted.items():
                key = (values["expiry"], values["strike"], values["right"], values["root"])
                row = by_qt.get(qt_id) or by_key.get(key)
                if row is None:
                    new.append(values)
                    continue
                for name in (*_CONTRACT_FIELDS, "is_monthly", "adjusted"):
                    if getattr(row, name) != values[name]:
                        setattr(row, name, values[name])
            s.flush()
            for i in range(0, len(new), INSERT_CHUNK):  # do nothing: another process stored it first
                s.execute(
                    pg_insert(m.OptionContract).values(new[i : i + INSERT_CHUNK]).on_conflict_do_nothing()
                )
        return len(wanted)

    async def expiries(self, underlying: str) -> list[ExpiryInfo]:
        """The unexpired expiries of the chain, nearest first. `strikes` counts distinct strikes."""
        chain, _ = await self._chain(await self._resolve(underlying))
        today = et_date(self._clock.now())
        strikes: dict[date, set[Decimal]] = {}
        for entry in chain:
            expiry = date.fromisoformat(entry["expiry"])
            if expiry >= today:
                strikes.setdefault(expiry, set()).update(Decimal(k["strike"]) for k in entry["strikes"])
        return [
            ExpiryInfo(expiry, dte(expiry, today), self.is_monthly(expiry), len(found))
            for expiry, found in sorted(strikes.items())
        ]

    async def strikes(self, underlying: str, expiry: date) -> list[ChainStrike]:
        """One row per strike of the expiry (empty for an expiry the chain does not have). Where a strike
        has a standard and an adjusted contract, the standard one is listed."""
        u = await self._resolve(underlying)
        await self._chain(u)
        return await self._db(self._strike_rows, u.symbol_id, expiry)

    def _strike_rows(self, symbol_id: int, expiry: date) -> list[ChainStrike]:
        with self._factory() as s:
            rows = s.execute(
                select(m.OptionContract.strike, m.OptionContract.right, m.OptionContract.id)
                .where(m.OptionContract.underlying_symbol_id == symbol_id, m.OptionContract.expiry == expiry)
                .order_by(m.OptionContract.strike, m.OptionContract.adjusted, m.OptionContract.id)
            ).all()
        ids: dict[Decimal, dict[str, int]] = {}
        for strike, right, cid in rows:
            ids.setdefault(strike, {}).setdefault(right, int(cid))
        return [ChainStrike(strike, v.get("call"), v.get("put")) for strike, v in ids.items()]

    # --- contracts ----------------------------------------------------------------------------------------
    async def contract(self, contract_id: int) -> OptionContract:
        found = (await self._contracts_by_id([contract_id])).get(contract_id)
        if found is None:
            raise UnknownContract(contract_id)
        return found

    async def _contracts_by_id(self, contract_ids: Sequence[int]) -> dict[int, OptionContract]:
        """The known contracts among the ids (an unknown id is left out)."""
        need = [cid for cid in dict.fromkeys(contract_ids) if cid not in self._contracts]
        if need:
            for c in await self._db(self._read_contracts, m.OptionContract.id.in_(need)):
                self._contracts[c.id] = c
        return {cid: self._contracts[cid] for cid in contract_ids if cid in self._contracts}

    def _read_contracts(self, *where: Any) -> list[OptionContract]:
        with self._factory() as s:
            rows = s.execute(
                select(m.OptionContract)
                .where(*where)
                .order_by(m.OptionContract.strike, m.OptionContract.adjusted, m.OptionContract.id)
            ).scalars()
            return [_contract(r) for r in rows]

    async def find_contract(self, key: ContractKey) -> OptionContract | None:
        """The contract a strategy named, or None (also for an unknown underlying). The standard contract
        wins over an adjusted one at the same strike."""
        try:
            u = await self._resolve(key.underlying)
        except UnknownUnderlying:
            return None
        await self._chain(u)
        found = await self._db(
            self._read_contracts,
            m.OptionContract.underlying_symbol_id == u.symbol_id,
            m.OptionContract.expiry == key.expiry,
            m.OptionContract.strike == key.strike,
            m.OptionContract.right == key.right,
        )
        return found[0] if found else None

    # --- quotes -------------------------------------------------------------------------------------------
    async def quotes(self, contract_ids: Sequence[int]) -> dict[int, OptionQuote]:
        """Live quotes by contract id. A contract Questrade sent no quote for, or an id that is not in the
        master, is left out."""
        contracts = await self._contracts_by_id(contract_ids)
        if not contracts:
            return {}
        back = {c.qt_symbol_id: cid for cid, c in contracts.items()}
        got = await self._client.option_quotes(list(back))
        return {back[q.symbol_id]: _quote(back[q.symbol_id], q) for q in got if q.symbol_id in back}

    async def quotes_for_expiry(
        self,
        underlying: str,
        expiry: date,
        right: Right,
        min_strike: Decimal | None = None,
        max_strike: Decimal | None = None,
    ) -> list[tuple[OptionContract, OptionQuote]]:
        """Live quotes of one expiry and right, lowest strike first (Questrade's filter call). Adjusted
        contracts are included: check `OptionContract.adjusted`."""
        u = await self._resolve(underlying)
        await self._chain(u)
        got = await self._client.option_quotes_filter(u.qid, expiry, right, min_strike, max_strike)
        if not got:
            return []
        found = await self._db(
            self._read_contracts, m.OptionContract.qt_symbol_id.in_([q.symbol_id for q in got])
        )
        by_qt = {c.qt_symbol_id: c for c in found}
        pairs = [(by_qt[q.symbol_id], _quote(by_qt[q.symbol_id].id, q)) for q in got if q.symbol_id in by_qt]
        return sorted(pairs, key=lambda pair: (pair[0].strike, pair[0].adjusted, pair[0].id))

    async def underlying_quote(self, underlying: str) -> QtQuote | None:
        """The live share quote; its `symbol_id` is `symbols.id`."""
        u = await self._resolve(underlying)
        for q in await self._client.quotes([u.qid]):
            if q.symbol_id == u.qid:
                return dataclasses.replace(q, symbol_id=u.symbol_id)
        return None

    async def facts(self, underlying: str) -> UnderlyingFacts | None:
        return None if self._facts is None else await self._facts.get(underlying)

    # --- marks --------------------------------------------------------------------------------------------
    async def record_marks(self, contract_ids: Sequence[int]) -> int:
        """Fetches a live quote for each contract and stores it as the contract's mark, with the
        underlying's last price. Returns the marks written. A stored mark is never replaced by an older
        quote."""
        quotes = await self.quotes(contract_ids)
        if not quotes:
            return 0
        contracts = await self._contracts_by_id(list(quotes))
        symbol_ids = sorted({c.underlying_symbol_id for c in contracts.values()})
        qids = await self._db(self._questrade_ids, symbol_ids)
        last_by_qid = {q.symbol_id: q.last for q in await self._client.quotes(list(qids.values()))}
        rows = [
            {
                "contract_id": cid,
                "underlying_price": last_by_qid.get(qids.get(contracts[cid].underlying_symbol_id, 0)),
                **{k: v for k, v in dataclasses.asdict(q).items() if k != "contract_id"},
            }
            for cid, q in quotes.items()
        ]
        await self._db(self._store_marks, rows)
        return len(rows)

    def _questrade_ids(self, symbol_ids: Sequence[int]) -> dict[int, int]:
        with self._factory() as s:
            rows = s.execute(
                select(m.Symbol.id, m.Symbol.questrade_id).where(
                    m.Symbol.id.in_(list(symbol_ids)), m.Symbol.questrade_id.is_not(None)
                )
            ).all()
        return {int(sid): int(qid) for sid, qid in rows}

    def _store_marks(self, rows: list[dict[str, Any]]) -> None:
        stmt = pg_insert(m.OptionQuoteMark).values(rows)
        with session_scope(self._factory) as s:
            s.execute(
                stmt.on_conflict_do_update(
                    index_elements=[m.OptionQuoteMark.contract_id],
                    set_={k: stmt.excluded[k] for k in rows[0] if k != "contract_id"},
                    where=m.OptionQuoteMark.fetched_at <= stmt.excluded.fetched_at,
                )
            )

    async def marks(self, contract_ids: Sequence[int]) -> dict[int, OptionQuote]:
        """The stored marks (no Questrade call). `fetched_at` is when the mark's quote was read: the caller
        judges whether it is too old to use."""
        return await self._db(self._read_marks, list(contract_ids))

    def _read_marks(self, contract_ids: list[int]) -> dict[int, OptionQuote]:
        if not contract_ids:
            return {}
        names = [f.name for f in dataclasses.fields(OptionQuote)]
        with self._factory() as s:
            rows = s.execute(
                select(m.OptionQuoteMark).where(m.OptionQuoteMark.contract_id.in_(contract_ids))
            ).scalars()
            return {r.contract_id: OptionQuote(**{n: getattr(r, n) for n in names}) for r in rows}

    # --- daily bars ---------------------------------------------------------------------------------------
    async def daily_bars(self, underlying: str, start: date, end: date) -> list[Candle]:
        """The daily bars of the sessions in [start, end], oldest first, from `daily_candles`. Complete
        sessions it lacks are fetched and stored first (today's counts once the session has closed)."""
        u = await self._resolve(underlying)
        now = self._clock.now()
        due = self._complete_sessions(start, end, now)
        have = await self._db(self._stored_days, u.symbol_id, start, end)
        tried = self._bars_tried.setdefault(u.symbol_id, {})
        missing = [d for d in due if d not in have and (d not in tried or now - tried[d] >= GAP_RETRY)]
        if missing:
            lo = datetime.combine(missing[0], time(0), tzinfo=ET)
            hi = datetime.combine(missing[-1] + ONE_DAY, time(0), tzinfo=ET)
            wanted = set(due)
            fetched = [
                c for c in await self._client.candles(u.qid, lo, hi, "OneDay") if et_date(c.start) in wanted
            ]
            await self._db(self._store_daily, u.symbol_id, fetched)
            tried.update(dict.fromkeys(missing, now))
        return await self._db(self._read_daily, u.symbol_id, start, end)

    def _complete_sessions(self, start: date, end: date, now: datetime) -> list[date]:
        today = et_date(now)
        out: list[date] = []
        d = start
        while d <= min(end, today):
            if self._cal.is_session(d) and (d < today or now >= self._cal.session_close(d)):
                out.append(d)
            d += ONE_DAY
        return out

    def _stored_days(self, symbol_id: int, start: date, end: date) -> set[date]:
        with self._factory() as s:
            return set(
                s.execute(
                    select(m.DailyCandle.date).where(
                        m.DailyCandle.symbol_id == symbol_id,
                        m.DailyCandle.date >= start,
                        m.DailyCandle.date <= end,
                    )
                ).scalars()
            )

    def _store_daily(self, symbol_id: int, candles: list[Candle]) -> None:
        with session_scope(self._factory) as s:
            repo.upsert_daily_candles(s, symbol_id, candles)

    def _read_daily(self, symbol_id: int, start: date, end: date) -> list[Candle]:
        with self._factory() as s:
            rows = s.execute(
                select(m.DailyCandle)
                .where(
                    m.DailyCandle.symbol_id == symbol_id,
                    m.DailyCandle.date >= start,
                    m.DailyCandle.date <= end,
                )
                .order_by(m.DailyCandle.date)
            ).scalars()
            out: list[Candle] = []
            for r in rows:  # a daily bar starts at 00:00 ET of its trading date
                day = datetime.combine(r.date, time(0), tzinfo=ET).astimezone(UTC)
                out.append(Candle(day, day + ONE_DAY, r.open, r.high, r.low, r.close, r.volume, r.vwap))
            return out

    # --- the calendar -------------------------------------------------------------------------------------
    def is_open(self, now: datetime) -> bool:
        """Inside the regular session: open <= now < close (an early close ends it early)."""
        day = et_date(now)
        if not self._cal.is_session(day):
            return False
        return self._cal.session_open(day) <= now < self._cal.session_close(day)

    def is_monthly(self, expiry: date) -> bool:
        return expiry == monthly_expiry(expiry.year, expiry.month, self._cal)
