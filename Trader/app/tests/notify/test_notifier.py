"""TelegramNotifier, NullNotifier and split_text (P3-T5). FakeTelegramApi, a real DB, no real sleeping."""

from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from structlog.testing import capture_logs

from tests.fakes_telegram import FakeTelegramApi
from trader.adapters.telegram.types import TelegramApiError
from trader.db.models import EventLog, Notification
from trader.market.clock import FixedClock
from trader.notify.notifier import NullNotifier, TelegramNotifier, split_text
from trader.notify.types import Button, Notifier, OutboundMessage

NOW = datetime(2026, 10, 6, 13, 40, tzinfo=UTC)
CHAT = 424242
BUTTONS = ((Button("Yes", "j:20261006:y:n1:mac"), Button("No", "j:20261006:n:n1:mac")),)


class FakeSleep:
    """Records waits and moves the clock forward, as real time would."""

    def __init__(self, clock: FixedClock) -> None:
        self.clock = clock
        self.waits: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)
        self.clock.advance(timedelta(seconds=seconds))


def make(
    factory: sessionmaker[Session], api: FakeTelegramApi | None = None
) -> tuple[TelegramNotifier, FakeTelegramApi, FakeSleep]:
    clock = FixedClock(NOW)
    sleep = FakeSleep(clock)
    fake = api or FakeTelegramApi()
    return TelegramNotifier(fake, CHAT, factory, clock, sleep=sleep), fake, sleep


def rows(factory: sessionmaker[Session]) -> list[Notification]:
    with factory() as s:
        return list(s.scalars(select(Notification).order_by(Notification.id)))


def events(factory: sessionmaker[Session]) -> list[EventLog]:
    with factory() as s:
        return list(s.scalars(select(EventLog).order_by(EventLog.id)))


def test_null_notifier_satisfies_protocol() -> None:
    null: Notifier = NullNotifier()
    assert null is not None


# --- split_text (no DB) -----------------------------------------------------------------------------


def test_split_short_text_is_one_part() -> None:
    assert split_text("hello") == ["hello"]
    assert split_text("x" * 4096) == ["x" * 4096]


def test_split_prefers_line_breaks_and_respects_limit() -> None:
    text = "\n".join(f"line {i:04d} " + "y" * 80 for i in range(100))  # ~9,000 characters
    parts = split_text(text)
    assert len(parts) == 3
    assert all(0 < len(p) <= 4096 for p in parts)
    assert all(p.startswith("line ") for p in parts)  # every cut is on a line break
    assert "\n".join(parts) == text


def test_split_without_line_breaks_cuts_hard() -> None:
    parts = split_text("z" * 9000, limit=4096)
    assert [len(p) for p in parts] == [4096, 4096, 808]
    assert "".join(parts) == "z" * 9000


# --- 10. NullNotifier -------------------------------------------------------------------------------


async def test_null_notifier_logs_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(NullNotifier, "_logged", False)
    with capture_logs() as logs:
        await NullNotifier().send(OutboundMessage(kind="alert", text="a"))
        await NullNotifier().send(OutboundMessage(kind="alert", text="b", dedupe_key="k"))
        await NullNotifier().send(OutboundMessage(kind="fill", text="c"))
    assert [e["event"] for e in logs] == ["telegram.not_configured"]


# --- DB-backed tests --------------------------------------------------------------------------------


@pytest.mark.db
async def test_sends_one_message_and_records_sent_row(db_factory: sessionmaker[Session]) -> None:
    notifier, api, _ = make(db_factory)

    await notifier.send(
        OutboundMessage(kind="fill", text="<b>BOUGHT</b> 33 AAA", buttons=BUTTONS, silent=True)
    )

    assert api.calls_of("send_message") == [
        {"chat_id": CHAT, "text": "<b>BOUGHT</b> 33 AAA", "buttons": BUTTONS, "silent": True}
    ]
    (row,) = rows(db_factory)
    assert (row.kind, row.status, row.message_ids, row.attempts, row.error) == ("fill", "sent", [1], 1, None)
    assert row.dedupe_key is None
    assert row.text == "<b>BOUGHT</b> 33 AAA"
    assert row.created_at == NOW
    assert row.sent_at == NOW


@pytest.mark.db
async def test_messages_without_key_are_sent_every_time(db_factory: sessionmaker[Session]) -> None:
    notifier, api, _ = make(db_factory)

    await notifier.send(OutboundMessage(kind="alert", text="a"))
    await notifier.send(OutboundMessage(kind="alert", text="a"))

    assert len(api.calls_of("send_message")) == 2
    assert [r.status for r in rows(db_factory)] == ["sent", "sent"]


@pytest.mark.db
async def test_same_dedupe_key_reaches_api_once(db_factory: sessionmaker[Session]) -> None:
    api = FakeTelegramApi()
    first, _, _ = make(db_factory, api)
    second, _, _ = make(db_factory, api)  # another process / notifier instance

    await first.send(OutboundMessage(kind="fill", text="one", dedupe_key="fill:7"))
    await first.send(OutboundMessage(kind="fill", text="one again", dedupe_key="fill:7"))
    await second.send(OutboundMessage(kind="fill", text="one more", dedupe_key="fill:7"))

    assert [c["text"] for c in api.calls_of("send_message")] == ["one"]
    (row,) = rows(db_factory)
    assert (row.dedupe_key, row.status) == ("fill:7", "sent")


@pytest.mark.db
async def test_failed_send_keeps_key_used(db_factory: sessionmaker[Session]) -> None:
    """At most once: a key whose send failed is not tried again by a later send."""
    notifier, api, _ = make(db_factory)
    api.fail("send_message", TelegramApiError(403, "Forbidden: bot was blocked by the user"))

    await notifier.send(OutboundMessage(kind="alert", text="x", dedupe_key="alert:1"))
    await notifier.send(OutboundMessage(kind="alert", text="x", dedupe_key="alert:1"))

    assert len(api.calls_of("send_message")) == 1
    assert [r.status for r in rows(db_factory)] == ["failed"]


