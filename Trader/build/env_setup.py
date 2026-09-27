"""Idempotently align Trader/docker/.env.dev with SPEC §13. Prints key names only, never values."""

import base64
import os
import re
import secrets
import tempfile
from pathlib import Path

ENV = Path(__file__).resolve().parents[1] / "docker" / ".env.dev"


def main() -> None:
    text = ENV.read_text()
    changed: list[str] = []
    if re.search(r"^DATABASE_OWNER_URL=", text, re.M) and not re.search(r"^MIGRATION_DATABASE_URL=", text, re.M):
        text = re.sub(r"^DATABASE_OWNER_URL=", "MIGRATION_DATABASE_URL=", text, flags=re.M)
        changed.append("MIGRATION_DATABASE_URL (renamed)")
    if not re.search(r"^APP_ENCRYPTION_KEY=", text, re.M):
        key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
        text += f"\n# Fernet key for tokens at rest. Added by build/env_setup.py.\nAPP_ENCRYPTION_KEY={key}\n"
        changed.append("APP_ENCRYPTION_KEY")
    if not re.search(r"^SESSION_SECRET=", text, re.M):
        text += f"SESSION_SECRET={secrets.token_urlsafe(48)}\n"
        changed.append("SESSION_SECRET")
    if changed:
        fd, tmp = tempfile.mkstemp(dir=ENV.parent, prefix=".env.dev.")
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, ENV)
    print("changed:", ", ".join(changed) or "nothing")


if __name__ == "__main__":
    main()
