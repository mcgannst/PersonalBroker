"""P3-T11: the post-close job and the candle archive (BR-33, BR-42, BR-60; SPEC §8, §9, §10)."""

import dataclasses
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session, sessionmaker

from tests.factories import add_run, add_strategy_config, add_symbol
from tests.fakes_telegram import FakeIssuer, FakeRenderer, FakeTelegramApi, RecordingNotifier
from trader.adapters.telegram.types import TelegramApiError
from trader.broker.types import PositionView
from trader.db import models as m
from trader.jobs.postclose import PostcloseDeps, daily_summary_view, run_postclose
from trader.market import repository as repo
from trader.market.calendar import SessionCalendar
from trader.market.clock import ET, FixedClock
from trader.market.types import Candle, Interval, OpeningBars, UniverseMember
from trader.notify.notifier import TelegramNotifier
from trader.notify.types import DailySummaryView
from trader.settings_store import RuntimeSettings

pytestmark = pytest.mark.db
CAL = SessionCalendar()
DAY = date(2026, 10, 6)  # Tuesday, EDT
EARLY = date(2026, 11, 27)  # the day after Thanksgiving, 13:00 ET close
THANKSGIVING = date(2026, 11, 26)
CHAT = 42
ONE_MIN = timedelta(minutes=1)


def et(d: date, h: int, mi: int) -> datetime:
    return datetime.combine(d, time(h, mi), tzinfo=ET).astimezone(UTC)


def post_close(d: date) -> datetime:
    return et(d, 16, 15)


def bar(start: datetime, step: timedelta = ONE_MIN, close: str = "21.40") -> Candle:
    return Candle(
        start, start + step, Decimal("21.00"), Decimal("21.50"), Decimal("20.90"), Decimal(close), 5000, None
    )


class FakeData:
    """ArchiveData. `opening_bars` serves every requested id except those in `no_bar`; `candles` serves
    1-minute bars at 04:00, 09:30, 12:59, 13:00, 15:59 and 19:59 ET, or raises for ids in `raise_for`."""

    def __init__(self, members: Sequence[UniverseMember] = ()) -> None:
        self.members = list(members)
        self.no_bar: set[int] = set()
        self.raise_for: set[int] = set()
        self.opening_calls: list[list[int] | None] = []
        self.candle_calls: list[tuple[int, datetime, datetime, Interval]] = []

    async def universe(self, session_date: date) -> list[UniverseMember]:
        return list(self.members)

    async def opening_bars(self, session_date: date, symbol_ids: Sequence[int] | None = None) -> OpeningBars:
        ids = list(symbol_ids) if symbol_ids is not None else [u.symbol_id for u in self.members]
        self.opening_calls.append(ids)
        open_ = CAL.session_open(session_date)
        bars = {sid: bar(open_, timedelta(minutes=5)) for sid in ids if sid not in self.no_bar}
        return OpeningBars(bars, {sid: "no_bar_at_open" for sid in ids if sid in self.no_bar})

    async def candles(
        self, symbol_id: int, start: datetime, end: datetime, interval: Interval
    ) -> list[Candle]:
        self.candle_calls.append((symbol_id, start, end, interval))
        if symbol_id in self.raise_for:
            raise RuntimeError("HTTP 500 from Questrade")
        d = start.astimezone(ET).date()
        return [bar(et(d, h, mi)) for h, mi in ((4, 0), (9, 30), (12, 59), (13, 0), (15, 59), (19, 59))]


@dataclass
class FakeEngine:
    still_open: list[PositionView] = field(default_factory=list)
    end_calls: list[date] = field(default_factory=list)

    async def run_event(self, event_key: str, session_date: date) -> Any:
        raise AssertionError("not used")

    async def poll_quotes(self) -> Sequence[Any]:
        return []

    async def tick(self, now: datetime) -> None:
        return None

    async def end_of_session(self, session_date: date) -> Sequence[Any]:
        self.end_calls.append(session_date)
        return list(self.still_open)


@dataclass
class World:
    factory: sessionmaker[Session]
    run_id: int
    ids: dict[str, int]
    data: FakeData
    engine: FakeEngine
    notifier: RecordingNotifier
    render: FakeRenderer
    issuer: FakeIssuer
    settings: RuntimeSettings

    def deps(self, now: datetime) -> PostcloseDeps:
        return PostcloseDeps(
            factory=self.factory,
            clock=FixedClock(now),
            calendar=CAL,
            settings=lambda: self.settings,
            engine=self.engine,
            data=self.data,
            notifier=self.notifier,
            render=self.render,
            issuer=self.issuer,
            chat_id=CHAT,
            run_id=self.run_id,
        )

    def summaries(self) -> list[DailySummaryView]:
        return [args[0] for name, args in self.render.calls if name == "daily_summary"]


