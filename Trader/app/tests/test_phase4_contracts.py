"""P4-T1 acceptance tests 4, 5 and 9: the Phase 4 backend contracts that T3-T11, T17 and T18 build against.

The typed assignments in `_structural_checks` are what mypy verifies (run mypy on this file); the runtime
tests below check the same things by name and signature, and pin every public name T1 created, so a
builder who renames a contract breaks this test. The owning tasks replace the stubs' bodies, never their
names.
"""

import asyncio
import dataclasses
import importlib
import inspect
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastapi import APIRouter
from sqlalchemy.orm import Session, sessionmaker

from tests.api.conftest import DEFAULT_USER, make_client
from tests.factories import add_strategy_config, add_symbol
from tests.fakes_api import (
    FakeCredentialStore,
    FakeFeed,
    FakeJobLauncher,
    RecordingDeciderFor,
    fake_candles,
    fake_quotes,
    make_services,
    test_core,
)
from tests.fakes_telegram import RecordingNotifier
from trader.adapters.questrade.auth import QuestradeAuth
from trader.api import deps, schemas
from trader.api.deps import ApiServices, AuthUser, ChangeFeed, CredentialStore, FeedMessage, JobLauncher
from trader.api.errors import ApiError
from trader.api.feed import PollingChangeFeed
from trader.api.launcher import SubprocessJobLauncher
from trader.api.routers import ROUTERS
from trader.db import models as m
from trader.engine.runs import get_live_run
from trader.market.clock import FixedClock
from trader.market.types import Candle
from trader.notify import views as nviews
from trader.notify.types import PositionLine, ProposalView
from trader.settings_store import RuntimeSettings

T0 = datetime(2026, 10, 6, 13, 35, 5, tzinfo=UTC)
DAY = date(2026, 10, 6)

STUB_MODULES: dict[str, set[str]] = {
    "trader.api": set(),
    "trader.api.errors": {"ApiError", "install_error_handlers"},
    "trader.api.deps": {
        "AuthUser",
        "actor",
        "CredentialStore",
        "JobLauncher",
        "ChangeFeed",
        "FeedMessage",
        "CandleSource",
        "ApiServices",
        "get_services",
        "current_user",
        "require_csrf",
        "live_run_id",
        "resolve_run",
        "Services",
        "CurrentUser",
        "CsrfUser",
    },
    "trader.api.views": {"token_out", "worker_out", "event_out", "killswitch_states"},
    "trader.api.routers": {"ROUTERS"},
    "trader.api.main": {"create_app", "web_dist_dir", "SECURITY_HEADERS"},
    "trader.api.__main__": {"main"},
    "trader.api.auth": {
        "COOKIE_NAME",
        "CSRF_HEADER",
        "MIN_PASSWORD_CHARS",
        "USERNAME_PATTERN",
        "hash_password",
        "verify_password",
        "SessionSigner",
        "LoginLimiter",
        "login",
        "logout",
        "authenticate",
        "check_csrf",
        "change_password",
        "totp_setup",
        "totp_confirm",
        "totp_disable",
        "ensure_admin",
    },
    "trader.api.quotes": {"CachedQuotes"},
    "trader.api.forms": {"field_out", "model_fields_out", "SETTING_GROUPS", "group_of"},
    "trader.api.launcher": {"CLI_ARGS", "SubprocessJobLauncher"},
    "trader.api.feed": {"WATERMARK_TOPICS", "PollingChangeFeed", "watermarks"},
    "trader.api.services": {"build_services"},
    "trader.api.routers.meta": {"router"},
    "trader.api.routers.auth": {"router"},
    "trader.api.routers.dashboard": {"router", "DAY_JOBS", "DayJob", "build_timeline"},
    "trader.api.routers.trading": {"router"},
    "trader.api.routers.proposals": {"router"},
    "trader.api.routers.killswitch": {"router"},
    "trader.api.routers.performance": {"router"},
    "trader.api.routers.journal": {"router"},
    "trader.api.routers.settings": {"router"},
    "trader.api.routers.strategies": {"router"},
    "trader.api.routers.system": {"router"},
    "trader.api.routers.jobs": {"router"},
    "trader.api.routers.credentials": {"router"},
    "trader.api.routers.watchlist": {"router"},
    "trader.api.routers.stream": {"router", "MAX_STREAMS", "KEEPALIVE_SECONDS", "SESSION_RECHECK_SECONDS"},
    "trader.market.watchlist": {
        "MAX_TICKERS",
        "MAX_BYTES",
        "ParsedWatchlist",
        "parse_watchlist_csv",
        "store_watchlist",
        "get_watchlist",
        "delete_watchlist",
    },
    "trader.reports": set(),
    "trader.reports.export": {"TRADE_CSV_COLUMNS", "trades_csv"},
}