@pytest.mark.db
async def test_400_is_recorded_failed_with_one_warning_event(db_factory: sessionmaker[Session]) -> None:
    notifier, api, sleep = make(db_factory)
    api.fail("send_message", TelegramApiError(400, "Bad Request: can't parse entities"))

    with capture_logs() as logs:
        await notifier.send(OutboundMessage(kind="alert", text="<b>broken", dedupe_key="alert:2"))

    assert len(api.calls_of("send_message")) == 1  # a 400 is not retried
    assert sleep.waits == []
    (row,) = rows(db_factory)
    assert row.status == "failed"
    assert row.error == "400 Bad Request: can't parse entities"
    assert row.sent_at is None
    assert row.attempts == 1
    (event,) = events(db_factory)
    assert (event.level, event.source) == ("warning", "telegram")
    assert "Bad Request: can't parse entities" in event.message
    warnings = [e for e in logs if e["log_level"] == "warning"]
    assert len(warnings) == 1
    assert warnings[0]["description"] == "Bad Request: can't parse entities"


@pytest.mark.db
async def test_429_waits_retry_after_then_sends_once(db_factory: sessionmaker[Session]) -> None:
    notifier, api, sleep = make(db_factory)
    api.fail("send_message", TelegramApiError(429, "Too Many Requests: retry after 3", retry_after=3))

    await notifier.send(OutboundMessage(kind="fill", text="x"))

    assert len(api.calls_of("send_message")) == 2  # the failed attempt and the retry
    assert sleep.waits == [3]
    (row,) = rows(db_factory)
    assert (row.status, row.message_ids, row.attempts) == ("sent", [1], 2)
    assert events(db_factory) == []


@pytest.mark.db
async def test_429_wait_is_capped(db_factory: sessionmaker[Session]) -> None:
    notifier, api, sleep = make(db_factory)
    api.fail("send_message", TelegramApiError(429, "Too Many Requests", retry_after=600))

    await notifier.send(OutboundMessage(kind="fill", text="x"))

    assert sleep.waits == [30]
    assert rows(db_factory)[0].status == "sent"


@pytest.mark.db
async def test_network_error_waits_two_seconds_and_retries_once(db_factory: sessionmaker[Session]) -> None:
    notifier, api, sleep = make(db_factory)
    api.fail("send_message", TelegramApiError(None, "TimedOut: Timed out"), times=2)

    await notifier.send(OutboundMessage(kind="alert", text="x"))

    assert len(api.calls_of("send_message")) == 2
    assert sleep.waits == [2]
    (row,) = rows(db_factory)
    assert (row.status, row.error) == ("failed", "None TimedOut: Timed out")
    assert len(events(db_factory)) == 1


@pytest.mark.db
async def test_api_raising_on_every_call_never_raises(db_factory: sessionmaker[Session]) -> None:
    notifier, api, _ = make(db_factory)
    api.fail("send_message", RuntimeError("unexpected"), times=10)

    await notifier.send(OutboundMessage(kind="alert", text="x"))
    await notifier.send(OutboundMessage(kind="alert", text="y", dedupe_key="k"))

    assert [r.status for r in rows(db_factory)] == ["failed", "failed"]
    assert rows(db_factory)[0].error == "RuntimeError"


class BrokenFactory:
    """A session factory whose sessions cannot be opened (the database is down)."""

    def __call__(self) -> Session:
        raise ConnectionError("database is down")


async def test_db_factory_raising_never_raises() -> None:
    api = FakeTelegramApi()
    clock = FixedClock(NOW)
    notifier = TelegramNotifier(
        api, CHAT, cast(sessionmaker[Session], BrokenFactory()), clock, sleep=FakeSleep(clock)
    )

    with capture_logs() as logs:
        await notifier.send(OutboundMessage(kind="alert", text="no key"))
        await notifier.send(OutboundMessage(kind="alert", text="keyed", dedupe_key="alert:9"))

    # Without a key nothing can be doubled, so it is still sent; a keyed message is held back (at most once).
    assert [c["text"] for c in api.calls_of("send_message")] == ["no key"]
    assert any(e["event"] == "notify.db_failed" for e in logs)


@pytest.mark.db
async def test_long_message_is_sent_in_three_parts_with_buttons_last(
    db_factory: sessionmaker[Session],
) -> None:
    notifier, api, sleep = make(db_factory)
    text = "\n".join(f"row {i:04d} " + "q" * 80 for i in range(100))
    assert 8900 < len(text) < 9200

    await notifier.send(OutboundMessage(kind="daily_summary", text=text, buttons=BUTTONS))

    sends = api.calls_of("send_message")
    assert len(sends) == 3
    assert [s["buttons"] for s in sends] == [(), (), BUTTONS]
    assert "\n".join(s["text"] for s in sends) == text
    assert sleep.waits == [1.0, 1.0]  # spaced one second apart
    (row,) = rows(db_factory)
    assert (row.status, row.message_ids) == ("sent", [1, 2, 3])


@pytest.mark.db
async def test_sends_are_spaced_one_second_apart(db_factory: sessionmaker[Session]) -> None:
    notifier, api, sleep = make(db_factory)

    await notifier.send(OutboundMessage(kind="alert", text="a"))
    await notifier.send(OutboundMessage(kind="alert", text="b"))
    sleep.clock.advance(timedelta(seconds=5))
    await notifier.send(OutboundMessage(kind="alert", text="c"))

    assert sleep.waits == [1.0]
    assert len(api.calls_of("send_message")) == 3
