from cryptography.fernet import Fernet

from trader.crypto import Crypto


def test_round_trip() -> None:
    c = Crypto(Fernet.generate_key().decode())
    token = c.encrypt("refresh-abc")
    assert token != "refresh-abc"
    assert c.decrypt(token) == "refresh-abc"


def test_wrong_key_or_garbage_returns_none() -> None:
    a, b = Crypto(Fernet.generate_key().decode()), Crypto(Fernet.generate_key().decode())
    assert b.decrypt(a.encrypt("x")) is None
    assert a.decrypt("not-a-token") is None
    assert a.decrypt(None) is None
    assert a.decrypt("") is None