ROUTER_ORDER = [
    "meta",
    "auth",
    "dashboard",
    "trading",
    "proposals",
    "killswitch",
    "performance",
    "journal",
    "settings",
    "strategies",
    "system",
    "jobs",
    "credentials",
    "watchlist",
    "replays",  # P5-T1
    "reports",  # P5-T1
    "decisions",  # P6-T12
    "live",  # DB-T1
    "control",  # DB-T1
    "stream",
]

SCHEMA_MODELS = {
    "FieldError",
    "ErrorBody",
    "ErrorOut",
    "OkOut",
    "LoginIn",
    "UserOut",
    "SessionOut",
    "PasswordChangeIn",
    "TotpSetupIn",
    "TotpSetupOut",
    "TotpConfirmIn",
    "TotpDisableIn",
    "HealthOut",
    "MetaOut",
    "TokenOut",
    "WorkerOut",
    "EventOut",
    "KillSwitchOut",
    "KillSwitchEventOut",
    "ProposalOut",
    "PositionOut",
    "OrderOut",
    "FillOut",
    "TradeOut",
    "SignalOut",
    "CandleOut",
    "PositionDetailOut",
    "CandidateOut",
    "HeadlineOut",
    "CatalystOut",
    "CandidatesOut",
    "SessionInfoOut",
    "TimelineItemOut",
    "PnlOut",
    "DashboardOut",
    "DecisionOut",
    "KillSwitchesOut",
    "ResetIn",
    "HistogramBinOut",
    "MetricsOut",
    "EquityPointOut",
    "EquityOut",
    "JournalDayOut",
    "JournalIn",
    "FieldOut",
    "SettingOut",
    "SettingsOut",
    "SettingIn",
    "StrategyOut",
    "StrategyIn",
    "JobRunOut",
    "JobRunIn",
    "JobLaunchOut",
    "NotificationOut",
    "SystemOut",
    "CredentialIn",
    "TelegramTestOut",
    "WatchlistOut",
    "RejectedRowOut",
    "WatchlistUploadOut",
    "StreamHello",
    "StreamInvalidate",
    "StreamEvents",
    "Items",
}


@pytest.mark.parametrize("module", sorted(STUB_MODULES))
def test_every_contract_module_imports_with_its_names(module: str) -> None:
    mod = importlib.import_module(module)
    missing = sorted(name for name in STUB_MODULES[module] if not hasattr(mod, name))
    assert missing == []


def test_routers_are_the_seventeen_in_order() -> None:
    assert (
        len(ROUTERS) == 20
    )  # Phase 5 added replays and reports (P5-T1), P6-T12 decisions, DB-T1 live/control
    assert all(isinstance(r, APIRouter) for r in ROUTERS)
    for name, router in zip(ROUTER_ORDER, ROUTERS, strict=True):
        assert router is importlib.import_module(f"trader.api.routers.{name}").router
        assert router.tags == [name]


