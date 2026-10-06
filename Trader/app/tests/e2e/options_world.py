"""The scripted world of the options end-to-end tests (OPTSIM task plan T16).

Everything inside the application is real and wired by `trader.options.runtime`, each "process" built the way
the real one is built:

- the options worker: `runtime.build_worker_deps(core, stack)` and `OptionsWorker.step()`;
- the cron commands: `runtime._command(core, name, ...)`, the async body of `run_cli_command`;
- the API: `create_app` over the real `build_services` (so `ApiServices.options` comes from
  `runtime.build_api_services`), used through a signed-in `TestClient` with the CSRF header.

Only the outside is faked: Questrade (`FakeQtOptions`), FinViz (a snapshot table per ticker and a screener
answer) and Telegram (`FakeTelegramApi` behind the real `TelegramNotifier`). The clock is a `FixedClock` the
test steps by hand.

The default underlying F passes the wheel's ten tests on Tue 2026-10-06 with the numbers T11 calibrated
(tests/option_strategies/wheel/test_strategy.py): 55 a share, puts 50 / 48 / 46 of the 2026-11-20 monthly
at 1.50 / 1.10 / 0.85, and 20,000 USD of cash (a 50 strike needs 5,000 of collateral).
"""

import contextlib
import dataclasses
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from importlib.metadata import EntryPoint
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

import trader.notify.notifier as notifier_module
import trader.option_strategies.registry as registry_module
import trader.options.runtime as options_runtime
import trader.runtime as stock_runtime
from tests.fakes_api import test_core
from tests.fakes_questrade import FakeQuestrade
from tests.fakes_telegram import FakeTelegramApi
from tests.options.fakes import FakeQtOptions
from trader.api import auth
from trader.api.deps import ApiServices
from trader.api.main import create_app
from trader.api.services import build_services
from trader.bootstrap import Core
from trader.db import models as m
from trader.market.calendar import SessionCalendar
from trader.market.clock import ET, FixedClock, et_date
from trader.market.types import Candle
from trader.option_strategies.registry import ENTRY_POINT_GROUP
from trader.option_strategies.wheel import screener
from trader.option_strategies.wheel.store import PositionRow, WheelStore
from trader.options.account import start_options_run
from trader.options.runtime import OptionsRuntime
from trader.options.settings import OptionSettingsStore
from trader.options.types import OptionAccountState, OptOrderView, StructureView
from trader.options.worker import OptionsWorker, StepReport

CAL = SessionCalendar()
D = Decimal
BASE = "https://testserver"
USER = "stephen"
PASSWORD = "correct-horse-battery"  # noqa: S105 (a throwaway test password)
CHAT_ID = 4242
WHEEL_ENTRY = "trader.option_strategies.wheel.strategy:WheelStrategy"
TOY_ENTRY = "tests.options.toy_plugin:ToyCallBuyer"

DAY0 = date(2026, 10, 6)  # Tuesday
DAY1 = date(2026, 10, 7)
PUT_EXPIRY = date(2026, 11, 20)  # the November monthly, 45 days after DAY0
NEXT_EXPIRY = date(2026, 12, 18)
CALL_EXPIRY = date(2027, 1, 15)
TIME_EXIT_DAY = date(2026, 11, 2)  # 18 days before PUT_EXPIRY
CALL_DAY = date(2026, 12, 7)  # CALL_EXPIRY is 39 days out
F_QID = 9001
FIRST_BAR = date(2025, 6, 2)
LAST_BAR = date(2027, 2, 26)
TREND_ANCHOR = date(2026, 10, 5)  # the last completed session on DAY0


def et(day: date, hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute, second), tzinfo=ET).astimezone(UTC)


class _Entered:
    """An async context manager handing out one shared fake client (as `QuestradeClient` is entered)."""

    def __init__(self, client: Any) -> None:
        self.client = client
        self.entered = 0

    async def __aenter__(self) -> Any:
        self.entered += 1
        return self.client

    async def __aexit__(self, *exc: object) -> None:
        return None


