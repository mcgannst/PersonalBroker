"""P3-T6: signed, single-use callback data (SPEC §14) and the DB-backed nonce issuer."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from trader.adapters.telegram.callbacks import CallbackSigner, DbCallbackIssuer, ParsedCallback
from trader.adapters.telegram.types import CallbackIssuer
from trader.db import models as m
from trader.market.clock import FixedClock

T = datetime(2026, 10, 6, 13, 40, tzinfo=UTC)
CHAT = 4242
SIGNER = CallbackSigner.derive("test-session-secret")


def _flip(ch: str) -> str:
    return "B" if ch == "A" else "A"


# --- CallbackSigner (no DB) ----------
def test_derive_is_deterministic_and_secret_dependent() -> None:
    a = CallbackSigner.derive("s1").data("p", "12", "a", "abcdefgh")
    b = CallbackSigner.derive("s1").data("p", "12", "a", "abcdefgh")
    c = CallbackSigner.derive("s2").data("p", "12", "a", "abcdefgh")
    assert a == b and a != c
    assert a.startswith("p:12:a:abcdefgh:") and len(a.rsplit(":", 1)[1]) == 16


def test_parse_round_trips_every_kind() -> None:
    cases = [("p", "12", "a", "proposal"), ("p", "12", "r", "proposal"), ("s", "3", "y", "pause")]
    cases += [("j", "20261006", "n", "journal")]
    for code, ref, action, kind in cases:
        parsed = SIGNER.parse(SIGNER.data(code, ref, action, "abc-_xyz"))
        assert parsed == ParsedCallback(kind, ref, action, "abc-_xyz")  # type: ignore[arg-type]


def test_a_changed_mac_or_field_is_refused() -> None:
    data = SIGNER.data("p", "12", "a", "abcdefgh")
    body, mac = data.rsplit(":", 1)
    assert SIGNER.parse(f"{body}:{mac[:-1]}{_flip(mac[-1])}") is None  # one MAC character changed
    assert SIGNER.parse(data.replace("p:12:", "p:13:", 1)) is None  # proposal id changed
    assert SIGNER.parse(data.replace(":a:", ":r:", 1)) is None  # action changed
    assert CallbackSigner.derive("other").parse(data) is None  # signed with another secret


@pytest.mark.parametrize(
    "data",
    ["", "garbage", "p:12:a:abcdefgh", "p:12:a:abcdefgh:x:y", "x:12:a:abcdefgh:0123456789abcdef"],
)
def test_malformed_data_is_refused(data: str) -> None:
    assert SIGNER.parse(data) is None


def test_an_action_or_ref_outside_the_kind_is_refused_even_when_signed() -> None:
    assert SIGNER.parse(SIGNER.data("p", "12", "y", "abcdefgh")) is None
    assert SIGNER.parse(SIGNER.data("j", "2026-10-06", "y", "abcdefgh")) is None
    assert SIGNER.parse(SIGNER.data("p", "abc", "a", "abcdefgh")) is None


def test_every_data_string_fits_telegrams_64_bytes() -> None:
    """Acceptance 14: a 10-digit proposal id still fits."""
    for code, ref, action in [("p", "9999999999", "a"), ("s", "9999999999", "y"), ("j", "20261006", "n")]:
        assert len(SIGNER.data(code, ref, action, "abcdefgh").encode()) <= 64


# --- DbCallbackIssuer ----------
@pytest.fixture
def issuer(db_factory: sessionmaker[Session]) -> DbCallbackIssuer:
    return DbCallbackIssuer(db_factory, FixedClock(T), SIGNER)


def _row(factory: sessionmaker[Session], nonce: str) -> m.TelegramCallback:
    with factory() as s:
        row = s.execute(select(m.TelegramCallback).where(m.TelegramCallback.nonce == nonce)).scalar_one()
        return row


@pytest.mark.db
def test_issue_writes_one_nonce_shared_by_the_buttons(
    issuer: DbCallbackIssuer, db_factory: sessionmaker[Session]
) -> None:
    as_protocol: CallbackIssuer = issuer
    nonce, data = as_protocol.issue("proposal", "12", ["a", "r"], CHAT, None)
    assert len(nonce) == 8 and set(data) == {"a", "r"}
    for action, d in data.items():
        assert SIGNER.parse(d) == ParsedCallback("proposal", "12", action, nonce)
    row = _row(db_factory, nonce)
    assert (row.kind, row.ref, row.chat_id, row.message_id) == ("proposal", "12", CHAT, None)
    assert row.created_at == T and row.expires_at is None and row.used_at is None
    nonce2, _ = issuer.issue("pause", "1", ["y", "n"], CHAT, 60)
    assert nonce2 != nonce and _row(db_factory, nonce2).expires_at == T + timedelta(seconds=60)


@pytest.mark.db
def test_claim_is_single_use(issuer: DbCallbackIssuer, db_factory: sessionmaker[Session]) -> None:
    nonce, _ = issuer.issue("proposal", "12", ["a", "r"], CHAT, None)
    issuer.bind(nonce, 7)
    assert issuer.claim(ParsedCallback("proposal", "12", "a", nonce), CHAT, 7) == "ok"
    assert issuer.claim(ParsedCallback("proposal", "12", "r", nonce), CHAT, 7) == "used"
    row = _row(db_factory, nonce)
    assert row.used_at == T and row.used_action == "a" and row.message_id == 7


@pytest.mark.db
def test_claim_results(issuer: DbCallbackIssuer) -> None:
    assert issuer.claim(ParsedCallback("proposal", "12", "a", "zzzzzzzz"), CHAT, 1) == "unknown"
    nonce, _ = issuer.issue("proposal", "12", ["a", "r"], CHAT, None)
    assert issuer.claim(ParsedCallback("proposal", "13", "a", nonce), CHAT, 1) == "unknown"  # other ref
    assert issuer.claim(ParsedCallback("pause", "12", "y", nonce), CHAT, 1) == "unknown"  # other kind
    assert issuer.claim(ParsedCallback("proposal", "12", "a", nonce), CHAT + 1, 1) == "unknown"  # other chat
    issuer.bind(nonce, 7)
    assert issuer.claim(ParsedCallback("proposal", "12", "a", nonce), CHAT, 8) == "wrong_message"
    assert issuer.claim(ParsedCallback("proposal", "12", "a", nonce), CHAT, 7) == "ok"


@pytest.mark.db
def test_an_unbound_nonce_skips_the_message_check(issuer: DbCallbackIssuer) -> None:
    nonce, _ = issuer.issue("journal", "20261006", ["y", "n"], CHAT, None)
    assert issuer.claim(ParsedCallback("journal", "20261006", "y", nonce), CHAT, 99) == "ok"


@pytest.mark.db
def test_expiry(db_factory: sessionmaker[Session]) -> None:
    clock = FixedClock(T)
    issuer = DbCallbackIssuer(db_factory, clock, SIGNER)
    nonce, _ = issuer.issue("pause", "1", ["y", "n"], CHAT, 60)
    clock.advance(timedelta(seconds=60))
    assert issuer.claim(ParsedCallback("pause", "1", "y", nonce), CHAT, None) == "expired"
    no_ttl, _ = issuer.issue("proposal", "5", ["a", "r"], CHAT, None)
    clock.advance(timedelta(days=3))
    assert issuer.claim(ParsedCallback("proposal", "5", "a", no_ttl), CHAT, None) == "ok"


@pytest.mark.db
def test_release_makes_the_nonce_usable_again(
    issuer: DbCallbackIssuer, db_factory: sessionmaker[Session]
) -> None:
    nonce, _ = issuer.issue("proposal", "12", ["a", "r"], CHAT, None)
    parsed = ParsedCallback("proposal", "12", "a", nonce)
    assert issuer.claim(parsed, CHAT, None) == "ok"
    issuer.release(nonce)
    row = _row(db_factory, nonce)
    assert row.used_at is None and row.used_action is None
    assert issuer.claim(parsed, CHAT, None) == "ok"
