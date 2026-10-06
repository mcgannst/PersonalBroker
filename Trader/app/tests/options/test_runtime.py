"""OPTSIM T16: `trader.options.runtime`, the composition root of the options processes. The network is
faked at the runtime's own builders (Questrade, FinViz) and at the stock Telegram builder; everything else
is the real thing over the test database."""

import asyncio
import json
from collections.abc import Iterator
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker
from structlog.testing import capture_logs
from typer.testing import CliRunner

import trader.options.runtime as runtime
import trader.runtime as stock_runtime
from tests.e2e import options_world as w
from tests.fakes_api import test_core
from tests.fakes_telegram import FakeTelegramApi
from trader import bootstrap
from trader.adapters.questrade.client import QuestradeClient
from trader.adapters.telegram.callbacks import DbCallbackIssuer
from trader.bootstrap import Core
from trader.cli import app
from trader.db import models as m
from trader.market.clock import FixedClock
from trader.notify.notifier import NullNotifier, TelegramNotifier
from trader.option_strategies.host import DefaultStrategyHost, NoOptionsRun, job_name
from trader.option_strategies.registry import OptionStrategyRegistry
from trader.options.account import start_options_run
from trader.options.facts import FactsService
from trader.options.market import OptionMarketService
from trader.options.messages import OptionMessages
from trader.options.prompts import DbPromptStore, PromptSender
from trader.options.protocols import OptionApiServices
from trader.options.settings import OptionSettings, OptionSettingsStore
from trader.options.worker import HEARTBEAT_PROCESS, OptionsWorker

pytestmark = pytest.mark.db
D = Decimal
SATURDAY = date(2026, 10, 10)
FRIDAY = date(2026, 10, 9)


@dataclass
class Env:
    core: Core
    clock: FixedClock
    qt: w.WorldQt
    telegram: FakeTelegramApi
    parts: dict[str, Any]

    def start_run(self, cash: int = 5000) -> int:
        f = self.core.factory
        settings = OptionSettingsStore(f, self.clock.now).load()
        return start_options_run(f, self.clock, w.CAL, settings, cash=D(cash), actor="test").run_id


@pytest.fixture
def env(db_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch) -> Iterator[Env]:
    """08:00 ET on Tue 2026-10-06, Telegram configured, no options run yet; `trader` commands get this
    core."""
    clock = FixedClock(w.et(w.DAY0, 8, 0))
    core = test_core(
        db_factory,
        clock,
        telegram_bot_token=SecretStr("123456:TEST-TOKEN-NOT-REAL"),
        telegram_chat_id=w.CHAT_ID,
    )
    parts: dict[str, Any] = {
        "qt": w.WorldQt(clock),
        "telegram": FakeTelegramApi(),
        "snapshots": {},
        "screen": [],
    }
    w.patch_outside(monkeypatch, parts)
    w.install_plugins(monkeypatch, ("wheel", w.WHEEL_ENTRY))
    monkeypatch.setattr(bootstrap, "build_core", lambda: core)
    made = Env(core, clock, parts["qt"], parts["telegram"], parts)
    made.qt.add_symbol("F", w.F_QID, eps=D(3), market_cap=D(50_000_000_000), industry_sector="Technology")
    made.qt.set_share_quote(w.F_QID, "55")
    made.qt.set_candles(w.F_QID, w.trend_bars())
    made.qt.add_expiry(w.F_QID, w.PUT_EXPIRY, [("50", 700_001, 700_002)])
    made.qt.set_option_quote(700_002, "1.50", "1.55", delta="-0.30", iv_pct="30")
    parts["snapshots"]["F"] = {"Debt/Eq": "0.50", "RSI (14)": "55.00"}
    # the benchmark is always in the refresh set: F is the one symbol this fake Questrade knows
    OptionSettingsStore(db_factory, clock.now).set("options.benchmark_ticker", "F", "test")
    yield made


def job_rows(env: Env) -> list[tuple[str, date, str]]:
    with env.core.factory() as s:
        rows = s.execute(select(m.JobRun.job, m.JobRun.session_date, m.JobRun.status).order_by(m.JobRun.id))
        return [(job, day, status) for job, day, status in rows]


# --- the composition ----------------------------------------------------------------------------------------