class WorldQt(FakeQtOptions):
    """The fake Questrade of the world. `lag` makes every option quote that old when it is handed out
    (a stale quote); otherwise quotes are stamped with the clock's time, as the base fake does."""

    lag: timedelta = timedelta(0)

    def _stamped(self, quote: Any) -> Any:
        fresh = super()._stamped(quote)
        if not self.lag:
            return fresh
        return dataclasses.replace(fresh, fetched_at=fresh.fetched_at - self.lag)


def trend_bars(first: date = FIRST_BAR, last: date = LAST_BAR) -> list[Candle]:
    """Daily bars whose trend inputs on DAY0 are the calibrated ones: 50-day average 52 (51 twenty sessions
    earlier) and a 52-week low of 40 made 150 sessions ago. Closes rise 0.05 a session over the last 70
    sessions before DAY0 and keep rising after it."""
    days: list[date] = []
    day = first
    while day <= last:
        if CAL.is_session(day):
            days.append(day)
        day += timedelta(days=1)
    anchor = days.index(TREND_ANCHOR)
    bars = []
    for i, d in enumerate(days):
        k = i - anchor  # sessions from the anchor; 0 is the anchor itself
        close = D("49.775") + D("0.05") * max(k + 69, 0)
        low = D(40) if k == -150 else close - D("0.25")
        start = datetime.combine(d, time(0), tzinfo=ET).astimezone(UTC)
        bars.append(
            Candle(start, start + timedelta(days=1), close, close + D("0.25"), low, close, 1_000_000, close)
        )
    return bars


