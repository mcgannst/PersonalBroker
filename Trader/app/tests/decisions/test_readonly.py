"""P6-T10 acceptance test 12 (integration): read-only. Every table except `decision_log` has the same row
count and max id before and after `record_day` over a seeded day (and after a final and a rebuild pass), and
strict fakes fail the test on any Questrade, Claude, Telegram or FinViz call or any non-local connection."""

import socket
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from tests.decisions.test_recorder import SCAN_AT, D, deps, et, rows, scan_world
from trader.db import models as m
from trader.db.session import session_scope
from trader.decisions.recorder import record_day

pytestmark = pytest.mark.db


class Forbidden(AssertionError):
    pass


def _refuse(what: str) -> Any:
    def fail(*args: Any, **kwargs: Any) -> Any:
        raise Forbidden(f"the recorder called {what}")

    return fail


@pytest.fixture
def strict(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any Questrade, Claude, Telegram or FinViz client, and any connection that is not local, fails."""
    from trader.adapters.claude import catalyst
    from trader.adapters.finviz import scraper
    from trader.adapters.questrade import auth, client
    from trader.adapters.telegram import bot

    monkeypatch.setattr(client.QuestradeClient, "__init__", _refuse("QuestradeClient"))
    monkeypatch.setattr(auth.QuestradeAuth, "__init__", _refuse("QuestradeAuth"))
    monkeypatch.setattr(catalyst.CatalystClassifier, "__init__", _refuse("CatalystClassifier"))
    monkeypatch.setattr(catalyst.CatalystService, "__init__", _refuse("CatalystService"))
    monkeypatch.setattr(scraper.FinvizScraper, "__init__", _refuse("FinvizScraper"))
    monkeypatch.setattr(bot.TelegramBot, "__init__", _refuse("TelegramBot"))
    real_connect = socket.socket.connect

    def local_only(self: socket.socket, address: Any) -> Any:
        host = address[0] if isinstance(address, tuple) else address
        if host not in ("127.0.0.1", "::1", "localhost") and not str(host).startswith("/"):
            raise Forbidden(f"network connection to {host}")
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", local_only)


def table_stats(factory: sessionmaker[Session]) -> dict[str, tuple[int, Any]]:
    out: dict[str, tuple[int, Any]] = {}
    with factory() as s:
        for t in m.Base.metadata.sorted_tables:
            if t.name == "decision_log":
                continue
            count = s.execute(select(func.count()).select_from(t)).scalar_one()
            mx = s.execute(select(func.max(t.c.id))).scalar_one() if "id" in t.c else None
            out[t.name] = (count, mx)
    return out


def seed_full_day(factory: sessionmaker[Session]) -> int:
    w = scan_world(factory)
    w.job("nightly", {"source": "finviz", "universe": 29}, et(8, 0) - timedelta(hours=14))
    w.job(
        "premarket", {"screens": [{"label": "news", "filter": "f", "rows": 1, "matched": ["R01"]}]}, et(8, 0)
    )
    w.catalyst("R01", gap="0.05")
    sig = w.signal("R01", evidence={"rvol": "5"})
    p = w.proposal(sig, "R01", status="submitted", via="telegram", by="telegram:1", latency=30_000)
    buy = w.order(
        "R01", side="buy", order_type="stop", purpose="entry", status="filled", stop="21.51", proposal_id=p
    )
    w.fill(buy, "21.53", at=SCAN_AT + timedelta(minutes=2))
    w.trade("R01", exit_reason="flatten_close", pnl="3.10", pnl_r="0.9", opened=SCAN_AT, closed=et(15, 50))
    w.event("strategy.spy_overlay", "overlay: decision", {"decision": "hold", "benchmark": "SPY"}, et(15, 30))
    with session_scope(factory) as s:
        s.add(
            m.KillSwitchEvent(
                run_id=w.run_id,
                switch="manual_pause",
                session_date=D,
                tripped_at=et(11, 0),
                value=None,
                threshold=None,
                reset_at=None,
                reset_reason=None,
                reset_by=None,
            )
        )
    return w.run_id


@pytest.mark.usefixtures("strict")
async def test_12_record_day_changes_no_other_table(db_factory: sessionmaker[Session]) -> None:
    run_id = seed_full_day(db_factory)
    before = table_stats(db_factory)
    d = deps(db_factory)
    assert (await record_day(d, run_id, D)).skipped is None
    assert (await record_day(d, run_id, D)).skipped == "unchanged"
    assert (await record_day(d, run_id, D, final=True)).final
    assert (await record_day(d, run_id, D, rebuild=True, final=True)).skipped is None
    after = table_stats(db_factory)
    assert after == before
    stages = {r.stage for r in rows(db_factory, run_id)}
    assert stages == {
        "universe",
        "premarket",
        "scan",
        "signal",
        "proposal",
        "approval",
        "order",
        "fill",
        "exit",
        "overlay",
        "kill_switch",
        "day",
    }
    assert Decimal(next(r for r in rows(db_factory, run_id, "fill")).data["diff_per_share"]) == Decimal(
        "0.02"
    )


def test_the_strict_fakes_are_not_vacuous(strict: None) -> None:
    from trader.adapters.finviz.scraper import FinvizScraper

    with pytest.raises(Forbidden):
        FinvizScraper()  # type: ignore[call-arg]
    with pytest.raises(Forbidden):
        socket.create_connection(("192.0.2.1", 443), timeout=0.1)