def member(sid: int, ticker: str) -> UniverseMember:
    return UniverseMember(sid, ticker, None, Decimal("20"), 1_000_000, Decimal("1"), "finviz")


@pytest.fixture
def world(db_factory: sessionmaker[Session]) -> World:
    """AAA, BBB, CCC in the universe (SPY exists as a symbol); an active live run."""
    with db_factory() as s:
        ids = {t: add_symbol(s, t) for t in ("AAA", "BBB", "CCC", "SPY")}
        run_id = add_run(s)
        s.commit()
    data = FakeData([member(ids[t], t) for t in ("AAA", "BBB", "CCC")])
    return World(
        db_factory,
        run_id,
        ids,
        data,
        FakeEngine(),
        RecordingNotifier(),
        FakeRenderer(),
        FakeIssuer(),
        RuntimeSettings(),
    )


def archive_rows(factory: sessionmaker[Session], interval: str) -> list[m.CandleArchive]:
    with factory() as s:
        return list(
            s.execute(
                select(m.CandleArchive)
                .where(m.CandleArchive.interval == interval)
                .order_by(m.CandleArchive.symbol_id, m.CandleArchive.start_ts)
            ).scalars()
        )


def add_candidates(s: Session, run_id: int, ranked: Sequence[int], strategy: str = "orb_sip") -> None:
    for rank, sid in enumerate(ranked, start=1):
        s.add(
            m.Candidate(
                run_id=run_id,
                session_date=DAY,
                strategy_key=strategy,
                symbol_id=sid,
                rank=rank,
                passed=True,
                created_at=et(DAY, 9, 35),
            )
        )


def error_events(factory: sessionmaker[Session]) -> list[m.EventLog]:
    with factory() as s:
        return list(
            s.execute(
                select(m.EventLog).where(m.EventLog.level == "error", m.EventLog.source == "job.postclose")
            ).scalars()
        )


# --- 1. holidays ---------------------------------------------------------------------------------------


async def test_holiday_runs_nothing_and_sends_nothing(world: World) -> None:
    out = await run_postclose(world.deps(post_close(THANKSGIVING)), THANKSGIVING)
    assert out == {"skipped": "not a session"}
    assert world.engine.end_calls == []
    assert world.notifier.sent == []
    assert world.data.opening_calls == [] and world.data.candle_calls == []
    with world.factory() as s:
        assert s.execute(select(func.count()).select_from(m.Journal)).scalar_one() == 0


# --- 2. the opening bars (archive a) ----------------------------------------------------------------------


async def test_opening_bars_come_from_the_cache_and_only_the_missing_one_is_fetched(world: World) -> None:
    open_ = CAL.session_open(DAY)
    with world.factory() as s:
        for t in ("AAA", "BBB"):
            repo.upsert_intraday_candles(s, world.ids[t], "5m", [bar(open_, timedelta(minutes=5))])
        s.commit()
    out = await run_postclose(world.deps(post_close(DAY)), DAY)
    rows = archive_rows(world.factory, "5m")
    assert {r.symbol_id for r in rows} == {world.ids[t] for t in ("AAA", "BBB", "CCC")}
    assert all(r.start_ts == open_ for r in rows)
    assert world.data.opening_calls == [[world.ids["CCC"]]]
    assert out["archive"]["5m"] == 3


# --- 3. and 4. the 1-minute candles (archive b) -----------------------------------------------------------


async def test_one_minute_candles_for_the_top_n_candidates_and_spy_in_regular_hours(
    world: World, db_factory: sessionmaker[Session]
) -> None:
    with db_factory() as s:
        ranked = [add_symbol(s, f"T{i:02d}") for i in range(1, 26)]
        add_candidates(s, world.run_id, ranked)
        add_candidates(s, world.run_id, ranked[:1], strategy="other")  # T01 again: distinct symbols
        s.commit()
    await run_postclose(world.deps(post_close(DAY)), DAY)
    rows = archive_rows(world.factory, "1m")
    assert {r.symbol_id for r in rows} == {*ranked[:20], world.ids["SPY"]}
    open_, close = CAL.session_open(DAY), CAL.session_close(DAY)
    assert all(open_ <= r.start_ts < close for r in rows)
    starts = {r.start_ts for r in rows}
    assert et(DAY, 4, 0) not in starts and et(DAY, 19, 59) not in starts
    assert starts == {et(DAY, 9, 30), et(DAY, 12, 59), et(DAY, 13, 0), et(DAY, 15, 59)}