@dataclass
class OptionsWorld:
    """See the module text. Build it with the `world` fixture factory of each test module."""

    core: Core
    clock: FixedClock
    qt: WorldQt
    telegram: FakeTelegramApi
    run_id: int
    web_dist: Path
    ids: dict[tuple[str, date, str, str], int] = field(default_factory=dict)  # Questrade option ids
    snapshots: dict[str, dict[str, str]] = field(default_factory=dict)
    screen: list[str] = field(default_factory=list)
    _next_option_id: int = 700_000
    _worker: OptionsWorker | None = None
    _worker_stack: AsyncExitStack | None = None
    _inspect: OptionsRuntime | None = None
    _inspect_stack: AsyncExitStack | None = None
    _client: TestClient | None = None
    _csrf: str = ""
    workers_built: int = 0

    @property
    def factory(self) -> sessionmaker[Session]:
        return self.core.factory

    # --- the clock ------------------------------------------------------------------------------------------

    def go(self, day: date, hour: int, minute: int = 0, second: int = 0) -> None:
        self.clock.set(et(day, hour, minute, second))

    @property
    def today(self) -> date:
        return et_date(self.clock.now())

    # --- the fake market ------------------------------------------------------------------------------------

    def add_stock(self, ticker: str, qid: int, price: str, **details: Any) -> None:
        """A share symbol that passes the wheel's business tests, with its trend bars and FinViz table."""
        self.qt.add_symbol(
            ticker,
            qid,
            **{
                "eps": D(3),
                "pe": D(18),
                "market_cap": D(50_000_000_000),
                "industry_sector": "Technology",
                "industry_group": "Software",
                **details,
            },
        )
        self.qt.set_candles(qid, trend_bars())
        self.set_price(ticker, price)
        self.snapshots[ticker] = {
            "EPS Y/Y TTM": "10.00%",
            "Debt/Eq": "0.50",
            "Book/sh": "20.00",
            "Payout": "-",
            "Short Float": "2.00%",
            "Earnings": "Feb 10 AMC",
            "RSI (14)": "55.00",
            "Market Cap": "50.00B",
        }

    def qid(self, ticker: str) -> int:
        return self.qt.symbols[ticker].symbol_id

    def set_price(self, ticker: str, price: str) -> None:
        self.qt.set_share_quote(self.qid(ticker), price)

    def add_expiry(self, ticker: str, expiry: date, strikes: tuple[str, ...]) -> None:
        """One expiry of the chain: a call and a put at each strike (no quotes yet)."""
        rows = []
        for strike in strikes:
            call, put = self._next_option_id, self._next_option_id + 1
            self._next_option_id += 2
            self.ids[(ticker, expiry, strike, "call")] = call
            self.ids[(ticker, expiry, strike, "put")] = put
            rows.append((strike, call, put))
        self.qt.add_expiry(self.qid(ticker), expiry, rows)

    def quote(
        self,
        ticker: str,
        expiry: date,
        strike: str,
        right: str,
        bid: str,
        *,
        ask: str | None = None,
        delta: str | None = None,
        **fields: Any,
    ) -> None:
        """A live quote, five cents wide unless `ask` is given."""
        if delta is None:
            delta = "-0.30" if right == "put" else "0.30"
        self.qt.set_option_quote(
            self.ids[(ticker, expiry, strike, right)],
            bid,
            ask if ask is not None else str(D(bid) + D("0.05")),
            delta=delta,
            iv_pct="30",
            open_interest=1000,
            **fields,
        )

    def contract_id(self, ticker: str, expiry: date, strike: str, right: str) -> int:
        """`option_contracts.id` of a chain contract (the chain must have been read once)."""
        with self.factory() as s:
            return int(
                s.execute(
                    select(m.OptionContract.id).where(
                        m.OptionContract.qt_symbol_id == self.ids[(ticker, expiry, strike, right)]
                    )
                ).scalar_one()
            )

    # --- the processes --------------------------------------------------------------------------------------

    async def worker(self) -> OptionsWorker:
        """The running options worker; built on first use the way `python -m trader.options.worker` builds
        it."""
        if self._worker is None:
            self._worker_stack = AsyncExitStack()
            deps = await options_runtime.build_worker_deps(self.core, self._worker_stack)
            self._worker = OptionsWorker(deps)
            self.workers_built += 1
        return self._worker

    async def stop_worker(self) -> None:
        """Kill the worker process: everything it held in memory is gone."""
        if self._worker_stack is not None:
            await self._worker_stack.aclose()
        self._worker, self._worker_stack = None, None

    async def step(self) -> StepReport:
        return await (await self.worker()).step()

    async def run_until(self, day: date, hour: int, minute: int, *, every: int = 30) -> list[StepReport]:
        """Step the worker from now until that ET time, one step every `every` seconds."""
        end = et(day, hour, minute)
        reports = [await self.step()]
        while self.clock.now() < end:
            self.clock.advance(timedelta(seconds=every))
            reports.append(await self.step())
        return reports

    async def command(self, name: str, **kwargs: Any) -> int:
        """One cron command: the async body of `run_cli_command` (a fresh set of services, as a process)."""
        return await options_runtime._command(self.core, name, None, False, kwargs)

    async def refresh(self, day: date) -> None:
        """The 08:15 refresh of that session."""
        self.go(day, 8, 15)
        assert await self.command("refresh") == 0

    async def postclose(self, day: date, *, expect: int = 0) -> None:
        """The 16:20 post-close of that session."""
        self.go(day, 16, 20)
        assert await self.command("postclose") == expect

    async def inspect(self) -> OptionsRuntime:
        """A read-only set of services for the test's own questions (an account, the structures)."""
        if self._inspect is None:
            self._inspect_stack = AsyncExitStack()
            self._inspect = await options_runtime.build_options(self.core, self._inspect_stack)
        return self._inspect

    @property
    def api(self) -> TestClient:
        """The API process, signed in. Writes carry the CSRF header."""
        if self._client is None:
            auth.ensure_admin(self.factory, self.clock, USER, SecretStr(PASSWORD))

            async def services(stack: AsyncExitStack) -> ApiServices:
                return await build_services(self.core, stack)

            app = create_app(services_factory=services, web_dist=self.web_dist)
            self._client = TestClient(app, base_url=BASE).__enter__()
            self._login()
        return self._client

    def _login(self) -> None:
        assert self._client is not None
        r = self._client.post("/api/auth/login", json={"username": USER, "password": PASSWORD})
        assert r.status_code == 200, r.text
        self._csrf = r.json()["csrf_token"]

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        """One request; a session that ran out while the script's clock jumped weeks ahead logs in again."""
        client = self.api
        for attempt in (1, 2):
            headers = {"X-CSRF-Token": self._csrf} if method != "GET" else {}
            r = client.request(method, f"/api/options{path}", headers=headers, **kwargs)
            if r.status_code != 401 or attempt == 2:
                break
            self._login()
        return r

    def get(self, path: str, **params: Any) -> Any:
        r = self._request("GET", path, params=params)
        assert r.status_code == 200, (path, r.status_code, r.text)
        return r.json()

    def send(self, method: str, path: str, body: dict[str, Any] | None = None, *, status: int = 200) -> Any:
        r = self._request(method, path, json=body or {})
        assert r.status_code == status, (path, r.status_code, r.text)
        return r.json()

    def post(self, path: str, body: dict[str, Any] | None = None, *, status: int = 200) -> Any:
        return self.send("POST", path, body, status=status)

    async def close(self) -> None:
        if self._client is not None:
            self._client.__exit__(None, None, None)
        await self.stop_worker()
        if self._inspect_stack is not None:
            await self._inspect_stack.aclose()

    # --- the owner ------------------------------------------------------------------------------------------

    def approve(self, ticker: str = "F") -> None:
        """Add the ticker to the wheel's list and approve it, through the Strategies tab's actions."""
        added = self.post(
            "/strategies/wheel/actions", {"action": "add_ticker", "row_id": None, "value": ticker}
        )
        assert added["ok"], added
        done = self.post(
            "/strategies/wheel/actions",
            {"action": "approve", "row_id": ticker, "value": "a business I want to hold"},
        )
        assert done["ok"], done

    def ticket(self, legs: list[dict[str, Any]], **fields: Any) -> dict[str, Any]:
        """An `OptOrderIn` body for the manual ticket."""
        return {
            "underlying": "F",
            "intent": "open",
            "structure_id": None,
            "legs": legs,
            "qty": 1,
            "order_type": "market",
            "net_limit": None,
            "tif": "day",
            "walk": False,
            **fields,
        }

    def chain_ids(self, expiry: date, ticker: str = "F") -> dict[tuple[str, str], int]:
        """(strike, right) -> contract id, as the Trade tab reads them from the chain."""
        assert self.get("/chain", underlying=ticker)["expiries"]
        rows = self.get("/chain/quotes", underlying=ticker, expiry=expiry.isoformat())["rows"]
        out: dict[tuple[str, str], int] = {}
        for row in rows:
            strike = format(D(row["strike"]).normalize(), "f")  # "50.0000" -> "50"
            out[(strike, "call")] = row["call_contract_id"]
            out[(strike, "put")] = row["put_contract_id"]
        return out

    def submit(self, legs: list[dict[str, Any]], **fields: Any) -> dict[str, Any]:
        """Submit a manual ticket; the order as the API answers it (working or rejected)."""
        order: dict[str, Any] = self.post("/orders", self.ticket(legs, **fields))
        return order

    def leg(self, contract_id: int | None, side: str, effect: str = "open", ratio: int = 1) -> dict[str, Any]:
        instrument = "shares" if contract_id is None else "option"
        return {
            "instrument": instrument,
            "contract_id": contract_id,
            "side": side,
            "effect": effect,
            "ratio": ratio,
        }

    # --- reading the books ----------------------------------------------------------------------------------

    async def account(self) -> OptionAccountState:
        return await (await self.inspect()).broker.account()

    async def structures(self, *, open_only: bool = True) -> list[StructureView]:
        return await (await self.inspect()).broker.structures(open_only=open_only)

    async def structure(self, structure_id: int | None) -> StructureView:
        assert structure_id is not None
        return next(s for s in await self.structures(open_only=False) if s.id == structure_id)

    async def orders(self, status: Any = None) -> list[OptOrderView]:
        return await (await self.inspect()).broker.orders(status=status)

    def ledger_total(self) -> Decimal:
        with self.factory() as s:
            total = s.execute(
                select(func.coalesce(func.sum(m.CashLedger.amount), 0)).where(
                    m.CashLedger.run_id == self.run_id
                )
            ).scalar_one()
        return D(total)

    def wheel(self) -> WheelStore:
        return WheelStore(self.factory, self.clock, self.run_id)

    def wheel_position(self, position_id: int | None = None) -> PositionRow:
        store = self.wheel()
        if position_id is None:
            (only,) = store.open_positions()
            return only
        found = store.position(position_id)
        assert found is not None
        return found

    def wheel_actions(self) -> list[str]:
        return [e.action for e in self.wheel().events() if e.action is not None]

    def count(self, model: Any, *where: Any) -> int:
        with self.factory() as s:
            return int(s.execute(select(func.count()).select_from(model).where(*where)).scalar_one())

    def messages(self) -> list[str]:
        """Every text sent to Telegram, oldest first."""
        return [call["text"] for call in self.telegram.calls_of("send_message")]

    def errors(self) -> list[str]:
        """The run's `error` and `critical` events (a failed hook or worker part is one of these)."""
        with self.factory() as s:
            rows = s.execute(
                select(m.EventLog).where(m.EventLog.level.in_(("error", "critical"))).order_by(m.EventLog.id)
            ).scalars()
            return [f"{row.source}: {row.message} {row.data}" for row in rows]

    async def assert_reconciled(self) -> OptionAccountState:
        """Feature acceptance item 4, checked against the tables and the fake quotes, not the broker's own
        arithmetic: cash is the ledger total; reserved is the open structures' reserves plus the working
        orders' reservations; free cash is cash less reserved; the account value is cash plus the
        liquidation value of every open position (a long at its bid, a short at its ask, shares at the
        bid); and no share covers more than one short call."""
        account = await self.account()
        structures = await self.structures()
        working = await self.orders("working")
        assert account.cash == self.ledger_total()
        reserved = sum((s.reserved_cash for s in structures), D(0)) + sum(
            (o.reserved_cash for o in working), D(0)
        )
        assert account.reserved == reserved
        assert account.free_cash == account.cash - account.reserved
        value = D(0)
        for st in structures:
            for p in st.positions:
                if p.qty == 0:
                    continue
                if p.contract is None:
                    share = self.qt.share_quotes[self.qid(p.underlying)]
                    assert share.bid is not None
                    value += share.bid * p.qty
                    continue
                q = self.qt.option_quote_rows[p.contract.qt_symbol_id]
                price = q.bid if p.qty > 0 else q.ask
                assert price is not None
                value += price * p.qty * p.contract.multiplier
        assert account.marks_complete
        assert account.positions_value == value
        assert account.equity == account.cash + value
        by_id = {s.id: s for s in structures}
        claimed: dict[int, int] = {}
        for st in structures:
            short_calls = sum(
                -p.qty * p.contract.multiplier
                for p in st.positions
                if p.contract is not None and p.contract.right == "call" and p.qty < 0
            )
            own = sum(p.qty for p in st.positions if p.contract is None)
            longs = sum(
                p.qty * p.contract.multiplier
                for p in st.positions
                if p.contract is not None and p.contract.right == "call" and p.qty > 0
            )
            need = max(short_calls - own - longs, 0)
            if need:
                assert st.cover_structure_id is not None, f"structure {st.id} has an uncovered short call"
                cover = by_id[st.cover_structure_id]
                assert cover.source == st.source and cover.kind == "shares"
                claimed[cover.id] = claimed.get(cover.id, 0) + need
        for cover_id, shares in claimed.items():
            held = sum(p.qty for p in by_id[cover_id].positions if p.contract is None)
            assert shares <= held, f"shares structure {cover_id} covers {shares} with {held}"
        return account


