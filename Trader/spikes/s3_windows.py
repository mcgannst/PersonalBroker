"""S3: which start/end combinations work for old intraday data."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import qt

ET = ZoneInfo("America/New_York")
c = qt.Client()
aapl = c.get("symbols", {"names": "AAPL"})["symbols"][0]["symbolId"]
now = datetime(2026, 9, 26, 0, 0, tzinfo=ET)

cases = [
    ("FiveMinutes", 100, 0), ("FiveMinutes", 200, 0), ("FiveMinutes", 400, 0),
    ("FiveMinutes", 100, 50), ("FiveMinutes", 100, 70), ("FiveMinutes", 75, 62),
    ("OneMinute", 100, 0), ("OneMinute", 200, 0), ("OneMinute", 90, 59), ("OneMinute", 90, 61),
]
for interval, start_back, end_back in cases:
    start, end = now - timedelta(days=start_back), now - timedelta(days=end_back)
    try:
        n = c.get(f"markets/candles/{aapl}", {"startTime": start.isoformat(), "endTime": end.isoformat(), "interval": interval})["candles"]
        span = f"{n[0]['start'][:10]} .. {n[-1]['start'][:10]}" if n else "empty"
        print(f"{interval:11} start -{start_back:>3}d end -{end_back:>2}d: {len(n):>6} candles  {span}")
    except RuntimeError as e:
        print(f"{interval:11} start -{start_back:>3}d end -{end_back:>2}d: ERROR {str(e)[-60:]}")
print("rate-limit headers:", c.last_headers)
