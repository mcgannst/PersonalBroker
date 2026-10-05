"""OPTSIM T1: the contracts every later task builds against. Value types and intents refuse floats, the
price helpers use the per-share, credit-positive convention, the fakes and the toy plug-in match their
protocols (names, parameters, async or sync), `FakeBook` passes the book contract, the CLI shell is
registered, and `ApiServices.options` defaults to None."""

import inspect
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, get_args

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

import trader.bootstrap
from tests.api.test_ts_contract import ts_unions
from tests.fakes_api import make_services, test_core
from tests.options import factories as f
from tests.options.contract_book import BookContract, BookEnv
from tests.options.fakes import (
    AcceptAll,
    FakeBook,
    FakeFacts,
    FakeOptionBroker,
    FakeOptionMarket,
    FakePromptStore,
    FakeQtOptions,
    FakeRegistry,
    RecordingHost,
    make_option_services,
)
from tests.options.toy_plugin import ToyCallBuyer, ToyParams
from trader.adapters.questrade.option_types import OptionQuoteClient
from trader.api import schemas
from trader.bootstrap import Core
from trader.cli import app
from trader.db import models as m
from trader.market.clock import FixedClock
from trader.option_strategies import base
from trader.option_strategies.base import (
    CancelOrder,
    CloseStructure,
    OpenStructure,
    OptionStrategy,
    Reprice,
    RollStructure,
    SellShares,
)
from trader.options import protocols as p
from trader.options import types as t
from trader.options.account import active_options_run_id
from trader.options.types import ContractKey, LegSpec, contract_label, dte, net_price, round_tick

runner = CliRunner()
NIGHT = datetime(2026, 9, 29, 3, 45, tzinfo=UTC)  # Monday 23:45 ET: outside the trading day
D = Decimal


# --- floats are refused -------------------------------------------------------------------------------------

LEG = LegSpec("option", "sell", 1, "F", ContractKey("F", f.EXPIRY, D("14.50"), "put"))


@pytest.mark.parametrize(
    "build",
    [
        lambda: ContractKey("F", f.EXPIRY, 14.5, "put"),  # type: ignore[arg-type]
        lambda: f.make_request(net_limit=0.45),  # type: ignore[arg-type]
        lambda: f.make_request(take_profit_pct=0.5),  # type: ignore[arg-type]
        lambda: OpenStructure((LEG,), 1, "limit", 0.45, "day", "x"),  # type: ignore[arg-type]
        lambda: OpenStructure((LEG,), 1, "limit", D("0.45"), "day", "x", take_profit_pct=0.5),  # type: ignore[arg-type]
        lambda: CloseStructure(1, "limit", 0.2, "x"),  # type: ignore[arg-type]
        lambda: RollStructure(1, (LEG,), "limit", 0.1, "x"),  # type: ignore[arg-type]
        lambda: RollStructure(1, (LEG,), "limit", D("0.1"), "x", take_profit_pct=0.5),  # type: ignore[arg-type]
        lambda: Reprice(1, 0.4),  # type: ignore[arg-type]
        lambda: Reprice(1, None),  # type: ignore[arg-type]
    ],
)
def test_intents_and_requests_reject_floats(build: Any) -> None:
    with pytest.raises(TypeError, match="must be a Decimal"):
        build()


def test_decimal_intents_and_requests_are_accepted_and_reasons_are_bounded() -> None:
    assert f.make_request(net_limit=D("0.45"), take_profit_pct=D("0.5")).net_limit == D("0.45")
    assert f.make_request(order_type="market").net_limit is None
    assert OpenStructure((LEG,), 1, "market", None, "day", "x").walk is False
    assert CloseStructure(1, "market", None, "x").qty is None
    assert (CancelOrder(1, "x").reason, SellShares(1, "x").structure_id) == ("x", 1)
    with pytest.raises(ValueError, match="reason"):
        f.make_request(reason="r" * 101)
    with pytest.raises(ValueError, match="reason"):
        SellShares(1, "r" * 101)


# --- net price and ticks ------------------------------------------------------------------------------------