def patch_outside(monkeypatch: pytest.MonkeyPatch, world_parts: dict[str, Any]) -> None:
    """Replace the three outside services with the fakes, at the builders the runtime calls (`monkeypatch`
    undoes it at the end of the test)."""
    qt: FakeQtOptions = world_parts["qt"]
    monkeypatch.setattr(options_runtime, "questrade_client", lambda core: _Entered(qt))
    monkeypatch.setattr(
        options_runtime,
        "finviz_snapshots",
        lambda core, stack: lambda ticker, today: dict(world_parts["snapshots"][ticker]),
    )
    monkeypatch.setattr(screener, "source", lambda filters: list(world_parts["screen"]))
    monkeypatch.setattr(stock_runtime, "build_telegram_api", lambda env: world_parts["telegram"])
    monkeypatch.setattr(stock_runtime, "questrade_client", lambda core: _Entered(FakeQuestrade()))
    # The log mirror is a thread writing `log.<process>` rows: the tests read event_log themselves.
    monkeypatch.setattr(stock_runtime, "install_log_mirror", lambda *args, **kwargs: None)
    # The notifier spaces its sends by the clock; a stepped clock would make it sleep for real.
    monkeypatch.setattr(notifier_module, "MIN_SEND_INTERVAL", 0.0)