def test_schema_models_and_literals() -> None:
    missing = sorted(name for name in SCHEMA_MODELS if not hasattr(schemas, name))
    assert missing == []
    assert schemas.Topic.__args__ == (  # type: ignore[attr-defined]
        "proposals",
        "orders",
        "fills",
        "positions",
        "trades",
        "candidates",
        "killswitch",
        "events",
        "journal",
        "jobs",
        "settings",
        "strategies",
        "system",
        "replays",  # P5-T1
        "reports",  # P5-T1
        "marks",  # DB-T1
        "activity",  # DB-T1
    )
    assert schemas.ManualJob.__args__ == (  # type: ignore[attr-defined]
        "nightly",
        "premarket",
        "preopen",
        "postclose",
        "token-refresh",
        "weekly",  # P5-T1
    )
    assert set(schemas.TimelineStatus.__args__) == {  # type: ignore[attr-defined]
        "done",
        "failed",
        "missed",
        "running",
        "skipped",
        "next",
        "upcoming",
    }
    assert len(schemas.FieldKind.__args__) == 8  # type: ignore[attr-defined]


def test_models_are_frozen() -> None:
    out = schemas.OkOut()
    assert out.ok is True and out.message is None
    with pytest.raises(Exception, match="frozen"):
        out.ok = False  # type: ignore[misc]


def test_items_is_generic() -> None:
    items = schemas.Items[schemas.OkOut](items=[schemas.OkOut(message="a")])
    assert items.model_dump(mode="json") == {"items": [{"ok": True, "message": "a"}]}


# --- test 4: ProposalView.decided_at ------------------------------------------------------------------------


def _view(**over: Any) -> ProposalView:
    base: dict[str, Any] = {
        "proposal_id": 1,
        "kind": "entry",
        "status": "pending",
        "ticker": "AAA",
        "side": "buy",
        "order_type": "stop",
        "qty": 33,
        "stop": Decimal("21.5608"),
        "limit": None,
        "stop_loss": Decimal("21.41"),
        "risk_usd": Decimal("14.40"),
        "reason": "ORB long",
        "strategy_key": "orb_sip",
        "created_at": T0,
        "expires_at": T0 + timedelta(minutes=5),
        "decided_via": None,
        "error": None,
    }
    return ProposalView(**{**base, **over})


def test_proposal_view_decided_at_is_the_last_field_and_defaults_to_none() -> None:
    assert [f.name for f in dataclasses.fields(ProposalView)][-1] == "decided_at"
    assert _view().decided_at is None


@pytest.mark.db
def test_proposal_view_of_a_decided_proposal_carries_decided_at(db_factory: sessionmaker[Session]) -> None:
    run = get_live_run(db_factory, FixedClock(T0), RuntimeSettings())
    decided = T0 + timedelta(seconds=65)
    with db_factory() as s:
        sym = add_symbol(s, "AAA")
        cfg = add_strategy_config(s, "orb_sip")
        sig = m.Signal(
            run_id=run.id,
            strategy_config_id=cfg,
            symbol_id=sym,
            session_date=DAY,
            event_key="orb_open",
            ts=T0,
            intent={"reason": "ORB long"},
            evidence={},
        )
        s.add(sig)
        s.flush()
        rows = []
        for status, at in (("submitted", decided), ("pending", None)):
            p = m.Proposal(
                run_id=run.id,
                signal_id=sig.id,
                kind="entry",
                order_spec={"symbol_id": sym, "side": "buy", "order_type": "stop", "stop": "21.55"},
                qty=33,
                status=status,
                created_at=T0,
                expires_at=T0 + timedelta(minutes=5),
                decided_at=at,
                decided_via="web" if at else None,
                decided_by="web:stephen" if at else None,
                escalations=0,
            )
            s.add(p)
            rows.append(p)
        s.flush()
        done, pending = (nviews.proposal_view(s, p) for p in rows)
    assert done.decided_at == decided and done.decided_via == "web"
    assert pending.decided_at is None


# --- test 5: pure mappings and Decimal on the wire ----------------------------------------------------------


