"""OPTSIM T2: the Questrade option calls (chain, option quotes by ids and by filter, symbol details), the
POST path's retry rules and the live check. respx only: nothing here reaches the network."""

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
import respx

from tests.options.fakes import FakeQtOptions
from trader.adapters.questrade.auth import AccessToken
from trader.adapters.questrade.client import MAX_ATTEMPTS, QuestradeApiError, QuestradeClient
from trader.adapters.questrade.option_types import OptionQuoteClient
from trader.adapters.questrade.options_check import live_check
from trader.market.clock import FixedClock

BASE = "https://api05.iq.questrade.com/v1/"
NOW = datetime(2026, 10, 6, 14, 0, tzinfo=UTC)
OPTION_QUOTES = BASE + "markets/quotes/options"
EXPIRY_TEXT = "2026-10-30T00:00:00.000000-04:00"
PACKAGE_401 = {
    "code": 1022,
    "message": "The requested data is not included in your current market data package",
}


class FakeTokens:
    def __init__(self) -> None:
        self.forced = 0

    def access(self) -> AccessToken:
        return AccessToken(f"tok-{self.forced}", BASE, NOW + timedelta(minutes=30))

    def force_refresh(self, rejected: str | None = None) -> AccessToken:
        self.forced += 1
        return self.access()


class Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def client(tokens: FakeTokens | None = None, sleep: Sleeps | None = None) -> QuestradeClient:
    return QuestradeClient(tokens or FakeTokens(), FixedClock(NOW), sleep=sleep or Sleeps())


def quote_json(**kw: Any) -> dict[str, Any]:
    """One option quote as Questrade sent it on 2026-10-05."""
    out: dict[str, Any] = {
        "underlying": "F",
        "underlyingId": 19117,
        "symbol": "F30Oct26P14.50",
        "symbolId": 79433391,
        "bidPrice": 2.21,
        "bidSize": 339,
        "askPrice": 2.59,
        "askSize": 335,
        "lastTradePriceTrHrs": 2.5,
        "lastTradePrice": 2.5,
        "lastTradeSize": 0,
        "lastTradeTick": "Equal",
        "lastTradeTime": "2026-10-02T00:00:00.000000-04:00",
        "volume": 0,
        "openPrice": 0,
        "highPrice": 0,
        "lowPrice": 0,
        "volatility": 36.224907,
        "delta": -0.961621,
        "gamma": 0.065614,
        "theta": -0.002822,
        "vega": 0.002497,
        "rho": -0.010029,
        "openInterest": 95,
        "delay": 0,
        "isHalted": False,
        "VWAP": 0,
    }
    out.update(kw)
    return out


def quotes_response(*quotes: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json={"optionQuotes": list(quotes)})


def body_of(call: Any) -> Any:
    return json.loads(call.request.content)


def test_the_client_satisfies_the_option_protocol() -> None:
    typed: OptionQuoteClient = client()
    assert typed is not None


@respx.mock
async def test_chain_parses_expiries_roots_and_strikes() -> None:
    def expiry(text: str, strikes: list[dict[str, Any]], root: str = "F", mult: int = 100) -> dict[str, Any]:
        return {
            "expiryDate": text,
            "description": "FORD MOTOR CO",
            "listingExchange": "NYSE",
            "optionExerciseType": "American",
            "chainPerRoot": [{"optionRoot": root, "multiplier": mult, "chainPerStrikePrice": strikes}],
        }

    route = respx.get(BASE + "symbols/19117/options").mock(
        return_value=httpx.Response(
            200,
            json={
                "optionChain": [
                    expiry("2026-11-20T00:00:00.000000-05:00", [], root="F1", mult=106),
                    expiry(
                        EXPIRY_TEXT,
                        [
                            {"strikePrice": 14.5, "callSymbolId": 79433370, "putSymbolId": 79433391},
                            {"strikePrice": 15, "callSymbolId": 79433371, "putSymbolId": 79433392},
                        ],
                    ),
                ]
            },
        )
    )
    async with client() as c:
        first, second = await c.option_chain(19117)
    assert route.call_count == 1
    assert first.expiry == date(2026, 10, 30) and second.expiry == date(2026, 11, 20)  # ET dates, ascending
    (root,) = first.roots
    assert (root.root, root.multiplier) == ("F", 100)
    assert [(s.strike, s.call_id, s.put_id) for s in root.strikes] == [
        (Decimal("14.5"), 79433370, 79433391),
        (Decimal("15"), 79433371, 79433392),
    ]
    assert all(isinstance(s.strike, Decimal) for s in root.strikes)
    assert (second.roots[0].root, second.roots[0].multiplier, second.roots[0].strikes) == ("F1", 106, ())