def install_plugins(monkeypatch: pytest.MonkeyPatch, *pairs: tuple[str, str]) -> None:
    """The option plug-ins as entry points: what `pip install` of a plug-in package declares."""
    eps = [EntryPoint(name=name, value=value, group=ENTRY_POINT_GROUP) for name, value in pairs]
    monkeypatch.setattr(registry_module, "entry_points", lambda group: [e for e in eps if e.group == group])


async def make_world(
    db_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    cash: int = 20_000,
    plugins: tuple[tuple[str, str], ...] = (("wheel", WHEEL_ENTRY),),
    wheel_params: dict[str, Any] | None = None,
    max_position_pct: str | None = None,
) -> OptionsWorld:
    """The world at 08:00 ET on DAY0 with an options run of `cash` USD, the F chain, and the benchmark set
    to F (so the morning refresh has one underlying to keep)."""
    clock = FixedClock(et(DAY0, 8, 0))
    core = test_core(
        db_factory,
        clock,
        telegram_bot_token=SecretStr("123456:TEST-TOKEN-NOT-REAL"),
        telegram_chat_id=CHAT_ID,
        admin_username=USER,
        admin_password_initial=SecretStr(PASSWORD),
    )
    parts: dict[str, Any] = {
        "qt": WorldQt(clock),
        "telegram": FakeTelegramApi(),
        "snapshots": {},
        "screen": [],
    }
    patch_outside(monkeypatch, parts)
    install_plugins(monkeypatch, *plugins)
    dist = tmp_path / "dist"
    dist.mkdir(exist_ok=True)
    (dist / "index.html").write_text("<!doctype html><div id='root'></div>")

    settings = OptionSettingsStore(db_factory, clock.now)
    settings.set("options.benchmark_ticker", "F", "test")
    if max_position_pct is not None:
        settings.set("options.max_position_pct", max_position_pct, "test")
    run = start_options_run(db_factory, clock, CAL, settings.load(), cash=D(cash), actor="cli:options-run")
    world = OptionsWorld(core, clock, parts["qt"], parts["telegram"], run.run_id, dist)
    world.snapshots = parts["snapshots"]
    world.screen = parts["screen"]

    world.add_stock("F", F_QID, "55")
    world.add_expiry("F", PUT_EXPIRY, ("46", "48", "50", "52"))
    world.add_expiry("F", NEXT_EXPIRY, ("46", "48", "50", "52"))
    world.add_expiry("F", CALL_EXPIRY, ("48", "50", "52"))
    for strike, delta, bid in (("50", "-0.30", "1.50"), ("48", "-0.25", "1.10"), ("46", "-0.20", "0.85")):
        world.quote("F", PUT_EXPIRY, strike, "put", bid, delta=delta)

    if wheel_params:
        rt = await world.inspect()
        await rt.host.ensure_defaults()
        rt.registry.update("wheel", params=wheel_params, actor="test")
    return world


@contextlib.asynccontextmanager
async def open_world(
    db_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **options: Any
) -> AsyncIterator[OptionsWorld]:
    world = await make_world(db_factory, monkeypatch, tmp_path, **options)
    try:
        yield world
    finally:
        await world.close()
