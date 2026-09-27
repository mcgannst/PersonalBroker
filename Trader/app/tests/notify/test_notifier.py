"""TelegramNotifier, NullNotifier and split_text (P3-T5). FakeTelegramApi, a real DB, no real sleeping."""

from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from structlog.testing import capture_logs

from tests.fakes_telegram import FakeTelegramApi
from trader.adapters.telegram.api import TelegramNotSentError
from trader.adapters.telegram.types import TelegramApiError
from trader.db.models import EventLog, Notification
from trader.market.clock import FixedClock
from trader.notify.notifier import NullNotifier, TelegramNotifier, split_text
from trader.notify.types import Button, Buttons, Notifier, OutboundMessage

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


def test_split_prefers_spaces_when_there_is_no_line_break() -> None:
    text = " ".join(["word"] * 30)  # 149 characters
    parts = split_text(text, limit=50)
    assert all(len(p) <= 50 for p in parts)
    assert " ".join(parts) == text
    assert all(p.startswith("word") and p.endswith("word") for p in parts)


def test_split_closes_and_reopens_tags_across_parts() -> None:
    text = '<b>bold <a href="https://x/y?a=1&amp;b=2">' + "link " * 30 + "</a> tail</b>"
    parts = split_text(text, limit=80)
    assert len(parts) > 2 and all(len(p) <= 80 for p in parts)
    for p in parts:
        assert p.startswith("<b>") and p.endswith("</b>")
        assert p.count("<a ") == p.count("</a>") <= 1
        assert p.count("<b>") == p.count("</b>") == 1
    assert parts[1].startswith('<b><a href="https://x/y?a=1&amp;b=2">')


def test_split_never_cuts_an_entity_and_drops_blank_parts() -> None:
    parts = split_text("a" * 8 + "&amp;" + "b" * 3 + "\n\n   \n", limit=10)
    assert [p.strip() for p in parts] == ["a" * 8, "&amp;bbb"]
    assert split_text("") == [] and split_text(" \n ") == []
    assert split_text("x" * 20 + "\n" + " " * 30, limit=20) == ["x" * 20]


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
async def test_failed_send_may_be_retried_by_a_later_send_up_to_three_times(
    db_factory: sessionmaker[Session],
) -> None:
    """Fix round 1: a `failed` row (surely not delivered) is claimed again by a later send with the same
    key, up to three sends in total; a key that finally went out is never sent again."""
    notifier, api, _ = make(db_factory)
    api.fail("send_message", TelegramApiError(403, "Forbidden: bot was blocked by the user"), times=3)

    for _ in range(5):
        await notifier.send(OutboundMessage(kind="alert", text="x", dedupe_key="alert:1"))

    assert len(api.calls_of("send_message")) == 3
    (row,) = rows(db_factory)
    assert (row.status, row.attempts) == ("failed", 3)
    events_ = events(db_factory)
    assert len(events_) == 3
    assert [e.data["retry_later"] for e in events_] == [True, True, False]


@pytest.mark.db
async def test_429_storm_does_not_lose_the_alert_for_good(db_factory: sessionmaker[Session]) -> None:
    notifier, api, sleep = make(db_factory)
    api.fail(
        "send_message", TelegramApiError(429, "Too Many Requests: retry after 5", retry_after=5), times=2
    )

    await notifier.send(OutboundMessage(kind="kill_switch", text="k", dedupe_key="event:9"))
    (row,) = rows(db_factory)
    assert (row.status, row.attempts, row.message_ids) == ("failed", 1, None)
    await notifier.send(OutboundMessage(kind="kill_switch", text="k", dedupe_key="event:9"))
    await notifier.send(OutboundMessage(kind="kill_switch", text="k", dedupe_key="event:9"))

    assert len(api.calls_of("send_message")) == 3  # 429, 429 (the in-send retry), then sent
    (row,) = rows(db_factory)
    assert (row.status, row.attempts, row.message_ids, row.error) == ("sent", 2, [1], None)
    assert sleep.waits == [5, 1.0]  # the 429 wait, then the 1 s spacing before the next send