@respx.mock
async def test_quotes_by_ids_are_batched_by_100() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        ids = json.loads(request.content)["optionIds"]
        return quotes_response(*(quote_json(symbolId=i, symbol=f"O{i}") for i in ids))

    route = respx.post(OPTION_QUOTES).mock(side_effect=handler)
    ids = list(range(1, 251))
    async with client() as c:
        got = await c.option_quotes(ids)
        assert await c.option_quotes([]) == []
        assert c.stats["market"].requests == 3 and c.stats["account"].requests == 0
    assert [len(body_of(call)["optionIds"]) for call in route.calls] == [100, 100, 50]
    assert [q.symbol_id for q in got] == ids
    request = route.calls[0].request
    assert request.headers["Authorization"] == "Bearer tok-0"
    assert request.headers["Content-Type"] == "application/json"

    q = got[0]
    assert (q.underlying, q.underlying_id, q.symbol) == ("F", 19117, "O1")
    assert (q.bid, q.ask, q.last) == (Decimal("2.21"), Decimal("2.59"), Decimal("2.5"))
    assert (q.bid_size, q.ask_size, q.volume, q.open_interest) == (339, 335, 0, 95)
    assert q.iv_pct == Decimal("36.224907")  # Questrade's percentage, unchanged
    assert (q.delta, q.gamma, q.theta) == (Decimal("-0.961621"), Decimal("0.065614"), Decimal("-0.002822"))
    assert (q.vega, q.rho, q.vwap) == (Decimal("0.002497"), Decimal("-0.010029"), Decimal("0"))
    assert q.last_trade_time == datetime(2026, 10, 2, 4, 0, tzinfo=UTC)
    assert (q.delay, q.is_halted) == (0, False)
    assert q.fetched_at == NOW and q.requested_at == NOW


@respx.mock
async def test_filter_body_is_exact() -> None:
    route = respx.post(OPTION_QUOTES).mock(return_value=quotes_response(quote_json()))
    async with client() as c:
        (bounded,) = await c.option_quotes_filter(
            19117, date(2026, 10, 30), "put", Decimal("12.5"), Decimal("16")
        )
        await c.option_quotes_filter(19117, date(2026, 11, 20), "call")
    assert bounded.symbol_id == 79433391
    assert body_of(route.calls[0]) == {
        "filters": [
            {
                "optionType": "Put",
                "underlyingId": 19117,
                "expiryDate": EXPIRY_TEXT,
                "minstrikePrice": 12.5,
                "maxstrikePrice": 16,
            }
        ]
    }
    # No strike bounds: the keys are left out. A November expiry carries the winter offset.
    assert body_of(route.calls[1]) == {
        "filters": [
            {"optionType": "Call", "underlyingId": 19117, "expiryDate": "2026-11-20T00:00:00.000000-05:00"}
        ]
    }


OK = quotes_response(quote_json())


