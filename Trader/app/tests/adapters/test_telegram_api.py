"""PtbTelegramApi (P3-T5): PTB's low-level Bot behind the TelegramApi protocol. respx only, no network."""

import json
import logging
import traceback
from typing import Any, cast
from urllib.parse import parse_qs

import httpx
import pytest
import respx
import telegram
from pydantic import SecretStr
from structlog.testing import capture_logs

from trader.adapters.telegram.api import PtbTelegramApi
from trader.adapters.telegram.types import TelegramApi, TelegramApiError
from trader.notify.types import Button

# A made-up token shaped like a real one (never a real secret in a fixture).
TOKEN = "123456789:FAKE-test-token-xyz"
BASE = f"https://api.telegram.org/bot{TOKEN}"
CHAT = 424242


def api() -> PtbTelegramApi:
    return PtbTelegramApi(SecretStr(TOKEN))


def form(request: httpx.Request) -> dict[str, str]:
    """PTB posts url-encoded form data whose complex values are JSON strings."""
    return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}


def message_json(message_id: int, chat_id: int = CHAT, text: str = "hi") -> dict[str, Any]:
    return {
        "message_id": message_id,
        "date": 1790000000,
        "chat": {"id": chat_id, "type": "private"},
        "from": {"id": 7, "is_bot": True, "first_name": "bot"},
        "text": text,
    }


def ok(result: Any) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": result})


def error(code: int, description: str, **parameters: Any) -> httpx.Response:
    body: dict[str, Any] = {"ok": False, "error_code": code, "description": description}
    if parameters:
        body["parameters"] = parameters
    return httpx.Response(code, json=body)


def test_satisfies_protocol() -> None:
    client: TelegramApi = api()
    assert client is not None


# --- 1. sendMessage ---------------------------------------------------------------------------------


@respx.mock
async def test_send_message_posts_html_and_keyboard_and_returns_id() -> None:
    route = respx.post(f"{BASE}/sendMessage").mock(return_value=ok(message_json(77)))
    buttons = ((Button("Approve", "p:1:a:n:m"), Button("Reject", "p:1:r:n:m")),)

    message_id = await api().send_message(CHAT, "<b>BUY</b> AAA", buttons)

    assert message_id == 77
    sent = form(route.calls.last.request)
    assert sent["chat_id"] == str(CHAT)
    assert sent["text"] == "<b>BUY</b> AAA"
    assert sent["parse_mode"] == "HTML"
    keyboard = json.loads(sent["reply_markup"])["inline_keyboard"]
    assert [[(b["text"], b["callback_data"]) for b in row] for row in keyboard] == [
        [("Approve", "p:1:a:n:m"), ("Reject", "p:1:r:n:m")]
    ]
    assert "disable_notification" not in sent or sent["disable_notification"] == "false"


@respx.mock
async def test_send_message_silent_and_without_buttons() -> None:
    route = respx.post(f"{BASE}/sendMessage").mock(return_value=ok(message_json(5)))

    await api().send_message(CHAT, "quiet", silent=True)

    sent = form(route.calls.last.request)
    assert sent["disable_notification"] == "true"
    assert "reply_markup" not in sent


@respx.mock
async def test_edit_message_edit_buttons_and_answer_callback() -> None:
    edit = respx.post(f"{BASE}/editMessageText").mock(return_value=ok(message_json(9)))
    markup = respx.post(f"{BASE}/editMessageReplyMarkup").mock(return_value=ok(message_json(9)))
    answer = respx.post(f"{BASE}/answerCallbackQuery").mock(return_value=ok(True))
    client = api()

    await client.edit_message(CHAT, 9, "<i>closed</i>")
    await client.edit_buttons(CHAT, 9, ())
    await client.answer_callback("cb1", "Approved")

    sent = form(edit.calls.last.request)
    assert (sent["chat_id"], sent["message_id"], sent["text"], sent["parse_mode"]) == (
        str(CHAT),
        "9",
        "<i>closed</i>",
        "HTML",
    )
    assert "reply_markup" not in sent  # editing without a keyboard removes the buttons
    assert "reply_markup" not in form(markup.calls.last.request)
    assert form(answer.calls.last.request) == {"callback_query_id": "cb1", "text": "Approved"}


# --- 2. getUpdates ----------------------------------------------------------------------------------


@respx.mock
async def test_get_updates_translates_callbacks_and_text() -> None:
    route = respx.post(f"{BASE}/getUpdates").mock(
        return_value=ok(
            [
                {
                    "update_id": 10,
                    "callback_query": {
                        "id": "cbq-1",
                        "from": {"id": 55, "is_bot": False, "first_name": "S"},
                        "chat_instance": "ci",
                        "data": "p:12:a:abcdefgh:mac",
                        "message": message_json(33),
                    },
                },
                {
                    "update_id": 11,
                    "message": {
                        **message_json(34, text="/status"),
                        "from": {"id": 55, "is_bot": False, "first_name": "S"},
                    },
                },
                {"update_id": 12, "edited_message": message_json(35, text="edited")},
            ]
        )
    )

    updates = await api().get_updates(offset=10, timeout=30)

    sent = form(route.calls.last.request)
    assert sent["offset"] == "10"
    assert sent["timeout"] == "30"
    assert json.loads(sent["allowed_updates"]) == ["message", "callback_query"]

    cb, text, other = updates
    assert cb.update_id == 10
    assert cb.text is None
    assert cb.chat_id == CHAT
    assert cb.from_id == 55
    assert cb.callback is not None
    assert (cb.callback.id, cb.callback.data, cb.callback.chat_id, cb.callback.from_id) == (
        "cbq-1",
        "p:12:a:abcdefgh:mac",
        CHAT,
        55,
    )
    assert cb.callback.message_id == 33

    assert (text.update_id, text.chat_id, text.from_id, text.text, text.callback) == (
        11,
        CHAT,
        55,
        "/status",
        None,
    )
    assert (other.update_id, other.text, other.callback) == (12, None, None)


