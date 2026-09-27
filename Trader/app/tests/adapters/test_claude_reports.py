"""P5-T9: the weekly commentary's Claude call (SPEC §4.3 "Weekly report"): a fake client, no network."""

import json
import re
from collections.abc import Sequence
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from trader.adapters.claude.catalyst import cost_usd
from trader.adapters.claude.reports import (
    COMMENTARY_SYSTEM_PROMPT,
    MAX_COMMENTARY_TOKENS,
    CommentaryWriter,
)
from trader.settings_store import RuntimeSettings

FACTS: dict[str, Any] = {
    "week": {"start": "2026-11-23", "end": "2026-11-27", "sessions": 4},
    "week_metrics": {"trades": 4, "win_rate": "0.5000", "total_pnl": "5.0000"},
    "best_trade": {"ticker": "AAA", "date": "2026-11-23", "pnl": "10.0000", "pnl_r": "1.0000"},
}
API_KEY = "sk-ant-api03-DoNotLeakThisKey0123456789abcdef"


def reply(
    text: str = "A calm week.", *, tin: int = 3000, tout: int = 400, stop: str = "end_turn", blocks: int = 1
) -> Any:
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)] * blocks,
        usage=SimpleNamespace(input_tokens=tin, output_tokens=tout),
        stop_reason=stop,
    )


class FakeMessages:
    def __init__(self, replies: Sequence[Any]) -> None:
        self.replies = list(replies)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class FakeClient:
    def __init__(self, *replies: Any) -> None:
        self.messages = FakeMessages(replies)
        self.api_key = API_KEY


def writer(client: Any, **settings: Any) -> CommentaryWriter:
    s = RuntimeSettings(**settings)
    return CommentaryWriter(client, lambda: s)


def user_text(call: dict[str, Any]) -> str:
    (msg,) = call["messages"]
    assert msg["role"] == "user"
    return str(msg["content"])


# --- 9. the prompt ------------------------------------------------------------------------------------------
async def test_prompt_carries_the_facts_and_the_numbers_rule() -> None:
    client = FakeClient(reply("The week went well."))
    c = await writer(client, claude_model="claude-haiku-4-5").write(FACTS)
    (call,) = client.messages.calls
    assert call["model"] == "claude-haiku-4-5"
    assert call["max_tokens"] == MAX_COMMENTARY_TOKENS == 900
    assert call["thinking"] == {"type": "disabled"}
    assert call["system"] == COMMENTARY_SYSTEM_PROMPT
    assert "ONLY numbers that appear in the facts" in COMMENTARY_SYSTEM_PROMPT
    assert "Never compute a new number" in COMMENTARY_SYSTEM_PROMPT
    assert "output_config" not in call  # plain text
    text = user_text(call)
    block = re.search(r"<facts>\n(.*)\n</facts>", text, re.DOTALL)
    assert block is not None and json.loads(block.group(1)) == FACTS
    assert "must not" not in text  # no avoid list on a first call
    assert c.status == "ok" and c.text == "The week went well." and c.error is None
    assert c.model == "claude-haiku-4-5"
    assert (c.input_tokens, c.output_tokens) == (3000, 400)
    assert c.cost_usd == cost_usd("claude-haiku-4-5", 3000, 400) == Decimal("0.005000")


async def test_avoid_lists_the_offending_numbers() -> None:
    client = FakeClient(reply())
    await writer(client).write(FACTS, avoid=["7", "12.34"])
    text = user_text(client.messages.calls[0])
    assert "7, 12.34" in text and "must not" in text
    assert client.messages.calls[0]["model"] == "claude-sonnet-5"


async def test_text_blocks_are_joined_and_trimmed() -> None:
    client = FakeClient(reply("  Part one. ", blocks=2))
    c = await writer(client).write(FACTS)
    assert c.text == ("  Part one. " * 2).strip()


# --- 7. errors ----------------------------------------------------------------------------------------------
async def test_sdk_error_is_one_line_without_the_key() -> None:
    boom = RuntimeError(f"connection failed\nheaders: x-api-key={API_KEY}\nretry later")
    client = FakeClient(boom)
    c = await writer(client).write(FACTS)
    assert c.status == "error" and c.text is None
    assert c.error is not None and "\n" not in c.error
    assert API_KEY not in c.error and "sk-ant" not in c.error
    assert c.error.startswith("RuntimeError: connection failed")
    assert c.cost_usd == Decimal(0) and c.input_tokens == 0


async def test_stop_reason_other_than_end_turn_is_an_error_with_its_cost() -> None:
    client = FakeClient(reply("cut off mid", tin=2000, tout=900, stop="max_tokens"))
    c = await writer(client).write(FACTS)
    assert c.status == "error" and c.text is None
    assert c.error == "stop_reason=max_tokens"
    assert c.cost_usd == cost_usd("claude-sonnet-5", 2000, 900)


async def test_empty_reply_is_an_error() -> None:
    client = FakeClient(reply("   "))
    c = await writer(client).write(FACTS)
    assert c.status == "error" and c.error == "empty reply"