async def test_runtime_builds_everything(env: Env) -> None:
    async with AsyncExitStack() as stack:
        rt = await runtime.build_options(env.core, stack)
        assert rt.client is env.qt
        assert isinstance(rt.market, OptionMarketService) and isinstance(rt.facts, FactsService)
        assert not isinstance(rt.market, runtime.OffLoopOptionMarket)
        assert isinstance(rt.broker, runtime.RunBoundBroker)
        assert isinstance(rt.registry, OptionStrategyRegistry) and rt.registry.keys() == ["wheel"]
        assert isinstance(rt.host, DefaultStrategyHost)
        assert isinstance(rt.prompts, DbPromptStore) and isinstance(rt.sender, PromptSender)
        assert (
            isinstance(rt.renderer, OptionMessages) and rt.renderer.base_url == env.core.env.public_base_url
        )
        # Telegram is configured: a direct notifier, and prompt buttons signed like the stock bot's
        assert isinstance(rt.notifier, TelegramNotifier) and rt.notifier.api is env.telegram
        assert isinstance(rt.sender.issuer, DbCallbackIssuer) and rt.sender.chat_id == w.CHAT_ID
        assert rt.sender.issuer.signer.data("o", "1", "y", "n") == stock_runtime.build_signer(env.core).data(
            "o", "1", "y", "n"
        )
        # the market reads its facts from the facts service, which reads its bars through that market
        assert await rt.market.facts("F") is None
        assert set(await rt.facts.refresh(["F"])) == {"F"}
        facts = await rt.market.facts("F")
        assert facts is not None and (facts.sma50, facts.debt_to_equity) == (D(52), D("0.5"))
        # no options run: the broker has nothing to act on, and says so
        assert rt.run_id() is None
        with pytest.raises(NoOptionsRun):
            await rt.broker.account()
        # the broker follows the active run without a rebuild (the API process after `options-run new`)
        first = env.start_run(5000)
        assert rt.run_id() == first and (await rt.broker.account()).cash == D(5000)
        second = env.start_run(7000)
        assert second != first and (await rt.broker.account()).cash == D(7000)
        jobs = runtime.postclose_deps(rt), runtime.refresh_deps(rt)
        assert all(d.broker is rt.broker and d.market is rt.market and d.host is rt.host for d in jobs)
        assert jobs[0].lifecycle is None and jobs[1].facts is rt.facts  # the jobs build the engine per run


def test_each_options_process_paces_questrade_at_two_requests_a_second(
    db_factory: sessionmaker[Session],
) -> None:
    """Risk R9: the stock processes pace at 17 a second; an options process takes 2 of the shared limit."""
    core = test_core(db_factory, FixedClock(w.et(w.DAY0, 8, 0)))
    built = runtime.questrade_client(core)  # the real builder (this test does not use the `env` fixture)
    assert isinstance(built, QuestradeClient)
    assert runtime.OPTION_MARKET_RPS == 2.0
    assert built._buckets["market"]._interval == 0.5
    stock = stock_runtime.questrade_client(core)
    assert isinstance(stock, QuestradeClient) and stock._buckets["market"]._interval < 0.1


def test_usd_cad_is_the_stock_rate_inverted_and_survives_an_unreadable_row(env: Env) -> None:
    rate = runtime.UsdCadRate(env.core)
    assert rate() == D("1.388889")  # 1 / 0.72, the default `fx.cad_usd_rate`
    env.core.settings.set("fx.cad_usd_rate", "0.8", "test")
    assert rate() == D("1.25")
    with env.core.factory() as s:
        s.execute(update(m.Setting).where(m.Setting.key == "fx.cad_usd_rate").values(value="not a number"))
        s.commit()
    with capture_logs() as logs:
        assert rate() == D("1.25") and rate() == D("1.25")  # the last good rate, one warning
    assert [e["event"] for e in logs].count("options.fx_rate_unreadable") == 1


