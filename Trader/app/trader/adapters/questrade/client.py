"""Async, rate-limited Questrade market-data client (SPEC §4.1; spikes S1–S4).

Findings built in: at most 100 names per symbols call; intraday history only ~3 months, and windows
entirely before it return HTTP 400, so start times are clamped; candles include 04:00–20:00 ET
(filtering to regular hours is the caller's job, see trader.market.indicators.regular_hours).
"""

import asyncio
import inspect
import json
import math
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, Self

import httpx
import structlog

from trader.adapters.questrade.auth import AccessToken, TokenSource
from trader.adapters.questrade.models import CandleRequest, QtQuote, QtSymbol
from trader.adapters.questrade.option_types import (
    QtChainExpiry,
    QtChainRoot,
    QtChainStrike,
    QtOptionQuote,
    QtSymbolDetails,
)
from trader.logging_setup import redact_text
from trader.market.clock import ET, Clock
from trader.market.types import Candle, Interval
from trader.options.types import Right

INTRADAY_HISTORY = timedelta(days=88)
NAMES_PER_CALL = 100
MAX_ATTEMPTS = 5
MAX_CANDLES_PER_REQUEST = 20_000
MAX_429_PAUSE = 30.0
# Market data is paced below Questrade's 20 req/s market limit: at exactly 20 the per-second window
# still answers 429 (2026-09-28 opening bars: 543 requests, 3 x 429 alone, 38 x 429 with other load).
MARKET_RPS = 17.0
# A 429 whose X-RateLimit-Reset is within WINDOW_429_MAX_PAUSE is the per-second window: pause until the
# reset plus a small margin, at least WINDOW_429_MIN_PAUSE. A Reset further ahead is a real (hourly)
# exhaustion and is waited for, at most MAX_429_PAUSE. Without a usable Reset (missing, unparseable, or
# more than STALE_429_RESET in the past, i.e. clock skew) the pause backs off 0.5 * 2**attempt, capped
# at WINDOW_429_MAX_PAUSE for market data.
WINDOW_429_MARGIN = 0.05
WINDOW_429_MIN_PAUSE = 0.05
WINDOW_429_MAX_PAUSE = 2.0
STALE_429_RESET = 1.0
# A cached access token is reused until this long before it expires (by the client's clock).
TOKEN_REUSE_MARGIN = timedelta(seconds=120)
# FIX-401: when the first FAIL_FAST_401 completed results of a candles_many batch are all HTTP 401 (each
# already after a forced token refresh), the rest are cancelled and reported with that 401 at once,
# instead of burning the whole deadline on requests Questrade will refuse (Tue 2026-09-29: 757 x 401).
FAIL_FAST_401 = 20
# QUOTEBAR: Questrade's code on a 401 for data the account's market-data package does not include ("...current
# market data package..."): Stephen's package serves intraday candles only ~10 minutes late. Raised at once,
# never retried with a new token (the token is fine).
PACKAGE_401_CODE = 1022
Category = Literal["market", "account"]

log = structlog.get_logger("questrade.client")

INTERVAL_LENGTH: dict[Interval, timedelta] = {
    "OneMinute": timedelta(minutes=1),
    "FiveMinutes": timedelta(minutes=5),
    "FifteenMinutes": timedelta(minutes=15),
    "OneHour": timedelta(hours=1),
    "OneDay": timedelta(days=1),
}


class QuestradeApiError(Exception):
    """A Questrade call failed. `status` is the HTTP status, or 0 for a transport or parse failure.

    FIX-401: `code` and `qt_message` are Questrade's own error code and message from a JSON error body
    ({"code": 1017, "message": "Access token is invalid"}), the message masked, on one line and at most
    MAX_QT_MESSAGE characters; both None when the body carried none. `summary` is the short form used in
    logs and missing reasons: "HTTP 401 1017 Access token is invalid", or just "HTTP 404"."""

    def __init__(
        self, status: int, message: str, *, code: int | None = None, qt_message: str | None = None
    ) -> None:
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.code = code
        self.qt_message = _short_message(qt_message) if qt_message else None

    @classmethod
    def from_response(cls, resp: httpx.Response) -> "QuestradeApiError":
        code, message = _questrade_error(resp)
        return cls(resp.status_code, resp.text[:300], code=code, qt_message=message)

    @property
    def summary(self) -> str:
        parts = [f"HTTP {self.status}"]
        if self.code is not None:
            parts.append(str(self.code))
        if self.qt_message:
            parts.append(self.qt_message)
        return " ".join(parts)[:MAX_REASON]


