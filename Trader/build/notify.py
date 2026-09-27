"""Send a Telegram message to Stephen via the dev bot. Used by the build orchestrator
until `trader notify` exists (P1-T9). Usage: python3 build/notify.py "text"."""

import json
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ENV = (Path(__file__).resolve().parents[1] / "docker" / ".env.dev").read_text()


def _get(key: str) -> str:
    m = re.search(rf"^{key}=(\S+)$", ENV, re.M)
    if not m:
        raise SystemExit(f"{key} missing from .env.dev")
    return m.group(1)


def main() -> None:
    text = " ".join(sys.argv[1:]) or "(empty)"
    data = urllib.parse.urlencode({"chat_id": _get("TELEGRAM_CHAT_ID"), "text": f"🛠 Trader build: {text}"})
    url = f"https://api.telegram.org/bot{_get('TELEGRAM_BOT_TOKEN')}/sendMessage"
    with urllib.request.urlopen(url, data=data.encode(), timeout=15) as r:  # noqa: S310
        print("sent" if json.loads(r.read()).get("ok") else "failed")


if __name__ == "__main__":
    main()