async def test_early_close_window_ends_at_13_00_et(world: World) -> None:
    await run_postclose(world.deps(post_close(EARLY)), EARLY)
    spy = [c for c in world.data.candle_calls if c[0] == world.ids["SPY"]]
    assert len(spy) == 1
    _, start, end, interval = spy[0]
    assert (start, end, interval) == (et(EARLY, 9, 30), et(EARLY, 13, 0), "OneMinute")
    rows = archive_rows(world.factory, "1m")
    assert {r.start_ts for r in rows} == {et(EARLY, 9, 30), et(EARLY, 12, 59)}


# --- 5. missing data ------------------------------------------------------------------------------------


async def test_a_failing_symbol_is_listed_missing_and_the_job_still_returns(
    world: World, db_factory: sessionmaker[Session]
) -> None:
    with db_factory() as s:
        add_candidates(s, world.run_id, [world.ids["AAA"], world.ids["BBB"]])
        s.commit()
    world.data.raise_for = {world.ids["AAA"]}
    out = await run_postclose(world.deps(post_close(DAY)), DAY)
    missing = out["archive"]["missing"]
    assert [(x["ticker"], x["interval"]) for x in missing] == [("AAA", "1m")]
    assert "HTTP 500" in missing[0]["reason"]
    assert {r.symbol_id for r in archive_rows(world.factory, "1m")} == {world.ids["BBB"], world.ids["SPY"]}
    assert error_events(world.factory) == []  # one candidate missing is not an error
    assert out["summary_sent"] is True


async def test_spy_missing_writes_an_error_event(world: World) -> None:
    world.data.raise_for = {world.ids["SPY"]}
    out = await run_postclose(world.deps(post_close(DAY)), DAY)
    assert [(x["ticker"], x["interval"]) for x in out["archive"]["missing"]] == [("SPY", "1m")]
    events = error_events(world.factory)
    assert len(events) == 1 and "SPY" in events[0].message
    assert events[0].run_id == world.run_id
    assert len(world.notifier.sent) == 1


async def test_more_than_5pct_of_opening_bars_missing_writes_an_error_event(world: World) -> None:
    world.data.no_bar = {world.ids["CCC"]}
    out = await run_postclose(world.deps(post_close(DAY)), DAY)
    assert [(x["ticker"], x["interval"], x["reason"]) for x in out["archive"]["missing"]] == [
        ("CCC", "5m", "no_bar_at_open")
    ]
    assert out["archive"]["5m"] == 2
    events = error_events(world.factory)
    assert len(events) == 1 and "opening bars" in events[0].message


# --- 6. re-runs --------------------------------------------------------------------------------------------


async def test_forced_rerun_writes_no_duplicates_keeps_the_answer_and_sends_one_summary(
    world: World, db_factory: sessionmaker[Session]
) -> None:
    with db_factory() as s:
        add_candidates(s, world.run_id, [world.ids["AAA"]])
        s.commit()
    first = await run_postclose(world.deps(post_close(DAY)), DAY)
    counts = (len(archive_rows(world.factory, "5m")), len(archive_rows(world.factory, "1m")))
    with db_factory() as s:
        row = s.get(m.Journal, (world.run_id, DAY))
        assert row is not None and row.rules_followed is None
        row.rules_followed, row.answered_via = True, "telegram"
        s.commit()
    second = await run_postclose(world.deps(post_close(DAY) + timedelta(minutes=5)), DAY)
    assert (len(archive_rows(world.factory, "5m")), len(archive_rows(world.factory, "1m"))) == counts
    assert first["archive"]["5m"] == second["archive"]["5m"] == 3
    with db_factory() as s:
        journal = s.execute(select(m.Journal)).scalars().all()
        assert [(j.rules_followed, j.answered_via) for j in journal] == [(True, "telegram")]
    assert [msg.kind for msg in world.notifier.sent] == ["daily_summary"]
    assert world.notifier.sent[0].dedupe_key == f"summary:{DAY.isoformat()}"


