"""Async, rate-limited Questrade market-data client (SPEC §4.1; spikes S1–S4).

Findings built in: at most 100 names per symbols call; intraday history only ~3 months, and windows
entirely before it return HTTP 400, so start times are clamped; candles include 04:00–20:00 ET
(filtering to regular hours is the caller's job, see trader.market.indicators.regular_hours).
"""

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, Self

import httpx

from trader.adapters.questrade.auth import AccessToken, TokenSource
from trader.adapters.questrade.models import CandleRequest, QtQuote, QtSymbol
from trader.market.clock import Clock
from trader.market.types import Candle, Interval

INTRADAY_HISTORY = timedelta(days=88)
NAMES_PER_CALL = 100
MAX_ATTEMPTS = 5
MAX_CANDLES_PER_REQUEST = 20_000
MAX_429_PAUSE = 30.0
# A cached access token is reused until this long before it expires (by the client's clock).
TOKEN_REUSE_MARGIN = timedelta(seconds=120)
Category = Literal["market", "account"]

INTERVAL_LENGTH: dict[Interval, timedelta] = {
    "OneMinute": timedelta(minutes=1),
    "FiveMinutes": timedelta(minutes=5),
    "FifteenMinutes": timedelta(minutes=15),
    "OneHour": timedelta(hours=1),
    "OneDay": timedelta(days=1),
}


class QuestradeApiError(Exception):
    """A Questrade call failed. `status` is the HTTP status, or 0 for a transport or parse failure."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"HTTP {status}: {message}")
        self.status = status


@dataclass
class CallStats:
    """Counters for one rate-limit category since the client was created (numbers only, never a token
    or URL). `pause_s` sums the 429 pauses put on the shared bucket; overlapping pauses both count."""

    requests: int = 0
    http_429: int = 0
    pause_s: float = 0.0
    http_5xx: int = 0
    transport_errors: int = 0


class TokenBucket:
    """Spaces calls evenly at `rate` per second. `pause_until` holds back every caller.

    A waiter cancelled before its slot (a batch deadline) gives the queue back once no waiter is left,
    so the next call is spaced from the last slot actually used, not from the abandoned queue."""

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
        self._paused_until = float("-inf")
        self._lock = asyncio.Lock()
        self._waiters = 0
        self._last_granted: float | None = None

    def now(self) -> float:
        """The bucket's own monotonic time (the clock `pause_until` is measured on)."""
        return self._monotonic()

    def pause_until(self, t_monotonic: float) -> None:
        """No call is let through before `t_monotonic`, including callers already waiting for a slot."""
        self._paused_until = max(self._paused_until, t_monotonic)
        self._next = t_monotonic if self._next is None else max(self._next, t_monotonic)

    async def acquire(self) -> None:
        self._waiters += 1
        try:
            while True:
                async with self._lock:
                    now: float = self._monotonic()
                    slot: float = max(now, self._paused_until)
                    if self._next is not None:
                        slot = max(slot, self._next)
                    self._next = slot + self._interval
                if slot > now:
                    await self._sleep(slot - now)
                # A pause that began while we waited and ends after our slot sends us back into the queue.
                if self._paused_until <= slot:
                    if self._last_granted is None or slot > self._last_granted:
                        self._last_granted = slot
                    return
        except asyncio.CancelledError:
            if self._waiters == 1:  # the last waiter left: drop the slots nobody will use
                self._next = None if self._last_granted is None else self._last_granted + self._interval
            raise
        finally:
            self._waiters -= 1