def test_proposal_out_from_view_copies_every_field() -> None:
    v = _view(status="submitted", decided_via="web", decided_at=T0 + timedelta(seconds=30), error="x")
    p = m.Proposal(
        id=1,
        decided_by="web:stephen",
        decision_latency_ms=30000,
        order_id=9,
        position_id=4,
        decided_at=T0 + timedelta(seconds=30),
    )
    out = schemas.ProposalOut.from_view(v, p)
    assert out.id == v.proposal_id
    for name in (
        "kind",
        "status",
        "ticker",
        "side",
        "order_type",
        "qty",
        "stop",
        "limit",
        "stop_loss",
        "risk_usd",
        "reason",
        "strategy_key",
        "created_at",
        "expires_at",
        "decided_at",
        "decided_via",
        "error",
    ):
        assert getattr(out, name) == getattr(v, name), name
    assert (out.decided_by, out.decision_latency_ms, out.order_id, out.position_id) == (
        "web:stephen",
        30000,
        9,
        4,
    )
    assert set(schemas.ProposalOut.model_fields) == {
        "id",
        *(f.name for f in dataclasses.fields(ProposalView) if f.name != "proposal_id"),
        "decided_by",
        "decision_latency_ms",
        "order_id",
        "position_id",
    }
    dumped = out.model_dump(mode="json")
    assert dumped["stop"] == "21.5608"  # Decimal as a JSON string, never a float
    assert dumped["created_at"] == "2026-10-06T13:35:05Z"


def test_position_out_from_line_copies_every_field() -> None:
    line = PositionLine(
        position_id=4,
        ticker="AAA",
        qty=33,
        entry=Decimal("21.5608"),
        last=Decimal("21.90"),
        stop=Decimal("21.41"),
        unrealized_pnl=Decimal("11.2"),
        unprotected_seconds=12,
        stop_working=True,
    )
    out = schemas.PositionOut.from_line(line, symbol_id=7, strategy_key="orb_sip", opened_at=T0)
    assert (out.id, out.symbol_id, out.strategy_key, out.status, out.opened_at, out.closed_at) == (
        4,
        7,
        "orb_sip",
        "open",
        T0,
        None,
    )
    for name in (
        "ticker",
        "qty",
        "entry",
        "last",
        "stop",
        "unrealized_pnl",
        "unprotected_seconds",
        "stop_working",
    ):
        assert getattr(out, name) == getattr(line, name), name
    assert out.model_dump(mode="json")["entry"] == "21.5608"


def test_datetimes_serialise_as_utc() -> None:
    from zoneinfo import ZoneInfo

    local = T0.astimezone(ZoneInfo("America/Edmonton"))
    out = schemas.StreamHello(server_time=local)
    assert out.model_dump(mode="json") == {"server_time": "2026-10-06T13:35:05Z"}


def test_reset_in_strips_and_bounds_the_reason() -> None:
    assert schemas.ResetIn(reason="  reviewed  ").reason == "reviewed"
    for bad in ("  ab  ", "x" * 501):
        with pytest.raises(ValueError):
            schemas.ResetIn(reason=bad)


def test_login_and_password_bounds() -> None:
    with pytest.raises(ValueError):
        schemas.LoginIn(username="stephen", password="pw", totp="12345")
    assert schemas.LoginIn(username="stephen", password="pw", totp="123456").totp == "123456"
    with pytest.raises(ValueError):
        schemas.PasswordChangeIn(current_password="old", new_password="7chars!")
    assert schemas.PasswordChangeIn(current_password="old", new_password="8chars!!").totp is None


def test_credential_in_hides_the_token() -> None:
    c = schemas.CredentialIn(refresh_token="abcdefghijklmnop")
    assert "abcdefghijklmnop" not in repr(c) and "abcdefghijklmnop" not in str(c.model_dump())
    with pytest.raises(ValueError):
        schemas.CredentialIn(refresh_token="")


def test_journal_in_tracks_the_fields_sent() -> None:
    body = schemas.JournalIn.model_validate({"notes": "late entry"})
    assert body.model_fields_set == {"notes"}
    with pytest.raises(ValueError):
        schemas.JournalIn(notes="x" * 5001)


# --- test 9: protocols, fakes and the test client -----------------------------------------------------------