# --- 7. end of session --------------------------------------------------------------------------------------


def add_position(
    s: Session,
    run_id: int,
    symbol_id: int,
    *,
    closed: bool,
    unprotected_seconds: int = 0,
    session_date: date = DAY,
) -> int:
    pos = m.Position(
        run_id=run_id,
        symbol_id=symbol_id,
        qty=100,
        avg_price=Decimal("21.0000"),
        stop_loss=Decimal("20.5000"),
        planned_risk=Decimal("50.0000"),
        session_date=session_date,
        opened_at=et(session_date, 9, 36),
        closed_at=et(session_date, 15, 55) if closed else None,
        entry_order_id=1,
        unprotected_seconds=unprotected_seconds,
    )
    s.add(pos)
    s.flush()
    return pos.id


async def test_end_of_session_is_called_once_and_an_open_position_shows_as_open(
    world: World, db_factory: sessionmaker[Session]
) -> None:
    with db_factory() as s:
        pid = add_position(s, world.run_id, world.ids["AAA"], closed=False)
        s.commit()
    world.engine.still_open = [
        PositionView(
            pid,
            world.ids["AAA"],
            None,
            100,
            Decimal("21"),
            Decimal("20.5"),
            et(DAY, 9, 36),
            DAY,
            None,
            None,
            0,
        )
    ]
    out = await run_postclose(world.deps(post_close(DAY)), DAY)
    assert world.engine.end_calls == [DAY]
    assert out["open_positions"] == [pid]
    (view,) = world.summaries()
    assert [(p.position_id, p.ticker, p.stop_working) for p in view.open_positions] == [(pid, "AAA", False)]


async def test_cancelled_counts_the_orders_end_of_session_cancelled(world: World) -> None:
    """The fake engine cancels one working order the way SimBroker.end_of_session does."""
    now = post_close(DAY)
    with world.factory() as s:
        order = m.Order(
            run_id=world.run_id,
            symbol_id=world.ids["AAA"],
            side="buy",
            order_type="stop",
            purpose="entry",
            qty=10,
            stop_price=Decimal("22"),
            tif="day",
            status="working",
            reason="orb_entry",
            session_date=DAY,
            submitted_at=et(DAY, 9, 35),
            stale_alerted=False,
        )
        s.add(order)
        s.commit()
        order_id = order.id

    factory = world.factory

    class CancellingEngine(FakeEngine):
        async def end_of_session(self, session_date: date) -> Sequence[Any]:
            with factory() as s:
                o = s.get(m.Order, order_id)
                assert o is not None
                o.status, o.closed_at, o.cancel_reason = "cancelled", now, "end_of_session"
                s.commit()
            return await super().end_of_session(session_date)

    world.engine = CancellingEngine()
    out = await run_postclose(world.deps(now), DAY)
    assert out["cancelled"] == 1


# --- 8. the summary view --------------------------------------------------------------------------------


def add_decided_proposal(s: Session, run_id: int, signal_id: int, latency_ms: int, via: str) -> None:
    created = et(DAY, 9, 35)
    s.add(
        m.Proposal(
            run_id=run_id,
            signal_id=signal_id,
            kind="entry",
            order_spec={},
            qty=10,
            status="approved",
            created_at=created,
            expires_at=created + timedelta(minutes=2),
            decided_at=created + timedelta(milliseconds=latency_ms),
            decided_via=via,
            decided_by="stephen",
            decision_latency_ms=latency_ms,
            escalations=0,
        )
    )


