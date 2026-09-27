from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from tests.strategies.fakes import CAL, NOW, SESSION, FakeData, make_ctx, position, quote
from trader.adapters.questrade.models import QtQuote
from trader.broker.types import FillEvent, OrderView
from trader.strategies.base import DecisionNote, Exit
from trader.strategies.registry import load_plugin
from trader.strategies.spy_overlay import DECISION_EVENT, SpyOverlay, SpyOverlayParams

SPY = 99


def data_with_spy(last: str | None, prior: str | None = "500.00") -> FakeData:
    d = FakeData()
    d.ids["SPY"] = SPY
    if prior is not None:
        d.closes[SPY] = Decimal(prior)
    if last is not None:
        d.quote_map[SPY] = quote(SPY, last, last, last)
    return d


async def decide(d: FakeData, positions: list[int]) -> tuple[list[object], dict[str, object], str]:
    s = SpyOverlay()
    ctx = make_ctx(d, s.params, positions=[position(p, 1) for p in positions])
    (event,) = s.schedule(CAL)
    intents = await s.on_event(ctx, event)
    note = next(n for n in ctx.notes if n.message.startswith("overlay"))
    return list(intents), note.data, note.level


async def test_negative_spy_exits_every_entry_position() -> None:
    intents, data, _ = await decide(data_with_spy("497.50"), [5, 6])
    assert intents == [
        Exit(5, "market", None, "overlay_negative"),
        Exit(6, "market", None, "overlay_negative"),
    ]
    assert data["decision"] == "exit" and data["spy_return"] == "-0.005000"
    assert data["position_ids"] == [5, 6]


async def test_a_flat_spy_counts_as_negative() -> None:
    intents, data, _ = await decide(data_with_spy("500.00"), [5])
    assert intents == [Exit(5, "market", None, "overlay_negative")] and data["decision"] == "exit"


async def test_positive_spy_holds_and_still_logs_the_decision() -> None:
    intents, data, level = await decide(data_with_spy("502.00"), [5])
    assert intents == [] and data["decision"] == "hold" and level == "info"
    assert data["spy_return"] == "0.004000" and data["prior_close"] == "500.00" and data["price"] == "502.00"


@pytest.mark.parametrize(("last", "prior"), [(None, "500.00"), ("497.00", None)])
async def test_missing_data_holds_with_a_warning(last: str | None, prior: str | None) -> None:
    intents, data, level = await decide(data_with_spy(last, prior), [5])
    assert intents == [] and data["decision"] == "hold" and level == "warning"


async def test_unknown_benchmark_holds_with_an_error() -> None:
    d = FakeData()
    intents, data, level = await decide(d, [5])
    assert intents == [] and level == "error" and data["decision"] == "hold"


def test_decision_time_is_close_minus_30m_even_on_early_closes() -> None:
    (event,) = SpyOverlay().schedule(CAL)
    assert event.key == DECISION_EVENT
    assert event.at.resolve(CAL, SESSION) == datetime(2026, 10, 6, 19, 30, tzinfo=UTC)  # 15:30 ET
    assert event.at.resolve(CAL, date(2026, 11, 27)) == datetime(2026, 11, 27, 17, 30, tzinfo=UTC)  # 12:30 ET


async def test_fills_need_nothing_from_the_overlay() -> None:
    s = SpyOverlay()
    ctx = make_ctx(FakeData(), s.params)
    fill = FillEvent(1, 1, 1, 1, "sell", "exit", 10, Decimal("20"), NOW, 5, 1, None, None, 2)
    assert await s.on_fill(ctx, fill) == []