def _structural_checks(
    auth: QuestradeAuth,
    fake_store: FakeCredentialStore,
    fake_launcher: FakeJobLauncher,
    real_launcher: SubprocessJobLauncher,
    fake_feed: FakeFeed,
    real_feed: PollingChangeFeed,
) -> None:
    """Never called: mypy checks these assignments (the protocols are satisfied structurally)."""
    a: CredentialStore = auth
    b: CredentialStore = fake_store
    c: JobLauncher = fake_launcher
    d: JobLauncher = real_launcher
    e: ChangeFeed = fake_feed
    f: ChangeFeed = real_feed
    del a, b, c, d, e, f


def _members(proto: type) -> dict[str, Callable[..., Any]]:
    return {
        name: value for name, value in vars(proto).items() if callable(value) and not name.startswith("_")
    }


def _params(fn: Callable[..., Any]) -> list[str]:
    return [p for p in inspect.signature(fn).parameters if p != "self"]


@pytest.mark.parametrize(
    ("impl", "proto"),
    [
        (QuestradeAuth, CredentialStore),
        (FakeCredentialStore, CredentialStore),
        (FakeJobLauncher, JobLauncher),
        (SubprocessJobLauncher, JobLauncher),
        (FakeFeed, ChangeFeed),
        (PollingChangeFeed, ChangeFeed),
    ],
    ids=lambda c: c.__name__,
)
def test_implementation_has_every_protocol_member(impl: type, proto: type) -> None:
    members = _members(proto)
    assert members, f"{proto.__name__} declares no methods"
    for name, spec in members.items():
        got = getattr(impl, name, None)
        assert got is not None, f"{impl.__name__} lacks {name}"
        assert _params(got) == _params(spec), f"{impl.__name__}.{name} parameters differ"
        assert inspect.iscoroutinefunction(got) == inspect.iscoroutinefunction(spec), name


def test_api_services_fields() -> None:
    assert [f.name for f in dataclasses.fields(ApiServices)] == [
        "core",
        "registry",
        "killswitches",
        "credentials",
        "decider_for",
        "notifier",
        "telegram_configured",
        "quotes",
        "candles",
        "jobs",
        "feed",
        "plan",
        "fired",
        "replays",  # P5-T1: ReplayLauncher | None = None
        "options",  # OPTSIM-T1: OptionApiServices | None = None
    ]
    assert ApiServices.__dataclass_params__.frozen  # type: ignore[attr-defined]


def test_auth_user_and_actor() -> None:
    user = AuthUser(id=1, username="stephen", session_id=3, csrf_token="t")
    assert deps.actor(user) == "web:stephen"
    with pytest.raises(dataclasses.FrozenInstanceError):
        user.username = "x"  # type: ignore[misc]


@pytest.mark.db
def test_make_client_serves_a_router_and_401s_without_a_user(db_factory: sessionmaker[Session]) -> None:
    services = make_services(test_core(db_factory, FixedClock(T0)))
    router = APIRouter(tags=["probe"])

    @router.get("/probe")
    def probe(user: deps.CurrentUser) -> dict[str, str]:
        return {"user": user.username}

    @router.post("/probe")
    def probe_post(user: deps.CsrfUser, services: deps.Services) -> dict[str, bool]:
        return {"telegram": services.telegram_configured}

    client = make_client(services, router)
    assert str(client.base_url) == "https://testserver"
    assert client.get("/api/probe").json() == {"user": DEFAULT_USER.username}
    assert client.post("/api/probe").json() == {"telegram": True}
    anonymous = make_client(services, router, user=None)
    for resp in (anonymous.get("/api/probe"), anonymous.post("/api/probe")):
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "unauthorized"


@pytest.mark.db
def test_resolve_run_and_live_run_id(db_factory: sessionmaker[Session]) -> None:
    services = make_services(test_core(db_factory, FixedClock(T0)))
    live = deps.live_run_id(services)
    assert deps.resolve_run(services, "live") == live
    assert deps.resolve_run(services, str(live)) == live
    for bad in ("999999", "nope", "-1"):
        with pytest.raises(ApiError) as err:
            deps.resolve_run(services, bad)
        assert err.value.status == 404 and err.value.code == "not_found"


