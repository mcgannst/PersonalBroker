"""Send a Telegram message to Stephen via the dev bot. Used by the build orchestrator
until `trader notify` exists (P1-T9). Usage: python3 build/notify.py "text".

Exits 0 when Telegram accepted the message, 1 otherwise. Never prints the bot token
or the request URL (which contains it)."""

import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ENV = Path(__file__).resolve().parents[1] / "docker" / ".env.dev"


def _get(env_text: str, key: str) -> str:
    m = re.search(rf"^{key}=(\S+)$", env_text, re.M)
    if not m:
        raise SystemExit(f"failed: {key} missing from .env.dev")
    return m.group(1)


def main() -> None:
    try:
        env_text = ENV.read_text()
    except FileNotFoundError:
        raise SystemExit(f"failed: {ENV} not found (it lives only in the main checkout)") from None
    text = " ".join(sys.argv[1:]) or "(empty)"
    chat_id = _get(env_text, "TELEGRAM_CHAT_ID")
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": f"🛠 Trader build: {text}"})
    url = f"https://api.telegram.org/bot{_get(env_text, 'TELEGRAM_BOT_TOKEN')}/sendMessage"
    try:
        with urllib.request.urlopen(url, data=data.encode(), timeout=15) as r:  # noqa: S310
            reply = json.loads(r.read())
    except urllib.error.HTTPError as e:
        # str(e) and e.url would include the token-bearing URL, so report only the status.
        print(f"failed: HTTP {e.code} from Telegram")
        raise SystemExit(1) from None
    except urllib.error.URLError as e:
        print(f"failed: could not reach Telegram ({type(e.reason).__name__})")
        raise SystemExit(1) from None
    if not reply.get("ok"):
        print(f"failed: Telegram replied ok=false ({reply.get('description', 'no description')})")
        raise SystemExit(1)
    print("sent")


if __name__ == "__main__":
    main()
