"""S6: Telegram inline approval round-trip: button -> callback -> DB update, target < 2 s.

Sends a test proposal with Approve/Reject buttons, long-polls for the tap, then
times: callback received -> DB update committed (trader_dev, temp table as the
app role) -> answerCallbackQuery -> message edited. Also measures API RTT.
Waits up to MAX_WAIT seconds for a tap.
"""
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

import psycopg2

ENV = open("/Users/stephen/Documents/Code/Claude Code/Trader/Trader/docker/.env.dev").read()
TOKEN = re.search(r"^TELEGRAM_BOT_TOKEN=(\S+)$", ENV, re.M).group(1)
CHAT = int(re.search(r"^TELEGRAM_CHAT_ID=(\S+)$", ENV, re.M).group(1))
DB_URL = re.search(r"^DATABASE_URL=(\S+)$", ENV, re.M).group(1).replace("postgresql+psycopg://", "postgresql://")
MAX_WAIT = 4 * 3600
API = f"https://api.telegram.org/bot{TOKEN}/"


def tg(method, **params):
    data = urllib.parse.urlencode({k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in params.items()}).encode()
    try:
        with urllib.request.urlopen(API + method, data=data, timeout=70) as r:
            out = json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        print(f"  Telegram {method} -> HTTP {e.code}: {body[:300]}", flush=True)
        return None
    if not out.get("ok"):
        raise RuntimeError(out)
    return out["result"]


# API round-trip baseline
rtts = []
for _ in range(5):
    t = time.monotonic()
    tg("getMe")
    rtts.append((time.monotonic() - t) * 1000)
print(f"Telegram API RTT (getMe x5): min {min(rtts):.0f} ms, median {sorted(rtts)[2]:.0f} ms, max {max(rtts):.0f} ms", flush=True)

# DB: temp table stands in for the proposals table
db = psycopg2.connect(DB_URL)
cur = db.cursor()
cur.execute("CREATE TEMP TABLE s6_proposal (id int primary key, status text, decided_at timestamptz)")
cur.execute("INSERT INTO s6_proposal VALUES (1, 'pending', NULL)")
db.commit()

# retire the first test message's buttons
tg("editMessageText", chat_id=CHAT, message_id=4, text="🧪 Phase 0 test S6 (first attempt): superseded by the next message.")

# drop any old updates
ups = tg("getUpdates", timeout=0)
offset = ups[-1]["update_id"] + 1 if ups else 0

msg = tg("sendMessage", chat_id=CHAT, parse_mode="HTML",
         text=("🧪 <b>Phase 0 test S6</b> (not a real trade)\n"
               "BUY 100 AAPL @ 341.07 · stop 338.50 · risk $257\n"
               "Tap <b>Approve</b> to time the button round-trip."),
         reply_markup={"inline_keyboard": [[{"text": "✅ Approve", "callback_data": "s6:approve:1"},
                                            {"text": "❌ Reject", "callback_data": "s6:reject:1"}]]})
sent = time.time()
print(f"test proposal sent (message {msg['message_id']}); waiting up to {MAX_WAIT // 60} min for a tap", flush=True)

while time.time() - sent < MAX_WAIT:
    t_poll = time.monotonic()
    ups = tg("getUpdates", offset=offset, timeout=50, allowed_updates=["callback_query"]) or []
    for u in ups:
        offset = u["update_id"] + 1
        cq = u.get("callback_query")
        if not cq or cq["from"]["id"] != CHAT or not cq.get("data", "").startswith("s6:"):
            continue
        if (cq.get("message") or {}).get("message_id") != msg["message_id"]:
            tg("answerCallbackQuery", callback_query_id=cq["id"], text="Old test message; use the newest one")
            continue
        t0 = time.monotonic()
        _, action, pid = cq["data"].split(":")
        cur.execute("UPDATE s6_proposal SET status=%s, decided_at=now() WHERE id=%s AND status='pending' RETURNING id",
                    ("approved" if action == "approve" else "rejected", int(pid)))
        won = cur.fetchone() is not None
        db.commit()
        t_db = time.monotonic()
        acked = tg("answerCallbackQuery", callback_query_id=cq["id"], text="Approved" if action == "approve" else "Rejected")
        t_ans = time.monotonic()
        if acked is None:
            print("  answerCallbackQuery failed; timings below still valid for the DB step", flush=True)
        tg("editMessageText", chat_id=CHAT, message_id=msg["message_id"], parse_mode="HTML",
           text=(f"🧪 <b>Phase 0 test S6</b>: {action}d.\n"
                 f"DB update {1000 * (t_db - t0):.0f} ms · button acknowledged {1000 * (t_ans - t0):.0f} ms after the callback arrived."))
        t_edit = time.monotonic()
        print(f"callback '{action}' received {time.time() - sent:.0f}s after sending; first decision won={won}", flush=True)
        print(f"  callback -> DB committed : {1000 * (t_db - t0):.0f} ms", flush=True)
        print(f"  callback -> button acked : {1000 * (t_ans - t0):.0f} ms", flush=True)
        print(f"  callback -> message edit : {1000 * (t_edit - t0):.0f} ms", flush=True)
        print(f"  (long-poll delivery adds roughly one API RTT, ~{sorted(rtts)[2]:.0f} ms)", flush=True)
        total = (t_ans - t0) * 1000 + sorted(rtts)[2]
        print(f"S6 {'PASS' if total < 2000 else 'FAIL'}: estimated tap -> acknowledged ≈ {total:.0f} ms (target < 2000 ms)", flush=True)
        raise SystemExit(0)
print("S6 not completed: no tap within the wait window", flush=True)