@pytest.mark.db
async def test_read_timeout_is_unknown_and_never_resent(db_factory: sessionmaker[Session]) -> None:
    """A read timeout may have delivered the message: no retry, status `unknown`, never sent again."""
    notifier, api, sleep = make(db_factory)
    api.fail("send_message", TelegramApiError(None, "TimedOut: Timed out"))

    await notifier.send(OutboundMessage(kind="fill", text="x", dedupe_key="fill:5"))
    await notifier.send(OutboundMessage(kind="fill", text="x", dedupe_key="fill:5"))

    assert len(api.calls_of("send_message")) == 1
    assert sleep.waits == []
    (row,) = rows(db_factory)
    assert (row.status, row.error) == ("unknown", "TimedOut: Timed out")
    (event,) = events(db_factory)
    assert event.level == "warning" and "may or may not have been delivered" in event.message


@pytest.mark.db
async def test_partly_delivered_message_is_never_claimed_again(db_factory: sessionmaker[Session]) -> None:
    class SecondPartRefused(FakeTelegramApi):
        async def send_message(
            self, chat_id: int, text: str, buttons: Buttons = (), silent: bool = False
        ) -> int:
            if len(self.calls_of("send_message")) == 1:
                self.fail("send_message", TelegramApiError(400, "Bad Request: can't parse entities"))
            return await super().send_message(chat_id, text, buttons, silent)

    notifier, api, _ = make(db_factory, SecondPartRefused())
    text = "\n".join(f"row {i:04d} " + "q" * 80 for i in range(100))

    await notifier.send(OutboundMessage(kind="daily_summary", text=text, dedupe_key="summary:1"))
    await notifier.send(OutboundMessage(kind="daily_summary", text=text, dedupe_key="summary:1"))

    assert len(api.calls_of("send_message")) == 2
    (row,) = rows(db_factory)
    assert (row.status, row.message_ids) == ("failed", [1])


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
    assert (row.status, row.message_ids, row.attempts) == ("sent", [1], 1)  # attempts counts sends
    assert events(db_factory) == []


@pytest.mark.db
async def test_429_wait_is_capped(db_factory: sessionmaker[Session]) -> None:
    notifier, api, sleep = make(db_factory)
    api.fail("send_message", TelegramApiError(429, "Too Many Requests", retry_after=600))

    await notifier.send(OutboundMessage(kind="fill", text="x"))

    assert sleep.waits == [30]
    assert rows(db_factory)[0].status == "sent"


@pytest.mark.db
@pytest.mark.parametrize(
    "exc",
    [
        TelegramNotSentError(None, "NetworkError: httpx.ConnectError: connection refused"),
        TelegramNotSentError(None, "Telegram server error 502: Bad Gateway"),
    ],
    ids=["connect", "5xx"],
)
async def test_surely_unsent_error_waits_two_seconds_and_retries_once(
    db_factory: sessionmaker[Session], exc: TelegramApiError
) -> None:
    notifier, api, sleep = make(db_factory)
    api.fail("send_message", exc, times=2)

    await notifier.send(OutboundMessage(kind="alert", text="x"))

    assert len(api.calls_of("send_message")) == 2
    assert sleep.waits == [2]
    (row,) = rows(db_factory)
    assert (row.status, row.error) == ("failed", exc.description)  # reads cleanly: no "None ..."
    assert len(events(db_factory)) == 1


@pytest.mark.db
async def test_api_raising_on_every_call_never_raises(db_factory: sessionmaker[Session]) -> None:
    """An unexpected exception may have happened after the request went out: `unknown`, not retried."""
    notifier, api, _ = make(db_factory)
    api.fail("send_message", RuntimeError("unexpected"), times=10)

    await notifier.send(OutboundMessage(kind="alert", text="x"))
    await notifier.send(OutboundMessage(kind="alert", text="y", dedupe_key="k"))
    await notifier.send(OutboundMessage(kind="alert", text="y", dedupe_key="k"))

    assert len(api.calls_of("send_message")) == 2
    assert [r.status for r in rows(db_factory)] == ["unknown", "unknown"]
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
