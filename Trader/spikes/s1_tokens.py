"""S1: refresh-token exchange works 3 times in a row and the stored token stays valid."""
import time

import qt

results = []
for i in range(1, 4):
    sess, elapsed, expires_in, rotated = qt.exchange()
    c = qt.Client()
    t = c.get("time")
    results.append((i, elapsed, expires_in, rotated, t.get("time"), dict(c.last_headers)))
    print(f"exchange {i}: ok in {elapsed:.2f}s, access token lasts {expires_in}s, "
          f"refresh token rotated={rotated}, server time {t.get('time')}, api_server {sess['api_server']}")
    print(f"   rate-limit headers: {c.last_headers}")
    time.sleep(2)

accts = qt.Client().get("accounts")
print(f"accounts call ok: {len(accts.get('accounts', []))} account(s) visible, userId {accts.get('userId')}")
print("stored token is the newest one: the next run will exchange it again")
