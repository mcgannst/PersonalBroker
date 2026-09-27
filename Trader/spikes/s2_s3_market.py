"""S2 (weekend part): quote fields, delay flag, extended-hours price. S3: candle limits and history depth."""
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import qt

ET = ZoneInfo("America/New_York")
c = qt.Client()

# --- symbol lookup ---------------------------------------------------------
names = ["AAPL", "SPY", "AMD", "RY.TO", "SHOP.TO"]
syms = c.get("symbols", {"names": ",".join(names)})["symbols"]
ids = {s["symbol"]: s["symbolId"] for s in syms}
print("symbol ids:", ids)
print("symbol fields:", sorted(syms[0].keys()))
search = c.get("symbols/search", {"prefix": "NVD"})["symbols"][:3]
print("search 'NVD':", [(s["symbol"], s["listingExchange"]) for s in search])

# --- S2 weekend part: quote fields -------------------------------------------
q = c.get("markets/quotes", {"ids": ",".join(str(i) for i in ids.values())})["quotes"]
print("\nquote fields:", sorted(q[0].keys()))
for x in q:
    print(f"  {x['symbol']:8} last={x.get('lastTradePrice')} lastRegHrs={x.get('lastTradePriceTrHrs')} "
          f"bid={x.get('bidPrice')} ask={x.get('askPrice')} vol={x.get('volume')} "
          f"lastTradeTime={x.get('lastTradeTime')} delay={x.get('delay')} halted={x.get('isHalted')} "
          f"high52={x.get('high52w')}")
print("quote rate-limit headers:", c.last_headers)

# --- S3: candles per request ---------------------------------------------
aapl = ids["AAPL"]


def candles(sym_id, start, end, interval):
    fmt = lambda d: d.isoformat(timespec="seconds")
    return c.get(f"markets/candles/{sym_id}",
                 {"startTime": fmt(start), "endTime": fmt(end), "interval": interval})["candles"]


friday = datetime(2026, 9, 25, 9, 30, tzinfo=ET)
print("\n9:30-9:35 5-min bar Fri 25 Sep:",
      candles(aapl, friday, friday + timedelta(minutes=5), "FiveMinutes"))

print("\nmax candles per request (OneMinute, growing windows):")
for days in (1, 3, 7, 14, 30):
    end = datetime(2026, 9, 26, 0, 0, tzinfo=ET)
    try:
        n = candles(aapl, end - timedelta(days=days), end, "OneMinute")
        span = f"{n[0]['start']} .. {n[-1]['end']}" if n else "none"
        ext = sum(1 for k in n if not ("09:30" <= k["start"][11:16] < "16:00"))
        print(f"  {days:>2} days -> {len(n)} candles ({span}); outside 9:30-16:00: {ext}")
    except RuntimeError as e:
        print(f"  {days:>2} days -> error {str(e)[:160]}")

for interval, days in (("FiveMinutes", 30), ("FiveMinutes", 90), ("OneDay", 3650)):
    end = datetime(2026, 9, 26, 0, 0, tzinfo=ET)
    try:
        n = candles(aapl, end - timedelta(days=days), end, interval)
        print(f"  {interval} {days} days -> {len(n)} candles ({n[0]['start'] if n else '-'} .. {n[-1]['end'] if n else '-'})")
    except RuntimeError as e:
        print(f"  {interval} {days} days -> error {str(e)[:160]}")

# --- S3: history depth (single trading-day windows, going back) -------------
print("\nhistory depth (one weekday each):")
today = datetime(2026, 9, 25, tzinfo=ET)
for interval in ("OneMinute", "FiveMinutes"):
    for back in (30, 90, 180, 365, 730, 1095, 1825):
        d = today - timedelta(days=back)
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        start = d.replace(hour=9, minute=30)
        try:
            n = candles(aapl, start, start + timedelta(hours=1), interval)
            print(f"  {interval:11} {back:>4} days back ({d.date()}): {len(n)} candles")
        except RuntimeError as e:
            print(f"  {interval:11} {back:>4} days back ({d.date()}): error {str(e)[:120]}")
print("final rate-limit headers:", c.last_headers)