@respx.mock
async def test_get_updates_without_offset() -> None:
    route = respx.post(f"{BASE}/getUpdates").mock(return_value=ok([]))

    assert await api().get_updates(offset=None, timeout=1) == []
    assert "offset" not in form(route.calls.last.request)


# --- 3. Telegram errors -----------------------------------------------------------------------------


@respx.mock
async def test_400_keeps_telegram_description() -> None:
    respx.post(f"{BASE}/editMessageText").mock(
        return_value=error(400, "Bad Request: message is not modified")
    )

    with pytest.raises(TelegramApiError) as exc:
        await api().edit_message(CHAT, 9, "same")

    assert exc.value.status == 400
    assert exc.value.description == "Bad Request: message is not modified"
    assert exc.value.retry_after is None


@respx.mock
async def test_429_carries_retry_after() -> None:
    respx.post(f"{BASE}/sendMessage").mock(
        return_value=error(429, "Too Many Requests: retry after 3", retry_after=3)
    )

    with pytest.raises(TelegramApiError) as exc:
        await api().send_message(CHAT, "x")

    assert exc.value.status == 429
    assert exc.value.retry_after == 3
    assert exc.value.description == "Too Many Requests: retry after 3"


@respx.mock
async def test_409_conflict() -> None:
    respx.post(f"{BASE}/getUpdates").mock(
        return_value=error(409, "Conflict: terminated by other getUpdates request")
    )

    with pytest.raises(TelegramApiError) as exc:
        await api().get_updates(offset=None, timeout=0)

    assert exc.value.status == 409
    assert exc.value.description.startswith("Conflict:")


@respx.mock
async def test_403_and_5xx() -> None:
    respx.post(f"{BASE}/sendMessage").mock(
        side_effect=[error(403, "Forbidden: bot was blocked by the user"), error(502, "Bad Gateway")]
    )
    client = api()

    with pytest.raises(TelegramApiError) as forbidden:
        await client.send_message(CHAT, "x")
    with pytest.raises(TelegramApiError) as gateway:
        await client.send_message(CHAT, "x")

    assert forbidden.value.status == 403
    # A server error is treated like a network error (retryable): status None, the code in the text.
    assert gateway.value.status is None
    assert "502" in gateway.value.description


# --- 4. transport errors never carry the token ------------------------------------------------------


@respx.mock
async def test_transport_error_is_status_none_without_token(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    respx.post(f"{BASE}/sendMessage").mock(
        side_effect=httpx.ConnectError(f"connection refused for {BASE}/sendMessage")
    )

    with capture_logs() as logs, pytest.raises(TelegramApiError) as exc:
        await api().send_message(CHAT, "x")

    err = exc.value
    assert err.status is None
    rendered = "".join(traceback.format_exception(err))
    for text in (str(err), repr(err), err.description, rendered, caplog.text, repr(logs)):
        assert TOKEN not in text
        assert TOKEN.split(":")[1] not in text


@respx.mock
async def test_timeout_is_status_none() -> None:
    respx.post(f"{BASE}/answerCallbackQuery").mock(side_effect=httpx.ReadTimeout("slow"))

    with pytest.raises(TelegramApiError) as exc:
        await api().answer_callback("cb1")

    assert exc.value.status is None
    assert TOKEN not in exc.value.description


class RaisingBot:
    """A stand-in for telegram.Bot that raises PTB's own exceptions."""

    def __init__(self, exc: Exception) -> None:
        self.exc = exc
        self.shut = False

    async def send_message(self, **_: Any) -> Any:
        raise self.exc

    async def shutdown(self) -> None:
        self.shut = True


@pytest.mark.parametrize(
    ("exc", "status", "retry_after"),
    [
        (telegram.error.RetryAfter(4), 429, 4.0),
        (telegram.error.BadRequest("Bad Request: chat not found"), 400, None),
        (telegram.error.Forbidden("Forbidden: bot was kicked"), 403, None),
        (telegram.error.Conflict("Conflict: other poller"), 409, None),
        (telegram.error.TimedOut(), None, None),
        (telegram.error.NetworkError(f"httpx.ConnectError: {BASE}"), None, None),
        (RuntimeError(f"boom {TOKEN}"), None, None),
    ],
)
async def test_ptb_exceptions_are_translated(
    exc: Exception, status: int | None, retry_after: float | None
) -> None:
    bot = RaisingBot(exc)
    client = PtbTelegramApi(SecretStr(TOKEN), bot=cast(telegram.Bot, bot))

    with pytest.raises(TelegramApiError) as caught:
        await client.send_message(CHAT, "x")

    assert caught.value.status == status
    assert caught.value.retry_after == retry_after
    assert TOKEN not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__

    await client.aclose()
    assert bot.shut


async def test_context_manager_closes_http_clients() -> None:
    async with PtbTelegramApi(SecretStr(TOKEN)) as client:
        assert isinstance(client, PtbTelegramApi)
    # Closing twice is harmless.
    await client.aclose()