def seed_day(s: Session, run_id: int, symbol_id: int) -> None:
    pid = add_position(s, run_id, symbol_id, closed=True, unprotected_seconds=95)
    s.add(
        m.Trade(
            run_id=run_id,
            position_id=pid,
            symbol_id=symbol_id,
            session_date=DAY,
            entry_price=Decimal("21.0000"),
            exit_price=Decimal("21.1500"),
            qty=100,
            pnl=Decimal("10.8157"),
            pnl_r=Decimal("0.2163"),
            planned_risk=Decimal("50.0000"),
            exit_reason="flatten_close",
            slippage_total=Decimal("0.0200"),
            fees_total=Decimal("4.1843"),
            opened_at=et(DAY, 9, 36),
            closed_at=et(DAY, 15, 55),
        )
    )
    cfg = add_strategy_config(s)
    signal = m.Signal(
        run_id=run_id,
        strategy_config_id=cfg,
        symbol_id=symbol_id,
        session_date=DAY,
        event_key="orb_open",
        ts=et(DAY, 9, 35),
        intent={},
        evidence={},
    )
    s.add(signal)
    s.flush()
    add_decided_proposal(s, run_id, signal.id, 1200, "telegram")
    add_decided_proposal(s, run_id, signal.id, 3000, "web")
    add_decided_proposal(s, run_id, signal.id, 0, "auto")  # not a human decision: not counted
    s.add(
        m.EquitySnapshot(
            run_id=run_id,
            ts=et(DAY, 16, 0),
            equity=Decimal("10010.8157"),
            cash=Decimal("10010.8157"),
            settled_cash=Decimal("10000.0000"),
            peak_equity=Decimal("10020.0000"),
            drawdown_pct=Decimal("0.0009"),
        )
    )
    s.add(
        m.KillSwitchEvent(run_id=run_id, switch="manual_pause", session_date=DAY, tripped_at=et(DAY, 10, 0))
    )