MAX_QT_MESSAGE = 120
MAX_REASON = 200
MISSING_PREFIX = "questrade_error: "


def _short_message(text: str) -> str:
    flat = " ".join(redact_text(text).split())
    return flat if len(flat) <= MAX_QT_MESSAGE else flat[: MAX_QT_MESSAGE - 1] + "…"


def _questrade_error(resp: httpx.Response) -> tuple[int | None, str | None]:
    """Questrade's {"code": ..., "message": ...} error body, or (None, None) when it isn't one."""
    try:
        data = json.loads(resp.text)
    except ValueError:
        return None, None
    if not isinstance(data, dict):
        return None, None
    raw_code, raw_message = data.get("code"), data.get("message")
    code = raw_code if isinstance(raw_code, int) and not isinstance(raw_code, bool) else None
    message = raw_message if isinstance(raw_message, str) and raw_message.strip() else None
    return code, message


def missing_reason(exc: "QuestradeApiError") -> str:
    """An opening-bar missing reason: "questrade_error: HTTP 401 1017 Access token is invalid" (at most 200
    characters), or "questrade_error: HTTP 500" when Questrade sent no code or message."""
    return (MISSING_PREFIX + exc.summary)[:MAX_REASON]


def reason_key(reason: str) -> str:
    """A missing reason without Questrade's code and message ("questrade_error: HTTP 401"), for counting."""
    if not reason.startswith(MISSING_PREFIX):
        return reason
    words = reason.removeprefix(MISSING_PREFIX).split()
    return MISSING_PREFIX + " ".join(words[:2])


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