@pytest.mark.db
def test_make_services_defaults_and_overrides(db_factory: sessionmaker[Session]) -> None:
    core = test_core(db_factory, FixedClock(T0))
    services = make_services(core)
    assert services.core is core and services.telegram_configured is True
    assert isinstance(services.notifier, RecordingNotifier)
    assert isinstance(services.credentials, FakeCredentialStore)
    assert isinstance(services.jobs, FakeJobLauncher) and isinstance(services.feed, FakeFeed)
    assert services.plan(DAY).session_date == DAY and services.fired(DAY) == set()
    off = make_services(core, telegram_configured=False, quotes=None)
    assert off.telegram_configured is False and off.quotes is None
    assert core.env.session_secret.get_secret_value()  # a generated throwaway key


@pytest.mark.db
def test_recording_decider_wraps_the_real_build_decider(db_factory: sessionmaker[Session]) -> None:
    core = test_core(db_factory, FixedClock(T0))
    services = make_services(core)
    assert isinstance(services.decider_for, RecordingDeciderFor)
    run_id = deps.live_run_id(services)
    with pytest.raises(KeyError):  # the real ProposalService: an unknown proposal
        services.decider_for(run_id)(12345, "approve", "web", "web:stephen")
    assert services.decider_for.calls == [(run_id, 12345, "approve", "web", "web:stephen")]


async def test_fake_quotes_and_candles() -> None:
    quotes = fake_quotes({1: Decimal("21.50")})
    got = await quotes([1, 2])
    assert set(got) == {1} and got[1].last == Decimal("21.50") and got[1].symbol_id == 1
    bar = Candle(
        T0, T0 + timedelta(minutes=5), Decimal(1), Decimal(2), Decimal("0.5"), Decimal("1.5"), 100, None
    )
    candles = fake_candles([bar])
    assert await candles(1, T0, T0 + timedelta(hours=1)) == [bar]
    assert candles.calls == [(1, T0, T0 + timedelta(hours=1))]


async def test_fake_job_launcher_records_and_can_be_running() -> None:
    launcher = FakeJobLauncher()
    out = await launcher.launch("nightly", DAY, True, "web:stephen")
    assert out.launched is True and out.job == "nightly" and out.session_date == DAY
    assert launcher.launches == [("nightly", DAY, True, "web:stephen")]
    assert launcher.running("nightly") is False
    launcher.running_jobs.add("nightly")
    assert launcher.running("nightly") is True


def test_fake_credential_store() -> None:
    store = FakeCredentialStore()
    assert store.health().seeded is True
    store.seed("new-token-value")
    assert store.seeded == ["new-token-value"]
    assert store.access().token
    store.access_error = RuntimeError("rejected")
    with pytest.raises(RuntimeError):
        store.access()


async def test_fake_feed_delivers_and_records_run() -> None:
    feed = FakeFeed()
    hello = FeedMessage("invalidate", {"topics": ["proposals"]})
    feed.push(hello)  # before anyone subscribed: kept for the first subscriber
    async with feed.subscribe() as messages:
        assert feed.subscriber_count() == 1
        feed.push(FeedMessage("events", {"items": []}))
        got = [await anext(messages), await anext(messages)]
    assert [g.kind for g in got] == ["invalidate", "events"]
    assert feed.subscriber_count() == 0
    stop = asyncio.Event()
    task = asyncio.create_task(feed.run(stop))
    await asyncio.sleep(0)
    assert feed.run_started and not feed.run_stopped
    stop.set()
    await task
    assert feed.run_stopped


async def test_fake_feed_close_ends_iteration() -> None:
    feed = FakeFeed()
    async with feed.subscribe() as messages:
        feed.close()
        assert [msg async for msg in messages] == []


def test_default_user() -> None:
    assert DEFAULT_USER == AuthUser(
        id=1, username="stephen", session_id=1, csrf_token=DEFAULT_USER.csrf_token
    )
