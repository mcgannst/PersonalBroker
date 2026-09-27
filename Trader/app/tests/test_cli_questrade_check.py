"""questrade-check with the token owner faked and Questrade mocked by respx (no database)."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
import respx
from typer.testing import CliRunner, Result

from trader import bootstrap
from trader.adapters.questrade import auth as auth_module
from trader.adapters.questrade.auth import AccessToken, QuestradeAuthError
from trader.cli import app
from trader.market.clock import FixedClock

BASE = "https://api05.iq.questrade.com/v1/"
NOW = datetime(2026, 9, 27, 14, 0, tzinfo=UTC)


class FakeAuth:
    dead = False

    def __init__(self, *_: object) -> None:
        pass

    def access(self) -> AccessToken:
        if FakeAuth.dead:
            raise QuestradeAuthError("The refresh token has already been used or has expired.")
        return AccessToken("tok-1", BASE, NOW + timedelta(minutes=30))

    def force_refresh(self) -> AccessToken:
        return self.access()


@pytest.fixture(autouse=True)
def fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeAuth.dead = False
    core = SimpleNamespace(factory=None, crypto=None, clock=FixedClock(NOW))
    monkeypatch.setattr(bootstrap, "build_core", lambda: core)
    monkeypatch.setattr(auth_module, "QuestradeAuth", FakeAuth)


def mock_time() -> None:
    respx.get(BASE + "time").mock(
        return_value=httpx.Response(200, json={"time": "2026-09-27T10:00:00-04:00"})
    )


def mock_symbol(name: str) -> respx.Route:
    return respx.get(BASE + "symbols").mock(
        return_value=httpx.Response(
            200,
            json={
                "symbols": [
                    {
                        "symbol": name,
                        "symbolId": 34987,
                        "listingExchange": "ARCA",
                        "currency": "USD",
                        "description": name,
                        "isTradable": True,
                        "isQuotable": True,
                    }
                ]
            },
        )
    )


def assert_clean_failure(result: Result, text: str) -> None:
    assert result.exit_code == 1
    assert "questrade-check failed: " in result.output
    assert text in result.output
    assert "Traceback" not in result.output
    assert isinstance(result.exception, SystemExit)


@respx.mock
def test_prints_quote_with_upper_cased_symbol_and_unknown_delay() -> None:
    mock_time()
    route = mock_symbol("SPY")
    respx.get(BASE + "markets/quotes").mock(
        return_value=httpx.Response(
            200,
            json={"quotes": [{"symbol": "SPY", "symbolId": 34987, "lastTradePrice": 771.35}]},
        )
    )
    result = CliRunner().invoke(app, ["questrade-check", "--symbol", "spy"])
    assert result.exit_code == 0, result.output
    assert route.calls[0].request.url.params["names"] == "SPY"
    assert "SPY: last=771.35" in result.output
    assert "delay=unknown" in result.output


@respx.mock
def test_unknown_symbol_fails_cleanly() -> None:
    mock_time()
    respx.get(BASE + "symbols").mock(return_value=httpx.Response(200, json={"symbols": []}))
    result = CliRunner().invoke(app, ["questrade-check", "--symbol", "nope"])
    assert_clean_failure(result, "unknown symbol NOPE")


@respx.mock
def test_empty_quotes_fail_cleanly() -> None:
    mock_time()
    mock_symbol("SPY")
    respx.get(BASE + "markets/quotes").mock(return_value=httpx.Response(200, json={"quotes": []}))
    result = CliRunner().invoke(app, ["questrade-check"])
    assert_clean_failure(result, "no quote")


def test_auth_error_fails_cleanly() -> None:
    FakeAuth.dead = True
    result = CliRunner().invoke(app, ["questrade-check"])
    assert_clean_failure(result, "already been used")


@respx.mock
def test_api_error_fails_cleanly() -> None:
    respx.get(BASE + "time").mock(return_value=httpx.Response(403, json={"code": 1016}))
    result = CliRunner().invoke(app, ["questrade-check"])
    assert_clean_failure(result, "HTTP 403")