def _takes_rejected(tokens: TokenSource) -> bool:
    """Whether `tokens.force_refresh` accepts the rejected token (FIX-401). A TokenSource written before it
    (test doubles among them) takes no argument and is called without one."""
    try:
        params = inspect.signature(tokens.force_refresh).parameters
    except (TypeError, ValueError):
        return False
    return "rejected" in params or any(p.kind is inspect.Parameter.VAR_POSITIONAL for p in params.values())


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
        market_rps: float = MARKET_RPS,
        account_rps: float = 30.0,
        http: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._tokens = tokens
        self._clock = clock
        self._owns_http = http is None
        self._http: httpx.AsyncClient = http if http is not None else httpx.AsyncClient(timeout=30)
        self._sleep = sleep
        self._buckets: dict[Category, TokenBucket] = {
            "market": TokenBucket(market_rps, monotonic=monotonic, sleep=sleep),
            "account": TokenBucket(account_rps, monotonic=monotonic, sleep=sleep),
        }
        self.rate_limit_remaining: dict[Category, int] = {}
        self.stats: dict[Category, CallStats] = {"market": CallStats(), "account": CallStats()}
        self._token: AccessToken | None = None
        self._replaced_token: AccessToken | None = None
        self._token_lock = asyncio.Lock()
        self._refresh_takes_rejected = _takes_rejected(tokens)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._owns_http:
            await self._http.aclose()

    def _pause_after_429(self, resp: httpx.Response, category: Category, attempt: int) -> None:
        reset: float | None = _float_or_none(resp.headers.get("X-RateLimit-Reset"))
        delta: float | None = None if reset is None else reset - self._clock.now().timestamp()
        pause: float
        if delta is None or math.isnan(delta) or delta < -STALE_429_RESET:  # no usable Reset
            cap: float = WINDOW_429_MAX_PAUSE if category == "market" else MAX_429_PAUSE
            pause = min(cap, 0.5 * 2**attempt)
        elif delta > WINDOW_429_MAX_PAUSE:
            pause = min(MAX_429_PAUSE, delta)
        else:
            pause = min(WINDOW_429_MAX_PAUSE, max(WINDOW_429_MIN_PAUSE, delta + WINDOW_429_MARGIN))
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
        rejected one (then that newer token is used instead). Tokens are compared by their string, never
        by object identity: a TokenSource hands out a new object for the same token (FIX-401). The
        rejected string goes to `force_refresh`, so the TokenSource never hands the same token back."""
        async with self._token_lock:
            if self._token is None or self._token.token == rejected.token:
                if self._refresh_takes_rejected:
                    fresh = await asyncio.to_thread(self._tokens.force_refresh, rejected.token)
                else:  # a TokenSource written before FIX-401
                    fresh = await asyncio.to_thread(self._tokens.force_refresh)
                self._cache_token(fresh)

    async def _get(self, path: str, params: dict[str, str], category: Category) -> Any:
        return (await self._get_sent(path, params, category))[0]

    async def _get_sent(self, path: str, params: dict[str, str], category: Category) -> tuple[Any, datetime]:
        """`_get`, with when the answering attempt was sent (FIX-DAY1b: after the bucket's pacing and any
        429 pause, by the injected clock)."""
        return await self._request_sent(path, params, category, None)

    async def _post_sent(self, path: str, body: dict[str, Any], category: Category) -> tuple[Any, datetime]:
        """A JSON POST under exactly the GET rules (pacing, one forced refresh on 401, the package-401 rule,
        429 pause, 5xx and transport back-off). Only for POSTs that read (OPTSIM: option quotes), since a
        failed attempt is sent again."""
        return await self._request_sent(path, {}, category, json.dumps(body))

    async def _request_sent(
        self, path: str, params: dict[str, str], category: Category, body: str | None
    ) -> tuple[Any, datetime]:
        """One call with retries: a GET with `params`, or a POST of the JSON text `body` when given."""
        refreshed = False
        last_status: int = 0
        last_text: str = ""
        last_resp: httpx.Response | None = None
        stats = self.stats[category]
        for attempt in range(MAX_ATTEMPTS):
            final: bool = attempt == MAX_ATTEMPTS - 1
            await self._buckets[category].acquire()
            token: AccessToken = await self._access()
            stats.requests += 1
            sent_at: datetime = self._clock.now()
            try:
                resp: httpx.Response
                if body is None:
                    resp = await self._http.get(
                        token.api_base + path,
                        params=params,
                        headers={"Authorization": f"Bearer {token.token}"},
                    )
                else:
                    resp = await self._http.post(
                        token.api_base + path,
                        content=body,
                        headers={
                            "Authorization": f"Bearer {token.token}",
                            "Content-Type": "application/json",
                        },
                    )
            except httpx.TransportError as exc:
                stats.transport_errors += 1
                last_status, last_text, last_resp = 0, self._transport_message(exc, token), None
                if not final:
                    await self._sleep(0.5 * 2**attempt)
                continue
            remaining: int | None = _int_or_none(resp.headers.get("X-RateLimit-Remaining"))
            if remaining is not None:
                self.rate_limit_remaining[category] = remaining
            last_status, last_text, last_resp = resp.status_code, resp.text[:300], resp
            if resp.status_code == 200:
                return json.loads(resp.text, parse_float=Decimal), sent_at
            if resp.status_code == 401 and _questrade_error(resp)[0] == PACKAGE_401_CODE:
                # QUOTEBAR: not a token problem (the data is outside the market-data package, e.g. a candle
                # less than ~10 minutes old): a forced refresh would only rotate the refresh token.
                raise QuestradeApiError.from_response(resp)
            if resp.status_code == 401 and not refreshed:
                refreshed = True
                # TOKEN-401: why Questrade refused a token (its code and message, never the token), so a
                # token another program invalidated can be told from one Questrade dropped by itself.
                code, message = _questrade_error(resp)
                log.warning(
                    "questrade.token_rejected",
                    path=path,
                    code=code,
                    message=(message or "")[:80],
                    seconds_to_expiry=int((token.expires_at - self._clock.now()).total_seconds()),
                )
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
            raise QuestradeApiError.from_response(resp)
        if last_resp is not None and last_resp.status_code == last_status:
            raise QuestradeApiError.from_response(last_resp)
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
            data, requested_at = await self._get_sent(
                "markets/quotes", {"ids": ",".join(str(x) for x in chunk)}, "market"
            )
            fetched_at = self._clock.now()  # FIX-DAY1: the fill model judges the book's freshness by it
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
                    open=_dec(q.get("openPrice")),
                    high=_dec(q.get("highPrice")),
                    low=_dec(q.get("lowPrice")),
                    fetched_at=fetched_at,
                    requested_at=requested_at,
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
        self,
        reqs: Sequence[CandleRequest],
        *,
        deadline_s: float | None = None,
        fail_fast_401: int | None = FAIL_FAST_401,
    ) -> dict[CandleRequest, list[Candle] | QuestradeApiError]:
        """Every completed request gets its own result: candles, or the error for that request alone.

        With `deadline_s`, requests still outstanding when it passes are cancelled (and awaited, so none
        outlives the call) and left out of the result; every request that completed keeps its result.
        Without it, every request is waited for. Cancelling the call itself cancels every request.

        FIX-401 fail fast: when the first `fail_fast_401` completed results are all HTTP 401 (a 401 is only
        returned after the request's forced token refresh), the outstanding requests are cancelled and
        each gets that 401 error too, so the batch returns at once; one error line is logged. None: off."""

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
        failed_fast: QuestradeApiError | None = None
        completed = 0
        try:
            if tasks:
                failed_fast, completed = await self._wait_batch(
                    list(tasks.values()), deadline_s, fail_fast_401
                )
        finally:
            outstanding = [t for t in tasks.values() if not t.done()]
            for t in outstanding:
                t.cancel()
            if outstanding:
                await asyncio.gather(*outstanding, return_exceptions=True)
        out: dict[CandleRequest, list[Candle] | QuestradeApiError] = {
            r: t.result() for r, t in tasks.items() if not t.cancelled()
        }
        if failed_fast is not None:
            for r in tasks:
                out.setdefault(r, failed_fast)
            log.error(
                "questrade.candles_fail_fast",
                requests=len(tasks),
                completed=completed,
                cancelled=len(tasks) - completed,
                reason=failed_fast.summary,
            )
        return out

    @staticmethod
    async def _wait_batch(
        tasks: list[asyncio.Task[list[Candle] | QuestradeApiError]],
        deadline_s: float | None,
        fail_fast_401: int | None,
    ) -> tuple[QuestradeApiError | None, int]:
        """Wait for the batch until the deadline. While fail fast is still undecided, results are taken as
        they complete: (the first 401, completed count) when the first `fail_fast_401` are all 401, else
        (None, completed) once a non-401 result shows up (then the rest is waited for in one go)."""
        if fail_fast_401 is None or fail_fast_401 < 1 or len(tasks) <= fail_fast_401:
            await asyncio.wait(tasks, timeout=deadline_s)
            return None, sum(1 for t in tasks if t.done())
        loop = asyncio.get_running_loop()
        until = None if deadline_s is None else loop.time() + deadline_s
        pending: set[asyncio.Task[list[Candle] | QuestradeApiError]] = set(tasks)
        completed = 0
        first_401: QuestradeApiError | None = None
        while pending:
            timeout = None if until is None else max(0.0, until - loop.time())
            done, pending = await asyncio.wait(pending, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            if not done:  # the deadline passed
                return None, completed
            completed += len(done)
            for t in done:
                result = t.result()
                if not (isinstance(result, QuestradeApiError) and result.status == 401):
                    if pending:
                        remaining = None if until is None else max(0.0, until - loop.time())
                        await asyncio.wait(pending, timeout=remaining)
                    return None, sum(1 for x in tasks if x.done())
                first_401 = first_401 or result
            if completed >= fail_fast_401:
                return first_401, completed
        return None, completed

    # --- OPTSIM: option chain, option quotes, symbol details (all on the "market" bucket) ---

    async def option_chain(self, symbol_id: int) -> list[QtChainExpiry]:
        """The underlying's chain, expiries ascending. An expiry is the ET date of the `expiryDate` sent."""
        data = await self._get(f"symbols/{symbol_id}/options", {}, "market")
        out: list[QtChainExpiry] = []
        for e in data.get("optionChain", []):
            expiry = _et_date(e.get("expiryDate"))
            if expiry is None:
                raise ValueError("option chain expiry without expiryDate")
            roots = tuple(
                QtChainRoot(
                    root=r.get("optionRoot", ""),
                    multiplier=int(r.get("multiplier") or 100),
                    strikes=tuple(
                        QtChainStrike(
                            strike=Decimal(str(s["strikePrice"])),
                            call_id=int(s["callSymbolId"]),
                            put_id=int(s["putSymbolId"]),
                        )
                        for s in r.get("chainPerStrikePrice", [])
                    ),
                )
                for r in e.get("chainPerRoot", [])
            )
            out.append(QtChainExpiry(expiry=expiry, roots=roots))
        return sorted(out, key=lambda x: x.expiry)

    async def _option_quotes_call(self, body: dict[str, Any]) -> list[QtOptionQuote]:
        data, requested_at = await self._post_sent("markets/quotes/options", body, "market")
        fetched_at = self._clock.now()
        return [
            QtOptionQuote(
                symbol_id=int(q["symbolId"]),
                symbol=q.get("symbol", ""),
                underlying=q.get("underlying", ""),
                underlying_id=int(q.get("underlyingId") or 0),
                bid=_dec(q.get("bidPrice")),
                ask=_dec(q.get("askPrice")),
                last=_dec(q.get("lastTradePrice")),
                bid_size=_opt_int(q.get("bidSize")),
                ask_size=_opt_int(q.get("askSize")),
                volume=_opt_int(q.get("volume")),
                open_interest=_opt_int(q.get("openInterest")),
                iv_pct=_dec(q.get("volatility")),
                delta=_dec(q.get("delta")),
                gamma=_dec(q.get("gamma")),
                theta=_dec(q.get("theta")),
                vega=_dec(q.get("vega")),
                rho=_dec(q.get("rho")),
                last_trade_time=_dt(q.get("lastTradeTime")),
                delay=_opt_int(q.get("delay")),
                is_halted=bool(q.get("isHalted")),
                vwap=_dec(q.get("VWAP")),
                fetched_at=fetched_at,
                requested_at=requested_at,
            )
            for q in data.get("optionQuotes", [])
        ]

    async def option_quotes(self, ids: Sequence[int]) -> list[QtOptionQuote]:
        """Quotes for option symbol ids, OPTION_IDS_PER_CALL per request. Numbers are passed on as sent: a
        null is None, a 0 bid stays 0, `iv_pct` is Questrade's percentage; `delay` None when omitted."""
        out: list[QtOptionQuote] = []
        for i in range(0, len(ids), OPTION_IDS_PER_CALL):
            chunk = [int(x) for x in ids[i : i + OPTION_IDS_PER_CALL]]
            out.extend(await self._option_quotes_call({"optionIds": chunk}))
        return out

    async def option_quotes_filter(
        self,
        underlying_id: int,
        expiry: date,
        right: Right,
        min_strike: Decimal | None = None,
        max_strike: Decimal | None = None,
    ) -> list[QtOptionQuote]:
        """Every quote of one expiry and right in one request, optionally within strike bounds (inclusive).
        `expiry` is sent the way the chain returns it: midnight ET with its UTC offset."""
        flt: dict[str, Any] = {
            "optionType": "Call" if right == "call" else "Put",
            "underlyingId": int(underlying_id),
            "expiryDate": _expiry_text(expiry),
        }
        if min_strike is not None:
            flt["minstrikePrice"] = _json_number(min_strike)
        if max_strike is not None:
            flt["maxstrikePrice"] = _json_number(max_strike)
        return await self._option_quotes_call({"filters": [flt]})

    async def symbol_details(self, ids: Sequence[int]) -> dict[int, QtSymbolDetails]:
        """Fundamentals per symbol id (`GET symbols?ids=`, NAMES_PER_CALL per request). Unknown ids are left
        out; `ex_date` is the ET date of `exDate`."""
        out: dict[int, QtSymbolDetails] = {}
        for i in range(0, len(ids), NAMES_PER_CALL):
            chunk = ids[i : i + NAMES_PER_CALL]
            data = await self._get("symbols", {"ids": ",".join(str(x) for x in chunk)}, "market")
            for s in data.get("symbols", []):
                symbol_id = int(s["symbolId"])
                out[symbol_id] = QtSymbolDetails(
                    symbol_id=symbol_id,
                    symbol=s.get("symbol", ""),
                    description=s.get("description") or "",
                    security_type=s.get("securityType") or "",
                    listing_exchange=s.get("listingExchange") or "",
                    currency=s.get("currency") or "",
                    has_options=bool(s.get("hasOptions")),
                    eps=_dec(s.get("eps")),
                    pe=_dec(s.get("pe")),
                    market_cap=_dec(s.get("marketCap")),
                    dividend=_dec(s.get("dividend")),
                    ex_date=_et_date(s.get("exDate")),
                    yield_pct=_dec(s.get("yield")),
                    industry_sector=s.get("industrySector") or None,
                    industry_group=s.get("industryGroup") or None,
                )
        return out


# ASSUMPTION (risk R11): option quotes accept 100 ids per request, like share quotes; checked live at T17.
OPTION_IDS_PER_CALL = 100


def _opt_int(value: Any) -> int | None:
    return None if value is None else int(value)


def _et_date(value: str | None) -> date | None:
    """The ET calendar date of a Questrade timestamp ("2026-10-30T00:00:00.000000-04:00"), or None."""
    parsed = _dt(value)
    return None if parsed is None else parsed.astimezone(ET).date()


def _expiry_text(expiry: date) -> str:
    """An expiry as the chain returns it: "2026-10-30T00:00:00.000000-04:00" (midnight ET)."""
    return datetime(expiry.year, expiry.month, expiry.day, tzinfo=ET).isoformat(timespec="microseconds")


def _json_number(value: Decimal) -> int | float:
    """A strike for a JSON request body (the only place a Decimal leaves as a float: the wire format)."""
    return int(value) if value == value.to_integral_value() else float(value)