SELL_PUT = f.make_leg(1, side="sell")
BUY_PUT = f.make_leg(2, side="buy", contract_id=2)
BUY_SHARES = f.make_leg(1, instrument="shares", side="buy", ratio=100)
SELL_CALL = f.make_leg(2, side="sell", contract_id=3)


@pytest.mark.parametrize(
    ("legs", "prices", "multipliers", "net"),
    [
        ([SELL_PUT], {1: D("0.45")}, {1: 100}, D("0.45")),  # a credit
        ([f.make_leg(1, side="buy")], {1: D("1.20")}, {1: 100}, D("-1.20")),  # a debit
        ([SELL_PUT, BUY_PUT], {1: D("0.45"), 2: D("0.15")}, {1: 100, 2: 100}, D("0.30")),  # credit spread
        ([BUY_PUT, SELL_PUT], {1: D("0.15"), 2: D("0.45")}, {}, D("-0.30")),  # debit spread, default 100
        ([BUY_SHARES, SELL_CALL], {1: D("14.50"), 2: D("0.40")}, {2: 100}, D("-14.10")),  # a buy-write
        ([f.make_leg(1, side="sell", ratio=2)], {1: D("0.45")}, {1: 100}, D("0.90")),  # ratio 2
        ([SELL_PUT], {1: D("0.45")}, {1: 50}, D("0.225")),  # an adjusted contract of 50 shares
    ],
)
def test_net_price_and_round_tick(
    legs: list[t.OrderLeg], prices: dict[int, Decimal], multipliers: dict[int, int], net: Decimal
) -> None:
    assert net_price(legs, prices, multipliers) == net
    # On the tick grid: `up` is toward a better net for the owner (more credit, less debit), for both signs.
    tick = D("0.005")  # every net of the table is on this grid
    off_grid = net + D("0.003")
    assert round_tick(net, tick, "up") == round_tick(net, tick, "down") == net.quantize(D("0.001"))
    up, down = round_tick(off_grid, tick, "up"), round_tick(off_grid, tick, "down")
    assert down < off_grid < up and up - down == tick
    assert up % tick == 0 and down % tick == 0


@pytest.mark.parametrize(
    ("value", "tick", "up", "down"),
    [
        ("0.453", "0.01", "0.46", "0.45"),
        ("-1.203", "0.01", "-1.20", "-1.21"),
        ("0.47", "0.05", "0.50", "0.45"),
        ("-0.47", "0.05", "-0.45", "-0.50"),
        ("0.45", "0.05", "0.45", "0.45"),
        ("0", "0.01", "0", "0"),
    ],
)
def test_round_tick_both_signs(value: str, tick: str, up: str, down: str) -> None:
    assert round_tick(D(value), D(tick), "up") == D(up)
    assert round_tick(D(value), D(tick), "down") == D(down)
    with pytest.raises(ValueError):
        round_tick(D(value), D("0"), "up")


def test_dte_and_contract_label() -> None:
    assert dte(f.EXPIRY, f.SESSION) == 45 and dte(f.SESSION, f.SESSION) == 0
    assert dte(f.SESSION, f.EXPIRY) == -45
    put = f.make_contract(expiry=date(2026, 10, 30), strike="14.5000")
    assert contract_label(put) == "F 2026-10-30 P 14.50"
    assert contract_label(f.make_contract(right="call", strike="15")) == "F 2026-11-20 C 15.00"
    assert contract_label(f.make_contract(strike="7.125")) == "F 2026-11-20 P 7.125"


# --- the book contract on the reference book ----------------------------------------------------------------


class TestFakeBookPassesTheBookContract(BookContract):
    @pytest.fixture
    def book_env(self) -> BookEnv:
        market = FakeOptionMarket(FixedClock(f.T0))
        market.add_underlying("F", "14.80")
        put = market.add_contract("F", f.EXPIRY, "14.50", "put")
        call = market.add_contract("F", f.EXPIRY, "15.00", "call")
        return BookEnv(FakeBook(self.START_CASH, market.contracts), put, call)


# --- fakes and the toy plug-in against their protocols ------------------------------------------------------


