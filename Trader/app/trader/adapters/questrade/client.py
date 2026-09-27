"""Async, rate-limited Questrade market-data client (SPEC §4.1; spikes S1–S4).

Findings built in: at most 100 names per symbols call; intraday history only ~3 months, and windows
entirely before it return HTTP 400, so start times are clamped; candles include 04:00–20:00 ET
(filtering to regular hours is the caller's job, see trader.market.indicators.regular_hours).
"""

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal, Self

import httpx

from trader.adapters.questrade.auth import TokenSource
from trader.adapters.questrade.models import CandleRequest, QtQuote, QtSymbol
from trader.market.clock import Clock
from trader.market.types import Candle, Interval

INTRADAY_HISTORY = timedelta(days=88)
NAMES_PER_CALL = 100
MAX_ATTEMPTS = 5
Category = Literal["market", "account"]


class QuestradeApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"HTTP {status}: {message}")
        self.status = status


class TokenBucket:
    """Spaces calls evenly at `rate` per second."""

    def __init__(
        self,
        rate: float,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._interval = 1.0 / rate
        self._monotonic = monotonic
        self._sleep = sleep
        self._next: float | None = None
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = self._monotonic()
            slot = now if self._next is None else max(now, self._next)
            self._next = slot + self._interval
        if slot > now:
            await self._sleep(slot - now)


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value).astimezone(UTC) if value else None


def _dec(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


class QuestradeClient:
    def __init__(
        self,
        tokens: TokenSource,
        clock: Clock,
        *,
        market_rps: float = 20.0,
        account_rps: float = 30.0,
        http: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._tokens = tokens
        self._clock = clock
        self._http = http or httpx.AsyncClient(timeout=30)
        self._sleep = sleep
        self._buckets: dict[Category, TokenBucket] = {
            "market": TokenBucket(market_rps, sleep=sleep),
            "account": TokenBucket(account_rps, sleep=sleep),
        }
        self.rate_limit_remaining: dict[str, int] = {}

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._http.aclose()

    async def _get(self, path: str, params: dict[str, str], category: Category) -> Any:
        refreshed = False
        last_status, last_text = 0, ""
        for attempt in range(MAX_ATTEMPTS):
            await self._buckets[category].acquire()
            token = await asyncio.to_thread(self._tokens.access)
            resp = await self._http.get(
                token.api_base + path, params=params, headers={"Authorization": f"Bearer {token.token}"}
            )
            if "X-RateLimit-Remaining" in resp.headers:
                self.rate_limit_remaining[category] = int(resp.headers["X-RateLimit-Remaining"])
            last_status, last_text = resp.status_code, resp.text[:300]
            if resp.status_code == 200:
                return json.loads(resp.text, parse_float=Decimal)
            if resp.status_code == 401 and not refreshed:
                refreshed = True
                await asyncio.to_thread(self._tokens.force_refresh)
                continue
            if resp.status_code == 429:
                reset = float(resp.headers.get("X-RateLimit-Reset", "0"))
                await self._sleep(min(5.0, max(0.5, reset - self._clock.now().timestamp())))
                continue
            if resp.status_code >= 500:
                await self._sleep(0.5 * 2**attempt)
                continue
            raise QuestradeApiError(resp.status_code, last_text)
        raise QuestradeApiError(last_status, last_text)

    async def server_time(self) -> datetime:
        data = await self._get("time", {}, "account")
        value = _dt(data["time"])
        assert value is not None
        return value

    async def _symbols_call(self, names: Sequence[str]) -> dict[str, QtSymbol]:
        data = await self._get("symbols", {"names": ",".join(names)}, "market")
        return {
            s["symbol"]: QtSymbol(
                symbol_id=int(s["symbolId"]),
                symbol=s["symbol"],
                listing_exchange=s.get("listingExchange", ""),
                currency=s.get("currency", ""),
                description=s.get("description", ""),
                is_tradable=bool(s.get("isTradable")),
                is_quotable=bool(s.get("isQuotable")),
            )
            for s in data.get("symbols", [])
        }

    async def symbols_by_names(self, names: Sequence[str]) -> dict[str, QtSymbol]:
        """Unknown names are left out. A chunk that fails is retried name by name."""
        out: dict[str, QtSymbol] = {}
        for i in range(0, len(names), NAMES_PER_CALL):
            chunk = list(names[i : i + NAMES_PER_CALL])
            try:
                out.update(await self._symbols_call(chunk))
            except QuestradeApiError as exc:
                if exc.status != 400:
                    raise
                for name in chunk:
                    try:
                        out.update(await self._symbols_call([name]))
                    except QuestradeApiError as one:
                        if one.status != 400:
                            raise
        return out

    async def quotes(self, ids: Sequence[int]) -> list[QtQuote]:
        out: list[QtQuote] = []
        for i in range(0, len(ids), NAMES_PER_CALL):
            chunk = ids[i : i + NAMES_PER_CALL]
            data = await self._get("markets/quotes", {"ids": ",".join(str(x) for x in chunk)}, "market")
            out.extend(
                QtQuote(
                    symbol_id=int(q["symbolId"]),
                    symbol=q["symbol"],
                    bid=_dec(q.get("bidPrice")),
                    ask=_dec(q.get("askPrice")),
                    last=_dec(q.get("lastTradePrice")),
                    last_regular=_dec(q.get("lastTradePriceTrHrs")),
                    volume=int(q.get("volume") or 0),
                    last_trade_time=_dt(q.get("lastTradeTime")),
                    delay=int(q.get("delay") or 0),
                    is_halted=bool(q.get("isHalted")),
                    vwap=_dec(q.get("VWAP")),
                )
                for q in data.get("quotes", [])
            )
        return out

    async def candles(
        self, symbol_id: int, start: datetime, end: datetime, interval: Interval
    ) -> list[Candle]:
        if interval != "OneDay":
            start = max(start, self._clock.now() - INTRADAY_HISTORY)
        if start >= end:
            return []
        data = await self._get(
            f"markets/candles/{symbol_id}",
            {"startTime": start.isoformat(), "endTime": end.isoformat(), "interval": interval},
            "market",
        )
        out = []
        for c in data.get("candles", []):
            s, e = _dt(c["start"]), _dt(c["end"])
            assert s is not None and e is not None
            out.append(
                Candle(
                    start=s,
                    end=e,
                    open=Decimal(c["open"]),
                    high=Decimal(c["high"]),
                    low=Decimal(c["low"]),
                    close=Decimal(c["close"]),
                    volume=int(c["volume"]),
                    vwap=_dec(c.get("VWAP")),
                )
            )
        return out

    async def candles_many(
        self, reqs: Sequence[CandleRequest]
    ) -> dict[CandleRequest, list[Candle] | QuestradeApiError]:
        async def one(r: CandleRequest) -> list[Candle] | QuestradeApiError:
            try:
                return await self.candles(r.symbol_id, r.start, r.end, r.interval)
            except QuestradeApiError as exc:
                return exc

        results = await asyncio.gather(*(one(r) for r in reqs))
        return dict(zip(reqs, results, strict=True))