@pytest.mark.parametrize(
    ("case", "responses", "calls", "forced", "raises"),
    [
        ("401_refreshes_once", [httpx.Response(401, json={"code": 1017, "message": "bad"}), OK], 2, 1, None),
        ("429_pauses_the_bucket", [httpx.Response(429), OK], 2, 0, None),
        ("5xx_backs_off", [httpx.Response(502), OK], 2, 0, None),
        ("transport_error_retried", [httpx.ConnectError("boom"), OK], 2, 0, None),
        ("package_401_raised_at_once", [httpx.Response(401, json=PACKAGE_401)], 1, 0, 401),
        ("five_failures_raise", [httpx.Response(503)] * MAX_ATTEMPTS, MAX_ATTEMPTS, 0, 503),
        ("second_401_is_raised", [httpx.Response(401), httpx.Response(401)], 2, 1, 401),
        ("400_is_not_retried", [httpx.Response(400, json={"code": 1002, "message": "bad"})], 1, 0, 400),
    ],
)
@respx.mock
async def test_post_retries(
    case: str, responses: list[Any], calls: int, forced: int, raises: int | None
) -> None:
    route = respx.post(OPTION_QUOTES).mock(side_effect=responses)
    tokens, sleeps = FakeTokens(), Sleeps()
    async with client(tokens, sleeps) as c:
        if raises is None:
            (quote,) = await c.option_quotes([79433391])
            assert quote.symbol_id == 79433391
        else:
            with pytest.raises(QuestradeApiError) as err:
                await c.option_quotes([79433391])
            assert err.value.status == raises
        stats = c.stats["market"]
    assert route.call_count == calls and tokens.forced == forced and stats.requests == calls
    assert all(body_of(call) == {"optionIds": [79433391]} for call in route.calls)  # the body is re-sent
    if case == "401_refreshes_once":
        assert route.calls[1].request.headers["Authorization"] == "Bearer tok-1"
    if case == "429_pauses_the_bucket":
        assert stats.http_429 == 1 and stats.pause_s == 0.5  # no Reset header: 0.5 * 2**0 on the bucket
    if case == "5xx_backs_off":
        assert stats.http_5xx == 1 and 0.5 in sleeps.calls
    if case == "transport_error_retried":
        assert stats.transport_errors == 1 and 0.5 in sleeps.calls
    if case == "package_401_raised_at_once":
        assert err.value.code == 1022
    if case == "five_failures_raise":
        assert stats.http_5xx == MAX_ATTEMPTS


@respx.mock
async def test_null_and_missing_fields_become_none() -> None:
    numbers = ("bidPrice", "askPrice", "lastTradePrice", "bidSize", "askSize", "volume", "openInterest")
    greeks = ("volatility", "delta", "gamma", "theta", "vega", "rho", "VWAP", "lastTradeTime")
    nulls = quote_json(symbolId=1, **dict.fromkeys(numbers + greeks))
    missing = {"symbolId": 2, "symbol": "X", "underlying": "F", "underlyingId": 19117, "delay": 0}
    zero_bid = quote_json(symbolId=3, bidPrice=0, isHalted=True)
    respx.post(OPTION_QUOTES).mock(return_value=quotes_response(nulls, missing, zero_bid))
    async with client() as c:
        a, b, z = await c.option_quotes([1, 2, 3])
    for q in (a, b):
        assert (q.bid, q.ask, q.last, q.bid_size, q.ask_size) == (None, None, None, None, None)
        assert (q.volume, q.open_interest, q.iv_pct, q.vwap, q.last_trade_time) == (None,) * 5
        assert (q.delta, q.gamma, q.theta, q.vega, q.rho) == (None,) * 5
        assert q.is_halted is False
    # A 0 bid is passed on as 0, not None: the fill model tells `zero_bid` from `one_sided`.
    assert z.bid == Decimal("0") and z.bid is not None and z.is_halted is True


@respx.mock
async def test_delay_missing_is_none() -> None:
    without = quote_json(symbolId=1)
    del without["delay"]
    respx.post(OPTION_QUOTES).mock(
        return_value=quotes_response(
            without, quote_json(symbolId=2, delay=None), quote_json(symbolId=3, delay=15)
        )
    )
    async with client() as c:
        a, b, late = await c.option_quotes([1, 2, 3])
    assert a.delay is None and b.delay is None and late.delay == 15


def details_json(symbol_id: int, **kw: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "symbol": f"S{symbol_id}",
        "symbolId": symbol_id,
        "description": "FORD MOTOR CO",
        "securityType": "Stock",
        "listingExchange": "NYSE",
        "currency": "USD",
        "eps": 1.46,
        "pe": 9.86,
        "marketCap": 57200000000,
        "dividend": 0.15,
        "exDate": "2026-08-11T00:00:00.000000-04:00",
        "yield": 4.17,
        "industrySector": "ConsumerCyclical",
        "industryGroup": "AutosAndAutoParts",
        "industrySubgroup": "AutoManufacturers",
        "hasOptions": True,
        "prevDayClosePrice": 14.4,
    }
    out.update(kw)
    return out


