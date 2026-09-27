"""Minimal Questrade client for the Phase 0 spikes.

Token rule: every refresh-token exchange invalidates the old token, so the new
refresh token is written to Trader/docker/.env.dev (atomically) BEFORE the
access token is used for anything.
"""
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timezone

import requests

REPO = "/Users/stephen/Documents/Code/Claude Code/Trader"
ENV_PATH = f"{REPO}/Trader/docker/.env.dev"
SESSION_PATH = os.path.join(os.path.dirname(__file__), ".qt_session.json")  # access token cache, 0600
LOGIN_URL = "https://login.questrade.com/oauth2/token"


def _read_env():
    return open(ENV_PATH).read()


def current_refresh_token():
    env = _read_env()
    m = re.search(r"^QUESTRADE_REFRESH_TOKEN=(\S+)$", env, re.M) or \
        re.search(r"^QUESTRADE_INITIAL_REFRESH_TOKEN=(\S+)$", env, re.M)
    if not m:
        raise SystemExit("no Questrade refresh token in .env.dev")
    return m.group(1)


def _save_refresh_token(new_token):
    env = _read_env()
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"QUESTRADE_REFRESH_TOKEN={new_token}"
    comment = f"# Questrade Trader-dev refresh token, rotated {stamp} (single use; always save the newest)"
    if re.search(r"^QUESTRADE_REFRESH_TOKEN=", env, re.M):
        env = re.sub(r"^# Questrade Trader-dev refresh token, rotated .*$", comment, env, flags=re.M)
        env = re.sub(r"^QUESTRADE_REFRESH_TOKEN=\S+$", line, env, flags=re.M)
    else:
        # First rotation: the initial token is now spent; replace it
        env = re.sub(r"^QUESTRADE_INITIAL_REFRESH_TOKEN=\S+$", comment + "\n" + line, env, flags=re.M)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(ENV_PATH), prefix=".env.dev.")
    with os.fdopen(fd, "w") as f:
        f.write(env)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, ENV_PATH)
    # Verify
    if current_refresh_token() != new_token:
        raise RuntimeError("refresh token save could not be verified")


def exchange():
    """Exchange the stored refresh token. Returns (session dict, elapsed seconds)."""
    old = current_refresh_token()
    t0 = time.monotonic()
    r = requests.get(LOGIN_URL, params={"grant_type": "refresh_token", "refresh_token": old}, timeout=20)
    elapsed = time.monotonic() - t0
    if r.status_code != 200:
        raise SystemExit(f"token exchange failed: HTTP {r.status_code} {r.text[:200]}")
    data = r.json()
    try:
        _save_refresh_token(data["refresh_token"])
    except Exception as exc:  # last resort so the chain isn't lost
        sys.stderr.write(f"CRITICAL: could not save new refresh token ({exc}). NEW TOKEN: {data['refresh_token']}\n")
        raise
    session = {
        "access_token": data["access_token"],
        "api_server": data["api_server"].rstrip("/") + "/",
        "expires_at": time.time() + int(data.get("expires_in", 1800)),
        "obtained_at": time.time(),
    }
    fd = os.open(SESSION_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(session, f)
    return session, elapsed, int(data.get("expires_in", 0)), old != data["refresh_token"]


def session():
    """Cached access token if it has > 2 min left, else a fresh exchange."""
    if os.path.exists(SESSION_PATH):
        s = json.load(open(SESSION_PATH))
        if s["expires_at"] - time.time() > 120:
            return s
    return exchange()[0]


class Client:
    def __init__(self):
        self.s = session()
        self.http = requests.Session()
        self.http.headers["Authorization"] = f"Bearer {self.s['access_token']}"
        self.last_headers = {}

    def get(self, path, params=None):
        url = self.s["api_server"] + "v1/" + path.lstrip("/")
        for attempt in range(5):
            r = self.http.get(url, params=params or {}, timeout=30)
            self.last_headers = {k: v for k, v in r.headers.items() if k.lower().startswith("x-ratelimit")}
            if r.status_code == 429:
                reset = int(r.headers.get("X-RateLimit-Reset", time.time() + 1))
                time.sleep(max(0.5, min(5, reset - time.time())))
                continue
            if r.status_code >= 500:
                time.sleep(0.5 * 2 ** attempt)
                continue
            if r.status_code != 200:
                raise RuntimeError(f"GET {path} -> HTTP {r.status_code}: {r.text[:300]}")
            return r.json()
        raise RuntimeError(f"GET {path} kept failing (last HTTP {r.status_code})")