def _methods(proto: type) -> dict[str, Any]:
    return {
        name: member
        for name, member in vars(proto).items()
        if inspect.isfunction(member) and not name.startswith("_")
    }


def assert_implements(obj: object, proto: type) -> None:
    """Every method of the protocol exists on `obj` with the same parameters (name, kind, default or not)
    and the same async-or-sync nature."""
    methods = _methods(proto)
    assert methods, f"{proto.__name__} has no methods"
    for name, wanted in methods.items():
        got = getattr(type(obj), name, None)
        assert got is not None, f"{type(obj).__name__} lacks {proto.__name__}.{name}"
        assert inspect.iscoroutinefunction(got) == inspect.iscoroutinefunction(wanted), name

        def shape(fn: Any) -> list[tuple[str, Any, bool]]:
            params = list(inspect.signature(fn).parameters.values())[1:]  # without self
            return [(q.name, q.kind, q.default is inspect.Parameter.empty) for q in params]

        assert shape(got) == shape(wanted), f"{type(obj).__name__}.{name} differs from {proto.__name__}"


def test_fakes_and_toy_plugin_satisfy_their_protocols() -> None:
    clock = FixedClock(f.T0)
    market = FakeOptionMarket(clock)
    pairs: list[tuple[object, type]] = [
        (market, p.OptionMarketView),
        (FakeBook(), p.OptionBook),
        (FakeOptionBroker(market), p.OptionBroker),
        (AcceptAll(), p.CollateralEngine),
        (FakeQtOptions(clock), OptionQuoteClient),
        (FakePromptStore(clock), p.PromptStore),
        (RecordingHost(), p.StrategyHost),
        (FakeFacts(), p.FactsProvider),
        (FakeRegistry(clock, ToyCallBuyer), p.OptionStrategyRegistryView),
        (ToyCallBuyer(), OptionStrategy),
    ]
    for obj, proto in pairs:
        assert_implements(obj, proto)
    toy = ToyCallBuyer(ToyParams(min_dte=30))
    assert isinstance(toy, OptionStrategy)
    assert (toy.key, toy.manual_events) == ("toy_call", ("toy_buy",))
    assert [(e.key, str(e.at)) for e in toy.schedule(None)] == [("toy_buy", "open+5m")]  # type: ignore[arg-type]
    import re

    assert re.fullmatch(base.KEY_PATTERN, toy.key) and base.ENTRY_POINT_GROUP == "trader.option_strategies"


async def test_the_fake_broker_fills_through_the_fake_book() -> None:
    """The fakes work together: sell a put, see the cash, the structure and the fill; buy it back."""
    clock = FixedClock(f.T0)
    market = FakeOptionMarket(clock)
    market.add_underlying("F", "14.80")
    put = market.add_contract("F", f.EXPIRY, "14.50", "put", bid="0.45", ask="0.50", delta="-0.30")
    broker = FakeOptionBroker(market)
    opened = await broker.submit(f.make_request([f.make_leg(contract_id=put.id)], take_profit_pct=D("0.5")))
    assert opened.order.status == "working" and (await broker.order(opened.order.id)) == opened.order
    (fill,) = await broker.fill_all_at_market()
    assert (fill.net_price, fill.fees, fill.realized_pnl) == (D("0.45"), D("0.99"), None)
    (structure,) = await broker.structures()
    assert (structure.entry_net, structure.take_profit_net) == (D("0.45"), D("-0.225"))
    assert [(x.qty, x.avg_price) for x in structure.positions] == [(-1, D("0.45"))]
    account = await broker.account()
    assert account.cash == D("5000") + D("45") - D("0.99") and account.premium_collected == D("45")
    assert account.positions_value == D("-50") and account.equity == account.cash - D("50")
    closing = f.make_leg(side="buy", effect="close", contract_id=put.id)
    closed = await broker.submit(
        f.make_request([closing], intent="close", structure_id=structure.id, order_type="market")
    )
    event = broker.fill(closed.order.id, {1: D("0.20")})
    assert event.realized_pnl == D("25") and await broker.structures() == []
    (done,) = await broker.structures(open_only=False)
    assert (done.state, done.close_reason, done.fees_total) == ("closed", "closed", D("1.98"))
    assert [o.status for o in await broker.orders()] == ["filled", "filled"]


