"""Idempotently align Trader/docker/.env.dev with SPEC §13. Prints key names only, never values."""

import base64
import os
import re
import secrets
import tempfile
from pathlib import Path

ENV = Path(__file__).resolve().parents[1] / "docker" / ".env.dev"


def _ensure_newline(text: str) -> str:
    """Never glue an appended key onto a last line saved without a trailing newline."""
    return text if not text or text.endswith("\n") else text + "\n"


def main() -> None:
    text = ENV.read_text()
    changed: list[str] = []
    if re.search(r"^DATABASE_OWNER_URL=", text, re.M) and not re.search(r"^MIGRATION_DATABASE_URL=", text, re.M):
        text = re.sub(r"^DATABASE_OWNER_URL=", "MIGRATION_DATABASE_URL=", text, flags=re.M)
        changed.append("MIGRATION_DATABASE_URL (renamed)")
    if not re.search(r"^APP_ENCRYPTION_KEY=", text, re.M):
        key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
        text = _ensure_newline(text)
        text += f"\n# Fernet key for tokens at rest. Added by build/env_setup.py.\nAPP_ENCRYPTION_KEY={key}\n"
        changed.append("APP_ENCRYPTION_KEY")
    if not re.search(r"^SESSION_SECRET=", text, re.M):
        text = _ensure_newline(text)
        text += f"SESSION_SECRET={secrets.token_urlsafe(48)}\n"
        changed.append("SESSION_SECRET")
    if changed:
        fd, tmp = tempfile.mkstemp(dir=ENV.parent, prefix=".env.dev.")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(text)
            os.chmod(tmp, 0o600)
            os.replace(tmp, ENV)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    print("changed:", ", ".join(changed) or "nothing")


if __name__ == "__main__":
    main()
