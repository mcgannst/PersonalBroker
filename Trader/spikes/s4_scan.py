"""S4: fetch the 9:30-9:35 five-minute bar for the whole universe, rate-limited, target < 60 s.

Symbol IDs are resolved first (the nightly job's job), then only the timed
candle fetch counts. Uses Friday 2026-09-25 as the session.
"""
import csv
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import qt

ET = ZoneInfo("America/New_York")
UNIVERSE = "/private/tmp/claude-501/-Users-stephen-Documents-Code-Claude-Code-Trader/f42f487e-1875-4270-ba6e-1460a134bfce/scratchpad/s5/universe.csv"
RATE = 20  # documented market-data limit per second

tickers = [r["ticker"] for r in csv.DictReader(open(UNIVERSE))]
c = qt.Client()

# --- nightly part: symbol IDs --------------------------------------------------
t = time.monotonic()
ids, missing = {}, []
for i in range(0, len(tickers), 100):
    chunk = tickers[i:i + 100]
    try:
        for s in c.get("symbols", {"names": ",".join(chunk)})["symbols"]:
            ids[s["symbol"]] = s["symbolId"]
    except RuntimeError:
        for tk in chunk:  # fall back one by one to find the bad name
            try:
                s = c.get("symbols", {"names": tk})["symbols"]
                ids[tk] = s[0]["symbolId"]
            except RuntimeError:
                missing.append(tk)
missing += [tk for tk in tickers if tk not in ids and tk not in missing]
print(f"symbol IDs: {len(ids)}/{len(tickers)} resolved in {time.monotonic() - t:.1f}s; unresolved: {missing[:15]}")

# --- timed part: 9:30-9:35 bars --------------------------------------------------
start = datetime(2026, 9, 25, 9, 30, tzinfo=ET)
params = {"startTime": start.isoformat(), "endTime": (start + timedelta(minutes=5)).isoformat(), "interval": "FiveMinutes"}

lock = threading.Lock()
next_slot = [time.monotonic()]
throttled = [0]


def take_slot():
    with lock:
        now = time.monotonic()
        slot = max(now, next_slot[0])
        next_slot[0] = slot + 1.0 / RATE
    time.sleep(max(0.0, slot - time.monotonic()))


def fetch(item):
    tk, sid = item
    take_slot()
    try:
        bars = c.get(f"markets/candles/{sid}", params)["candles"]
        return tk, bars[0] if bars else None, None
    except RuntimeError as e:
        return tk, None, str(e)[:80]


t0 = time.monotonic()
with ThreadPoolExecutor(max_workers=12) as pool:
    results = list(pool.map(fetch, ids.items()))
elapsed = time.monotonic() - t0

got = [r for r in results if r[1]]
empty = [r[0] for r in results if not r[1] and not r[2]]
errors = [(r[0], r[2]) for r in results if r[2]]
print(f"candle fetch: {len(results)} symbols in {elapsed:.1f}s ({len(results) / elapsed:.1f} req/s)")
print(f"  bars returned: {len(got)}, no bar: {len(empty)} {empty[:10]}, errors: {len(errors)} {errors[:3]}")
print(f"  rate-limit headers after: {c.last_headers}")
print(f"S4 {'PASS' if elapsed < 60 and not errors else 'FAIL'}: {elapsed:.1f}s for {len(results)} symbols (target < 60 s)")
print(f"  headroom: at {RATE} req/s the 60 s budget covers ~{RATE * 60} symbols")