async def test_the_toy_plugin_buys_the_nearest_call_at_least_30_days_out() -> None:
    clock = FixedClock(f.T0)
    market = FakeOptionMarket(clock)
    market.add_underlying("F", "14.80")
    market.add_contract("F", date(2026, 10, 16), "15.00", "call")  # 10 days: too near
    for strike in ("14.00", "15.00", "16.00"):
        market.add_contract("F", f.EXPIRY, strike, "call")
    toy = ToyCallBuyer()
    state: dict[str, dict[str, Any]] = {}

    class State:
        def get(self, scope_key: str) -> dict[str, Any] | None:
            return state.get(scope_key)

        def put(self, scope_key: str, value: dict[str, Any]) -> None:
            state[scope_key] = value

        def delete(self, scope_key: str) -> None:
            state.pop(scope_key, None)

    from tests.fakes_api import CAL
    from trader.options.settings import OptionSettings

    ctx = base.OptionStrategyContext(
        clock=clock,
        calendar=CAL,
        session_date=f.SESSION,
        run_id=1,
        strategy_key=toy.key,
        config_id=1,
        params=toy.params,
        settings=OptionSettings(),
        market=market,
        account=await FakeOptionBroker(market).account(),
        structures=[],
        orders=[],
        state=State(),
        factory=None,  # type: ignore[arg-type]
        usd_cad_rate=D("1.38"),
        prompt=lambda key: None,
        equity_on=lambda day: None,
    )
    (intent,) = await toy.on_event(ctx, base.OptionEvent("toy_buy", f.SESSION))
    assert isinstance(intent, OpenStructure) and (intent.order_type, intent.qty) == ("market", 1)
    (leg,) = intent.legs
    assert (leg.side, leg.contract) == ("buy", ContractKey("F", f.EXPIRY, D("15.00"), "call"))
    panel = await toy.panel(ctx)
    assert [tb.key for tb in panel.tables] == ["holdings"] and [a.key for a in panel.actions] == ["note"]
    result = await toy.on_action(ctx, base.PanelActionRequest("note", None, "watch the dividend"))
    assert result.ok and state["note"]["text"] == "watch the dividend"
    assert await toy.on_event(ctx, base.OptionEvent("other", f.SESSION)) == []


# --- the TypeScript literals of the option API --------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "literal"),
    [
        ("OptRight", t.Right),
        ("OptEffect", t.Effect),
        ("OptInstrument", t.Instrument),
        ("OptOrderType", t.OptOrderType),
        ("OptTif", t.Tif),
        ("OptOrderIntent", t.OrderIntent),
        ("OptOrderStatus", t.OrderStatus),
        ("OptStructureKind", t.StructureKind),
        ("OptStructureState", t.StructureState),
        ("OptCloseReason", t.CloseReason),
        ("OptRejectReason", t.RejectReason),
        ("OptPromptStatus", t.PromptStatus),
        ("OptAnsweredVia", t.AnsweredVia),
        ("OptActivityKind", schemas.OptActivityKind),
        ("OptTone", base.Tone),
        ("OptPanelColumnKind", base.PanelColumnKind),
        ("OptPanelActionKind", base.PanelActionKind),
        ("Side", t.Side),
    ],
)
def test_option_literals_are_mirrored_in_types_ts(name: str, literal: Any) -> None:
    assert ts_unions()[name] == set(get_args(literal))


# --- the CLI shell ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "words"),
    [
        (["options-run", "new", "--help"], ["--cash", "--confirm"]),
        (["options-check", "--help"], ["--symbol"]),
        (["options-refresh", "--help"], ["--date", "--force"]),
        (["options-postclose", "--help"], ["--date", "--force"]),
        (["options-event", "--help"], ["--due", "--date", "--force", "STRATEGY", "KEY"]),
    ],
)
def test_cli_registers_the_option_commands(argv: list[str], words: list[str]) -> None:
    result = runner.invoke(app, argv, env={"COLUMNS": "200", "NO_COLOR": "1", "TERM": "dumb"})
    assert result.exit_code == 0, result.output
    for word in words:
        assert word in result.output
    assert runner.invoke(app, ["live-run", "new", "--help"]).exit_code == 0  # the stock commands remain