@respx.mock
async def test_symbol_details_parse_and_chunking() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        asked = [int(x) for x in request.url.params["ids"].split(",")]
        return httpx.Response(200, json={"symbols": [details_json(i) for i in asked if i != 150]})

    route = respx.get(BASE + "symbols").mock(side_effect=handler)
    ids = list(range(1, 151))
    async with client() as c:
        got = await c.symbol_details(ids)
        assert c.stats["market"].requests == 2
    assert [len(call.request.url.params["ids"].split(",")) for call in route.calls] == [100, 50]
    assert set(got) == set(ids) - {150}  # an id Questrade does not return is left out
    d = got[7]
    assert (d.symbol_id, d.symbol, d.description) == (7, "S7", "FORD MOTOR CO")
    assert (d.security_type, d.listing_exchange, d.currency, d.has_options) == ("Stock", "NYSE", "USD", True)
    assert (d.eps, d.pe, d.dividend) == (Decimal("1.46"), Decimal("9.86"), Decimal("0.15"))
    assert d.market_cap == Decimal("57200000000") and isinstance(d.market_cap, Decimal)
    assert (d.industry_sector, d.industry_group) == ("ConsumerCyclical", "AutosAndAutoParts")


@respx.mock
async def test_ex_date_and_yield_parse() -> None:
    bare = details_json(2, exDate=None, eps=None, pe=None, dividend=None, industrySector="", hasOptions=False)
    del bare["yield"], bare["marketCap"], bare["industryGroup"]
    respx.get(BASE + "symbols").mock(
        return_value=httpx.Response(200, json={"symbols": [details_json(1), bare]})
    )
    async with client() as c:
        got = await c.symbol_details([1, 2])
    assert got[1].ex_date == date(2026, 8, 11) and got[1].yield_pct == Decimal("4.17")
    b = got[2]
    assert (b.ex_date, b.yield_pct, b.eps, b.pe, b.dividend, b.market_cap) == (None,) * 6
    assert (b.industry_sector, b.industry_group, b.has_options) == (None, None, False)


@respx.mock
async def test_live_check_reports_delay_and_masks_secrets() -> None:
    qt = FakeQtOptions(FixedClock(NOW))
    qt.add_symbol("F", 19117, eps=Decimal("1.46"), ex_date=date(2026, 8, 11))
    qt.set_share_quote(19117, "14.40")
    qt.add_expiry(19117, date(2026, 10, 30), [("13", 1, 2), ("14.5", 3, 4), ("16", 5, 6)])
    qt.add_expiry(19117, date(2026, 11, 20), [("14", 7, 8)])
    qt.set_option_quote(4, "0.40", "0.44", delay=0, iv_pct="36.2", delta="-0.48")

    report = await live_check(qt, "f")
    assert report["ok"] is True and report["error"] is None and report["real_time"] is True
    assert (report["symbol"], report["symbol_id"], report["underlying_last"]) == ("F", 19117, "14.40")
    assert (report["expiries"], report["first_expiry"], report["last_expiry"]) == (
        2,
        "2026-10-30",
        "2026-11-20",
    )
    put = report["put"]
    assert (put["symbol_id"], put["strike"], put["delay"]) == (4, "14.5", 0)  # the strike nearest 14.40
    assert (put["bid"], put["ask"], put["iv_pct"], put["delta"]) == ("0.40", "0.44", "36.2", "-0.48")
    assert report["details"]["present"] == ["eps", "ex_date"] and report["details"]["has_options"] is True
    json.dumps(report)  # plain JSON values only

    qt.set_option_quote(4, "0.40", "0.44", delay=15)
    delayed = await live_check(qt, "F")
    assert delayed["put"]["delay"] == 15 and delayed["real_time"] is False
    qt.set_option_quote(4, "0.40", "0.44", delay=None)
    assert (await live_check(qt, "F"))["real_time"] is False  # unknown delay is never real-time
    unknown = await live_check(qt, "NOPE")
    assert unknown["ok"] is False and unknown["error"] == "unknown symbol NOPE"

    # A failing call ends the check with a masked error: neither the token nor the API host is reported.
    secret = "tok-0"
    respx.get(BASE + "symbols").mock(
        side_effect=httpx.ConnectError(f"cannot reach {BASE}symbols as {secret}")
    )
    async with client() as c:
        failed = await live_check(c, "F")
    text = json.dumps(failed)
    assert failed["ok"] is False and "ConnectError" in failed["error"]
    assert secret not in text and "api05" not in text
