from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from tests.strategies.fakes import CAL, NOW, SESSION, FakeData, make_ctx, position, quote
from trader.broker.types import FillEvent
from trader.strategies.base import Exit
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
