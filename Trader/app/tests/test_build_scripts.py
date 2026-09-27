"""Unit tests for the build helper scripts in Trader/build (no network, fake env files only)."""

import email.message
import importlib.util
import io
import json
import urllib.error
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

BUILD_DIR = Path(__file__).resolve().parents[2] / "build"
FAKE_TOKEN = "123456:fake-bot-token-notify-9x"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"build_{name}_under_test", BUILD_DIR / f"{name}.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------- notify.py


class _FakeResponse(io.BytesIO):
    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


@pytest.fixture
def notify(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    env_file = tmp_path / ".env.dev"
    env_file.write_text(f"TELEGRAM_BOT_TOKEN={FAKE_TOKEN}\nTELEGRAM_CHAT_ID=42\n")
    module = _load("notify")
    monkeypatch.setattr(module, "ENV", env_file)
    monkeypatch.setattr("sys.argv", ["notify.py", "hello"])
    return module


def _reply(monkeypatch: pytest.MonkeyPatch, module: ModuleType, payload: dict[str, Any]) -> list[str]:
    calls: list[str] = []

    def fake_urlopen(url: str, data: bytes, timeout: float) -> _FakeResponse:
        calls.append(url)
        return _FakeResponse(json.dumps(payload).encode())

    monkeypatch.setattr(module.urllib.request, "urlopen", fake_urlopen)
    return calls


def _raise(monkeypatch: pytest.MonkeyPatch, module: ModuleType, exc: Exception) -> None:
    def fake_urlopen(url: str, data: bytes, timeout: float) -> _FakeResponse:
        raise exc

    monkeypatch.setattr(module.urllib.request, "urlopen", fake_urlopen)


def test_notify_import_does_not_read_env_file() -> None:
    # Reading .env.dev at import time crashed any importer in a worktree without it.
    module = _load("notify")
    assert isinstance(module.ENV, Path)


def test_notify_sends_and_prints_sent(
    notify: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = _reply(monkeypatch, notify, {"ok": True})
    notify.main()
    assert capsys.readouterr().out.strip() == "sent"
    assert len(calls) == 1


def test_notify_exits_nonzero_when_telegram_replies_not_ok(
    notify: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _reply(monkeypatch, notify, {"ok": False, "description": "Bad Request: chat not found"})
    with pytest.raises(SystemExit) as exc:
        notify.main()
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert out.startswith("failed:")
    assert FAKE_TOKEN not in out


def test_notify_http_error_is_clean_and_hides_token(
    notify: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    url = f"https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage"
    error = urllib.error.HTTPError(url, 401, "Unauthorized", email.message.Message(), None)
    _raise(monkeypatch, notify, error)
    with pytest.raises(SystemExit) as exc:
        notify.main()
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert captured.out.startswith("failed:")
    assert "401" in captured.out
    assert FAKE_TOKEN not in captured.out + captured.err
    assert "api.telegram.org" not in captured.out + captured.err


def test_notify_url_error_is_clean_and_hides_token(
    notify: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _raise(monkeypatch, notify, urllib.error.URLError(OSError(f"cannot reach bot{FAKE_TOKEN}")))
    with pytest.raises(SystemExit) as exc:
        notify.main()
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert captured.out.startswith("failed:")
    assert FAKE_TOKEN not in captured.out + captured.err


def test_notify_missing_env_file_gives_clear_message(
    notify: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(notify, "ENV", tmp_path / "missing" / ".env.dev")
    with pytest.raises(SystemExit) as exc:
        notify.main()
    assert isinstance(exc.value.code, str)
    assert "not found" in exc.value.code


def test_notify_missing_key_gives_clear_message(notify: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    notify.ENV.write_text("TELEGRAM_CHAT_ID=42\n")
    with pytest.raises(SystemExit) as exc:
        notify.main()
    assert exc.value.code == "failed: TELEGRAM_BOT_TOKEN missing from .env.dev"


# ---------------------------------------------------------------- env_setup.py


def test_env_setup_removes_temp_file_when_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    env_file = tmp_path / ".env.dev"
    original = "DATABASE_URL=postgresql://a:b@db/x\n"
    env_file.write_text(original)
    module = _load("env_setup")
    monkeypatch.setattr(module, "ENV", env_file)

    def boom(src: str, dst: Path) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(module.os, "replace", boom)
    with pytest.raises(OSError, match="disk full"):
        module.main()
    assert sorted(p.name for p in tmp_path.iterdir()) == [".env.dev"]
    assert env_file.read_text() == original