@pytest.fixture
def core(db_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch) -> Iterator[Core]:
    c = test_core(db_factory, FixedClock(NIGHT))
    monkeypatch.setattr(trader.bootstrap, "build_core", lambda *a, **k: c)
    yield c


@pytest.mark.db
def test_options_run_new_needs_confirm(core: Core) -> None:
    result = runner.invoke(app, ["options-run", "new", "--cash", "5000"])
    assert result.exit_code == 0
    assert "would start a new options run with 5000 USD" in result.output and "--confirm" in result.output
    assert active_options_run_id(core.factory) is None  # nothing was changed

    done = runner.invoke(app, ["options-run", "new", "--cash", "5000", "--confirm"])
    assert done.exit_code == 0, done.output
    run_id = active_options_run_id(core.factory)
    assert run_id is not None and f"started options run {run_id}: cash 5000.0000 USD" in done.output

    again = runner.invoke(app, ["options-run", "new"])  # the default cash is the setting
    assert again.exit_code == 0 and f"would retire options run {run_id} and start" in again.output
    assert "5000 USD" in again.output and active_options_run_id(core.factory) == run_id
    bad = runner.invoke(app, ["options-run", "new", "--cash", "-1", "--confirm"])
    assert bad.exit_code == 1 and "--cash" in bad.output
    with core.factory() as s:
        assert s.execute(select(func.count()).select_from(m.Run)).scalar_one() == 1


@pytest.mark.parametrize(
    "argv",
    [
        ["options-check"],
        ["options-check", "--symbol", "SOFI"],
        ["options-refresh"],
        ["options-refresh", "--date", "2026-10-06", "--force"],
        ["options-postclose"],
        ["options-event", "wheel", "opt_daily"],
        ["options-event", "--due"],
    ],
)
def test_unwired_commands_exit_1_with_a_clear_line(argv: list[str]) -> None:
    try:
        import trader.options.runtime  # noqa: F401
    except ModuleNotFoundError:
        pass
    else:  # pragma: no cover - from T16 on the commands are wired; T16's own tests cover them
        pytest.skip("trader.options.runtime exists: the commands are wired")
    result = runner.invoke(app, argv)
    assert result.exit_code == 1
    assert result.output.strip() == f"{argv[0]}: options runtime is not wired yet"


@pytest.mark.parametrize(
    "argv",
    [
        ["options-event"],
        ["options-event", "wheel"],
        ["options-event", "wheel", "opt_daily", "--due"],
        ["options-event", "--due", "--force"],
    ],
)
def test_options_event_argument_errors_exit_2(argv: list[str]) -> None:
    result = runner.invoke(app, argv)
    assert result.exit_code == 2 and result.output.startswith("options-event:")


def test_a_bad_date_is_one_line_and_exit_1() -> None:
    result = runner.invoke(app, ["options-refresh", "--date", "tomorrow"])
    assert result.exit_code == 1 and "--date tomorrow is not a valid date" in result.output


# --- the API services ---------------------------------------------------------------------------------------


@pytest.mark.db
def test_api_services_options_defaults_to_none(db_factory: sessionmaker[Session]) -> None:
    clock = FixedClock(f.T0)
    services = make_services(test_core(db_factory, clock))  # every existing make_services call still works
    assert services.options is None
    assert [fld.name for fld in __import__("dataclasses").fields(services)][-1] == "options"
    options, fakes = make_option_services(db_factory, clock, ToyCallBuyer, run_id=7)
    wired = make_services(test_core(db_factory, clock), options=options)
    assert wired.options is options and options.run_id() == 7
    fakes.run["id"] = None
    assert options.run_id() is None
    assert options.registry.keys() == ["toy_call"] and options.settings.load().max_legs == 4