def test_daily_summary_view_for_a_seeded_day(world: World, db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        seed_day(s, world.run_id, world.ids["AAA"])
        s.commit()
    view = daily_summary_view(db_factory, world.run_id, DAY, post_close(DAY), {"5m": 3, "1m": 8})
    assert view.session_date == DAY
    assert view.realized_pnl == Decimal("10.8157")
    assert view.fees == Decimal("4.1843")
    assert view.avg_decision_seconds == pytest.approx(2.1)
    assert view.decisions == 2
    assert view.unprotected_seconds == 95
    assert [(t.ticker, t.qty, t.pnl, t.exit_reason) for t in view.trades] == [
        ("AAA", 100, Decimal("10.8157"), "flatten_close")
    ]
    assert (view.equity, view.drawdown_pct) == (Decimal("10010.8157"), Decimal("0.0009"))
    assert view.open_positions == ()
    assert view.blocking_switches == ("manual_pause",)
    assert dict(view.archive) == {"5m": 3, "1m": 8}


def test_daily_summary_view_of_an_empty_day(world: World) -> None:
    view = daily_summary_view(world.factory, world.run_id, DAY, post_close(DAY), {})
    assert (view.trades, view.realized_pnl, view.fees, view.decisions) == ((), Decimal(0), Decimal(0), 0)
    assert view.avg_decision_seconds is None
    assert view.unprotected_seconds == 0


# --- 9. the journal buttons --------------------------------------------------------------------------------


async def test_summary_carries_the_journal_buttons_from_the_issuer(world: World) -> None:
    await run_postclose(world.deps(post_close(DAY)), DAY)
    (issued,) = world.issuer.issued
    assert (issued["kind"], issued["ref"], issued["actions"], issued["chat_id"], issued["ttl_seconds"]) == (
        "journal",
        "20261006",
        ["y", "n"],
        CHAT,
        None,
    )
    (msg,) = world.notifier.sent
    data = [b.callback_data for row in msg.buttons for b in row]
    assert data == [issued["data"]["y"], issued["data"]["n"]]
    assert world.issuer.bound == {}  # not bound: Notifier.send returns no message id


# --- 10. upsert_candle_archive ----------------------------------------------------------------------------


def test_upsert_candle_archive_inserts_then_updates(world: World, db_factory: sessionmaker[Session]) -> None:
    sid = world.ids["AAA"]
    first = [bar(et(DAY, 9, 30)), bar(et(DAY, 9, 31))]
    with db_factory() as s:
        assert repo.upsert_candle_archive(s, sid, "1m", first) == 2
        assert repo.upsert_candle_archive(s, sid, "1m", []) == 0
        s.commit()
    changed = [bar(et(DAY, 9, 31), close="22.00"), bar(et(DAY, 9, 32))]
    with db_factory() as s:
        assert repo.upsert_candle_archive(s, sid, "1m", changed) == 2
        s.commit()
    rows = archive_rows(db_factory, "1m")
    assert [(r.start_ts, r.close) for r in rows] == [
        (et(DAY, 9, 30), Decimal("21.4000")),
        (et(DAY, 9, 31), Decimal("22.0000")),
        (et(DAY, 9, 32), Decimal("21.4000")),
    ]


# --- fix round 1 ------------------------------------------------------------------------------------------


async def no_sleep(_seconds: float) -> None:
    return None


def telegram_world(world: World, now: datetime) -> tuple[PostcloseDeps, FakeTelegramApi]:
    """The world's deps with a real TelegramNotifier (dedupe and status through `notifications`)."""
    api = FakeTelegramApi()
    notifier = TelegramNotifier(api, CHAT, world.factory, FixedClock(now), sleep=no_sleep)
    return dataclasses.replace(world.deps(now), notifier=notifier), api


async def test_a_failing_archive_is_an_error_event_and_the_summary_still_goes_out(world: World) -> None:
    """BR-60: the archive raising (the universe read, here) must not cost the summary and the journal."""

    async def broken_universe(session_date: date) -> list[UniverseMember]:
        raise ConnectionError("do not report this text")

    world.data.universe = broken_universe  # type: ignore[method-assign]
    out = await run_postclose(world.deps(post_close(DAY)), DAY)
    assert out["archive"] == {"error": "ConnectionError"}
    assert out["summary_sent"] is True and [msg.kind for msg in world.notifier.sent] == ["daily_summary"]
    (view,) = world.summaries()
    assert view.archive == {}
    (event,) = error_events(world.factory)
    assert "archive" in event.message and "do not report" not in event.message
    assert event.data["error"] == "ConnectionError"
    with world.factory() as s:
        assert s.execute(select(func.count()).select_from(m.Journal)).scalar_one() == 1


async def test_failing_journal_buttons_send_the_summary_without_buttons(world: World) -> None:
    def broken_issue(*args: object) -> tuple[str, dict[str, str]]:
        raise RuntimeError("issuer bug")

    world.issuer.issue = broken_issue  # type: ignore[method-assign,assignment]
    out = await run_postclose(world.deps(post_close(DAY)), DAY)
    (msg,) = world.notifier.sent
    assert msg.kind == "daily_summary" and msg.buttons == ()
    assert out["summary_sent"] is True


async def test_summary_status_sent_then_duplicate_without_a_new_nonce(world: World) -> None:
    """A forced re-run finds the `summary:<date>` row: no second send and no orphan journal nonce."""
    deps1, api1 = telegram_world(world, post_close(DAY))
    first = await run_postclose(deps1, DAY)
    assert (first["summary"], first["summary_sent"]) == ("sent", True)
    assert len(api1.calls_of("send_message")) == 1 and len(world.issuer.issued) == 1

    deps2, api2 = telegram_world(world, post_close(DAY) + timedelta(minutes=30))
    second = await run_postclose(deps2, DAY)
    assert (second["summary"], second["summary_sent"]) == ("duplicate", False)
    assert api2.calls_of("send_message") == [] and len(world.issuer.issued) == 1


async def test_summary_status_failed_when_telegram_rejects_it(world: World) -> None:
    deps, api = telegram_world(world, post_close(DAY))
    api.fail("send_message", TelegramApiError(400, "Bad Request: chat not found"))
    out = await run_postclose(deps, DAY)
    assert (out["summary"], out["summary_sent"]) == ("failed", False)


async def test_opening_bars_are_upserted_in_one_statement(world: World) -> None:
    """Three universe symbols: one 5m statement (and one 1m statement for SPY), not one per symbol."""
    engine = world.factory.kw["bind"]
    statements: list[str] = []

    def record(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        if statement.lstrip().upper().startswith("INSERT INTO") and "candle_archive" in statement:
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        out = await run_postclose(world.deps(post_close(DAY)), DAY)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert out["archive"]["5m"] == 3 and len(archive_rows(world.factory, "5m")) == 3
    assert len(statements) == 2


def test_upsert_candle_archive_bars_many_symbols(world: World, db_factory: sessionmaker[Session]) -> None:
    open_ = CAL.session_open(DAY)
    five = timedelta(minutes=5)
    a, b = world.ids["AAA"], world.ids["BBB"]
    pairs = [(a, bar(open_, five)), (b, bar(open_, five)), (a, bar(open_, five, close="22.00"))]
    with db_factory() as s:
        assert repo.upsert_candle_archive_bars(s, "5m", pairs) == 2  # (a, open) once, the last one wins
        assert repo.upsert_candle_archive_bars(s, "5m", []) == 0
        s.commit()
    assert [(r.symbol_id, r.close) for r in archive_rows(db_factory, "5m")] == [
        (a, Decimal("22.0000")),
        (b, Decimal("21.4000")),
    ]