def _dt(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp has no UTC offset: {value!r}")
    return parsed.astimezone(UTC)


def _dec(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _int_or_none(value: str | None) -> int | None:
    try:
        return None if value is None else int(value)
    except ValueError:
        return None


def _float_or_none(value: str | None) -> float | None:
    try:
        return None if value is None else float(value)
    except ValueError:
        return None


def _query_time(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


class QuestradeClient:
    """Questrade market-data calls, rate limited per category and retried on 401/429/5xx/transport errors.

    Caveats for callers:
    - The intraday history clamp (`INTRADAY_HISTORY`) is measured from the injected clock, so pass a
      real wall clock. Phase 5 replay must NOT pass a ReplayClock here: the clamp would then cut off
      data that Questrade still has (or keep windows it no longer has).
    - Questrade returns at most 20,000 candles per request. Keep windows small; `candles()` raises
      ValueError when the window could hold more bars than that (e.g. OneMinute over ~14 days).
    - The access token is cached in the client and reused until TOKEN_REUSE_MARGIN before it
      expires (by the injected clock); an asyncio.Lock makes concurrent requests fetch it once. A 401
      force-refreshes it (once per request, and only if no other request already replaced it).
    """

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
        self._owns_http = http is None
        self._http: httpx.AsyncClient = http if http is not None else httpx.AsyncClient(timeout=30)
        self._sleep = sleep
        self._buckets: dict[Category, TokenBucket] = {
            "market": TokenBucket(market_rps, sleep=sleep),
            "account": TokenBucket(account_rps, sleep=sleep),
        }
        self.rate_limit_remaining: dict[Category, int] = {}
        self.stats: dict[Category, CallStats] = {"market": CallStats(), "account": CallStats()}
        self._token: AccessToken | None = None
        self._replaced_token: AccessToken | None = None
        self._token_lock = asyncio.Lock()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._owns_http:
            await self._http.aclose()

    def _pause_after_429(self, resp: httpx.Response, category: Category, attempt: int) -> None:
        reset: float | None = _float_or_none(resp.headers.get("X-RateLimit-Reset"))
        reset_delta: float = 0.0 if reset is None else reset - self._clock.now().timestamp()
        pause: float = min(MAX_429_PAUSE, max(reset_delta, 0.5 * 2**attempt))
        self.stats[category].pause_s += pause
        bucket = self._buckets[category]
        bucket.pause_until(bucket.now() + pause)

    @staticmethod
    def _transport_message(exc: httpx.TransportError, token: AccessToken) -> str:
        text: str = str(exc).replace(token.token, "<token>").replace(token.api_base, "<api>")
        return f"{type(exc).__name__}: {text}"[:300]

    def _masked_message(self, exc: BaseException) -> str:
        """`TypeName: message`, at most 300 chars, with the cached token (and the one it replaced,
        which an in-flight request may still have used) and their api bases masked."""
        text: str = str(exc)
        for token in (self._token, self._replaced_token):
            if token is not None:
                text = text.replace(token.token, "<token>").replace(token.api_base, "<api>")
        return f"{type(exc).__name__}: {text}"[:300]

    def _cache_token(self, token: AccessToken) -> None:
        if self._token is not None and self._token is not token:
            self._replaced_token = self._token
        self._token = token

    async def _access(self) -> AccessToken:
        """The cached access token, fetched from the TokenSource only when missing or near expiry."""
        async with self._token_lock:
            token = self._token
            if token is None or token.expires_at - TOKEN_REUSE_MARGIN <= self._clock.now():
                token = await asyncio.to_thread(self._tokens.access)
                self._cache_token(token)
            return token

    async def _refresh_after_401(self, rejected: AccessToken) -> None:
        """Force a refresh and cache the new token, unless another request already replaced the
        rejected one (then that newer token is used instead)."""
        async with self._token_lock:
            if self._token is None or self._token is rejected:
                self._cache_token(await asyncio.to_thread(self._tokens.force_refresh))

    async def _get(self, path: str, params: dict[str, str], category: Category) -> Any:
        refreshed = False
        last_status: int = 0
        last_text: str = ""
        stats = self.stats[category]
        for attempt in range(MAX_ATTEMPTS):
            final: bool = attempt == MAX_ATTEMPTS - 1
            await self._buckets[category].acquire()
            token: AccessToken = await self._access()
            stats.requests += 1
            try:
                resp: httpx.Response = await self._http.get(
                    token.api_base + path,
                    params=params,
                    headers={"Authorization": f"Bearer {token.token}"},
                )
            except httpx.TransportError as exc:
                stats.transport_errors += 1
                last_status, last_text = 0, self._transport_message(exc, token)
                if not final:
                    await self._sleep(0.5 * 2**attempt)
                continue
            remaining: int | None = _int_or_none(resp.headers.get("X-RateLimit-Remaining"))
            if remaining is not None:
                self.rate_limit_remaining[category] = remaining
            last_status, last_text = resp.status_code, resp.text[:300]
            if resp.status_code == 200:
                return json.loads(resp.text, parse_float=Decimal)
            if resp.status_code == 401 and not refreshed:
                refreshed = True
                await self._refresh_after_401(token)
                continue
            if resp.status_code == 429:
                stats.http_429 += 1
                if not final:
                    self._pause_after_429(resp, category, attempt)
                continue
            if resp.status_code >= 500:
                stats.http_5xx += 1
                if not final:
                    await self._sleep(0.5 * 2**attempt)
                continue
            raise QuestradeApiError(resp.status_code, last_text)
        raise QuestradeApiError(last_status, last_text)

    async def server_time(self) -> datetime:
        data = await self._get("time", {}, "account")
        value: datetime | None = _dt(data.get("time"))
        if value is None:
            raise QuestradeApiError(0, "time response had no time")
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
            chunk: list[str] = list(names[i : i + NAMES_PER_CALL])
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
        """`delay` is None when Questrade omits it (unknown, not real-time)."""
        out: list[QtQuote] = []
        for i in range(0, len(ids), NAMES_PER_CALL):
            chunk: Sequence[int] = ids[i : i + NAMES_PER_CALL]
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
                    delay=None if q.get("delay") is None else int(q["delay"]),
                    is_halted=bool(q.get("isHalted")),
                    vwap=_dec(q.get("VWAP")),
                )
                for q in data.get("quotes", [])
            )
        return out

    async def candles(
        self, symbol_id: int, start: datetime, end: datetime, interval: Interval
    ) -> list[Candle]:
        """Raises ValueError for naive datetimes or a window that could exceed 20,000 bars."""
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("candles() needs timezone-aware start and end")
        if interval != "OneDay":
            start = max(start, self._clock.now() - INTRADAY_HISTORY)
        start, end = start.replace(microsecond=0), end.replace(microsecond=0)
        if start >= end:
            return []
        if (end - start) / INTERVAL_LENGTH[interval] > MAX_CANDLES_PER_REQUEST:
            raise ValueError(
                f"{interval} window {start.isoformat()}..{end.isoformat()} could exceed "
                f"{MAX_CANDLES_PER_REQUEST} candles; request smaller windows"
            )
        data = await self._get(
            f"markets/candles/{symbol_id}",
            {"startTime": _query_time(start), "endTime": _query_time(end), "interval": interval},
            "market",
        )
        if not isinstance(data, dict):
            raise ValueError(f"candles response is a {type(data).__name__}, not an object")
        out: list[Candle] = []
        for c in data.get("candles", []):
            s, e = _dt(c["start"]), _dt(c["end"])
            if s is None or e is None:
                raise ValueError("candle without start or end")
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
        self, reqs: Sequence[CandleRequest], *, deadline_s: float | None = None
    ) -> dict[CandleRequest, list[Candle] | QuestradeApiError]:
        """Every completed request gets its own result: candles, or the error for that request alone.

        With `deadline_s`, requests still outstanding when it passes are cancelled (and awaited, so none
        outlives the call) and left out of the result; every request that completed keeps its result.
        Without it, every request is waited for. Cancelling the call itself cancels every request."""

        async def one(r: CandleRequest) -> list[Candle] | QuestradeApiError:
            try:
                return await self.candles(r.symbol_id, r.start, r.end, r.interval)
            except QuestradeApiError as exc:
                return exc
            except (ValueError, KeyError, TypeError, InvalidOperation) as exc:
                # json.JSONDecodeError is a ValueError.
                return QuestradeApiError(0, f"{type(exc).__name__}: {exc}"[:300])
            except Exception as exc:  # noqa: BLE001 - one request's failure must not end the batch
                # e.g. httpx.DecodingError, httpx.TooManyRedirects, a QuestradeAuthError from a forced
                # refresh. CancelledError is a BaseException and still propagates.
                return QuestradeApiError(0, self._masked_message(exc))

        unique: list[CandleRequest] = list(dict.fromkeys(reqs))
        tasks: dict[CandleRequest, asyncio.Task[list[Candle] | QuestradeApiError]] = {
            r: asyncio.create_task(one(r)) for r in unique
        }
        try:
            if tasks:
                await asyncio.wait(tasks.values(), timeout=deadline_s)
        finally:
            outstanding = [t for t in tasks.values() if not t.done()]
            for t in outstanding:
                t.cancel()
            if outstanding:
                await asyncio.gather(*outstanding, return_exceptions=True)
        return {r: t.result() for r, t in tasks.items() if not t.cancelled()}