def test_finviz_is_opened_on_the_first_snapshot_only(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[dict[str, Any]] = []

    class Scraper:
        def __init__(self, **kwargs: Any) -> None:
            opened.append(kwargs)
            self.closed = False

        def __enter__(self) -> "Scraper":
            return self

        def __exit__(self, *exc: object) -> None:
            self.closed = True

        def snapshot(self, ticker: str, today: date) -> dict[str, str]:
            return {"ticker": ticker}

    monkeypatch.setattr(runtime, "FinvizScraper", Scraper)
    monkeypatch.setenv(stock_runtime.FINVIZ_CACHE_ENV, "/nonexistent/finviz-cache")
    snapshots = runtime.LazySnapshots(env.core)
    assert opened == []
    assert snapshots("F", w.DAY0) == {"ticker": "F"} and snapshots("G", w.DAY0) == {"ticker": "G"}
    assert len(opened) == 1 and opened[0]["cache_dir"] == Path("/nonexistent/finviz-cache")
    scraper = snapshots._scraper
    snapshots.close()
    assert scraper is not None and scraper.closed and snapshots._scraper is None
    snapshots.close()  # closing twice is fine


# --- the worker ---------------------------------------------------------------------------------------------


async def test_worker_deps_without_an_options_run_idle_and_then_follow_a_new_run(env: Env) -> None:
    async with AsyncExitStack() as stack:
        deps = await runtime.build_worker_deps(env.core, stack)
        assert deps.run_id is None and deps.engine is env.core.engine
        worker = OptionsWorker(deps)
        env.clock.set(w.et(w.DAY0, 10, 0))
        report = await worker.step()  # in session hours, but there is nothing to trade for
        assert (report.phase, report.parts) == ("idle", ("ensure_defaults",))
        with env.core.factory() as s:
            beat = s.execute(select(m.WorkerHeartbeat)).scalar_one()
            assert (beat.process, beat.detail["run_id"]) == (HEARTBEAT_PROCESS, None)
            assert s.execute(select(m.OptionStrategyConfig.strategy_key)).scalars().all() == ["wheel"]
        # a run appears: this worker stops for a restart (exit 4); it never trades on services built for none
        env.clock.set(w.et(w.DAY0, 17, 0))
        run_id = env.start_run()
        stop = asyncio.Event()
        with pytest.raises(SystemExit) as exit_info:
            await worker.run(stop)
        assert exit_info.value.code == 4
    async with AsyncExitStack() as stack:
        deps = await runtime.build_worker_deps(env.core, stack)
        assert deps.run_id == run_id
        assert (await deps.broker.account()).cash == D(5000)
        assert await deps.host.due_events(w.DAY0, env.clock.now()) != []  # the wheel's daily event, unfired


# --- the API ------------------------------------------------------------------------------------------------


async def test_api_services_are_built_off_the_loop_and_a_failure_gives_none(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with AsyncExitStack() as stack:
        services = await runtime.build_api_services(env.core, stack, notifier=NullNotifier())
        assert isinstance(services, OptionApiServices)
        assert isinstance(services.market, runtime.OffLoopOptionMarket)
        assert services.run_id() is None
        assert services.registry.current("wheel").enabled is True  # the first settings row was written
        assert isinstance(services.settings.load(), OptionSettings)

    def broken(core: Core) -> Any:
        raise RuntimeError("no client for you postgresql+psycopg://user:hunter2@db/x")

    monkeypatch.setattr(runtime, "questrade_client", broken)
    with capture_logs() as logs:
        async with AsyncExitStack() as stack:
            assert await runtime.build_api_services(env.core, stack) is None
    (line,) = [e for e in logs if e["event"] == "options.api_services_not_built"]
    assert line["error_type"] == "RuntimeError" and "hunter2" not in json.dumps(line)


# --- the command line ---------------------------------------------------------------------------------------


def test_cli_commands_dispatch(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    def run(name: str, **kwargs: Any) -> tuple[int, str, str]:
        code = runtime.run_cli_command(
            name, session_date=kwargs.pop("session_date", None), force=False, **kwargs
        )
        out = capsys.readouterr()
        return code, out.out, out.err

    code, _, err = run("nonsense")
    assert code == 2 and "unknown options command" in err

    # check: read-only, JSON on stdout, exit 0 only when the chain, a quote and the details came back
    code, out, _ = run("check", symbol="f")
    report = json.loads(out)
    assert code == 0 and (report["symbol"], report["ok"], report["real_time"]) == ("F", True, True)
    assert report["put"]["bid"] == "1.50" and report["expiries"] == 1
    code, out, _ = run("check", symbol="NOPE")
    assert code == 1 and json.loads(out)["error"] == "unknown symbol NOPE"

    # without an options run the jobs skip, and write no job row
    assert run("refresh") == (0, "options-refresh 2026-10-06: skipped (no options run)\n", "")
    assert run("postclose") == (0, "options-postclose 2026-10-06: skipped (the session has not closed)\n", "")
    code, out, _ = run("event", strategy="wheel", key="opt_daily", due=False)
    assert code == 0 and "skipped: there is no options run" in out
    assert job_rows(env) == []

    env.start_run()
    code, out, _ = run("refresh")
    assert code == 0 and out.startswith('options-refresh 2026-10-06: succeeded {"underlyings": 1, "ok": 1')
    assert run("refresh") == (0, "options-refresh 2026-10-06: skipped (already succeeded)\n", "")
    assert run("refresh", session_date=SATURDAY) == (
        0,
        "options-refresh 2026-10-10: skipped (not a session)\n",
        "",
    )
    env.clock.set(w.et(w.DAY0, 16, 20))
    code, out, _ = run("postclose")
    assert code == 0 and out.startswith("options-postclose 2026-10-06: succeeded")
    assert env.telegram.calls_of("send_message")[-1]["text"].startswith(
        "<b>Options account value $5,000.00</b>"
    )

    # events: every due one, one by name, and the usage errors
    env.clock.set(w.et(date(2026, 10, 7), 10, 35))
    code, out, _ = run("event", strategy=None, key=None, due=True)
    assert code == 0 and "wheel opt_daily 2026-10-07: succeeded" in out
    code, out, _ = run("event", strategy=None, key=None, due=True)
    assert (code, out) == (0, "options-event: nothing is due for 2026-10-07\n")
    code, out, _ = run("event", strategy="nobody", key="opt_daily", due=False)
    assert code == 2 and "unknown strategy" in out
    code, out, _ = run("event", strategy="wheel", key="Bad Key", due=False)
    assert code == 2
    assert [(job, status) for job, _, status in job_rows(env)] == [
        ("options_refresh", "succeeded"),
        ("options_postclose", "succeeded"),
        (job_name("wheel", "postclose"), "succeeded"),
        (job_name("wheel", "opt_daily"), "succeeded"),
    ]


def test_the_saturday_screen_runs_for_the_latest_session(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    """`0 8 * * 6 trader options-event wheel screen`: Saturday is not a session, so with no --date the
    event runs for Friday, the date the deploy catch-up tool pins it to; an explicit --date is kept."""
    env.start_run()
    env.parts["screen"].append("F")
    env.clock.set(w.et(SATURDAY, 8, 0))
    assert runtime.default_session_date(env.core, "event", {"strategy": "wheel", "key": "screen"}) == FRIDAY
    assert runtime.default_session_date(env.core, "event", {"due": True}) == SATURDAY
    assert runtime.default_session_date(env.core, "refresh", {}) == SATURDAY

    result = CliRunner().invoke(app, ["options-event", "wheel", "screen"])
    assert result.exit_code == 0, result.output
    assert "wheel screen 2026-10-09: succeeded" in result.output
    assert job_rows(env) == [(job_name("wheel", "screen"), FRIDAY, "succeeded")]
    with env.core.factory() as s:  # the screen's candidate reached the wheel's list
        assert s.execute(select(m.WheelTicker.ticker, m.WheelTicker.origin)).all() == [("F", "screen")]

    result = CliRunner().invoke(app, ["options-event", "wheel", "screen", "--date", "2026-10-08"])
    assert result.exit_code == 0 and "wheel screen 2026-10-08: succeeded" in result.output
    capsys.readouterr()


def test_a_command_that_cannot_start_is_one_masked_line(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def no_core() -> Core:
        raise RuntimeError("could not connect to postgresql+psycopg://trader:hunter2@db:5432/trader")

    monkeypatch.setattr(bootstrap, "build_core", no_core)
    assert runtime.run_cli_command("refresh", session_date=None, force=False) == 1
    err = capsys.readouterr().err
    assert err.startswith("options-refresh: failed: RuntimeError") and "hunter2" not in err
    assert len(err.splitlines()) == 1

    result = CliRunner().invoke(app, ["options-postclose"])
    assert result.exit_code == 1 and "hunter2" not in result.output and "Traceback" not in result.output