@pytest.mark.parametrize(
    "bad", [{"decision_at": "15:30"}, {"benchmark": "spy"}, {"signal": "vwap"}, {"x": 1}]
)
def test_invalid_params_are_rejected(bad: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        SpyOverlayParams.model_validate(bad)


def test_the_plugin_loads_through_its_entry_point() -> None:
    cls = load_plugin("spy_overlay")
    assert cls is SpyOverlay and cls.kind == "overlay" and cls.version == "1.0.0"


# --- fix round 1 (gauntlet findings) ------------------------------------------------------------------


async def run_with(
    q: QtQuote | None, *, orders: list[OrderView] | None = None, now: datetime = NOW
) -> tuple[list[object], DecisionNote]:
    d = data_with_spy(None)
    if q is not None:
        d.quote_map[SPY] = q
    s = SpyOverlay()
    ctx = make_ctx(d, s.params, positions=[position(5, 1), position(6, 1)], orders=orders or [], now=now)
    (event,) = s.schedule(CAL)
    intents = await s.on_event(ctx, event)
    return list(intents), next(n for n in ctx.notes if n.message.startswith("overlay"))


async def test_a_tiny_positive_return_holds_even_though_it_rounds_to_zero() -> None:
    intents, note = await run_with(quote(SPY, "500.0001", "500.0001", "500.0001"))
    assert intents == [] and note.data["decision"] == "hold" and note.data["spy_return"] == "0.000000"


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"delay": 15}, "delayed"),
        ({"delay": None}, "delayed"),
        ({"last_trade_time": None}, "no_trade_time"),
        ({"last_trade_time": datetime(2026, 10, 5, 19, 59, tzinfo=UTC)}, "not_this_session"),
        ({"last_trade_time": NOW - timedelta(seconds=121)}, "stale"),
    ],
)
async def test_a_quote_that_is_not_current_holds_with_a_warning(
    change: dict[str, object], reason: str
) -> None:
    q = replace(quote(SPY, "497.00", "497.00", "497.00"), **change)  # type: ignore[arg-type]
    intents, note = await run_with(q)
    assert intents == [] and note.level == "warning"
    assert note.data["decision"] == "hold" and note.data["reason"] == reason
    assert note.data["benchmark"] == "SPY" and "quote_time" in note.data and "delay" in note.data


async def test_a_quote_just_inside_the_age_limit_is_used() -> None:
    q = replace(quote(SPY, "497.00", "497.00", "497.00"), last_trade_time=NOW - timedelta(seconds=120))
    intents, note = await run_with(q)
    assert note.data["decision"] == "exit" and len(intents) == 2
    assert note.data["quote_time"] == (NOW - timedelta(seconds=120)).isoformat() and note.data["delay"] == 0


@pytest.mark.parametrize(("last", "last_regular"), [("0", None), ("-1", None), ("0", "0")])
async def test_a_non_positive_price_counts_as_missing(last: str, last_regular: str | None) -> None:
    q = replace(
        quote(SPY, "497.00", "497.00", "497.00"),
        last=Decimal(last),
        last_regular=None if last_regular is None else Decimal(last_regular),
    )
    intents, note = await run_with(q)
    assert intents == [] and note.level == "warning" and note.data["reason"] == "missing"


def _order(oid: int, purpose: str, position_id: int) -> OrderView:
    return OrderView(
        oid,
        1,
        "sell",
        "stop" if purpose == "stop" else "market",
        purpose,  # type: ignore[arg-type]
        10,
        None,
        None,
        "working",
        position_id,
        1,
        None,
        NOW,
    )


async def test_exits_skip_positions_already_exiting_but_not_ones_with_only_a_stop() -> None:
    orders = [_order(40, "exit", 5), _order(41, "stop", 6)]
    intents, note = await run_with(quote(SPY, "497.00", "497.00", "497.00"), orders=orders)
    assert intents == [Exit(6, "market", None, "overlay_negative")]
    assert note.data["already_exiting"] == [5] and note.data["position_ids"] == [5, 6]


@pytest.mark.parametrize("bad", ["close", "close+5m", "open+60m", "open"])
def test_decision_at_must_be_before_the_close(bad: str) -> None:
    with pytest.raises(ValidationError):
        SpyOverlayParams(decision_at=bad)


def test_max_quote_age_is_bounded() -> None:
    assert SpyOverlayParams().max_quote_age_seconds == 120
    with pytest.raises(ValidationError):
        SpyOverlayParams(max_quote_age_seconds=0)
