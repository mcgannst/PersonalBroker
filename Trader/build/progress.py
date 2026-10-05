"""Live build progress board: a tiny local web page fed by the orchestrator and every sub-agent.

State lives OUTSIDE the repo and outside /tmp (worktree agents share it by absolute path, and it survives the
nightly tmp cleanup): $TRADER_PROGRESS_DIR, default ~/Code/trader-progress.

  plan.json      the build: title, tasks (id, title, wave, deps, est_min), written by `init`
  status.json    header fields and per-task status, written by `head` and `task` (orchestrator only)
  events.jsonl   one line per report, appended by anyone (`report`)

Commands (all print one line and exit 0 unless the arguments are wrong):
  progress.py init PLAN.json                      load a plan (keeps existing status for matching task ids)
  progress.py head "key=value" ...                set header fields (now, next, wave, note, ...)
  progress.py task ID STATUS [--commit X] [--note TEXT]
        STATUS: todo | planning | building | checking | fixing | done | blocked
  progress.py report ID ROLE "message" [--pct N] [--state working|done|blocked|failed] [--agent NAME]
        what an agent is doing right now; call it at every step (at least every few minutes)
  progress.py milestone "text"                    a line for the milestones list
  progress.py serve [--port 8765]                 serve the page (foreground)
  progress.py daemon [--port 8765]                start `serve` detached and return
  progress.py snapshot                            print the merged state as JSON (what the page shows)

Estimates: each task has est_min (planned minutes of build + check). When tasks finish, the ratio of actual
to planned minutes over finished tasks (the pace) rescales every unfinished task's estimate, and a forecast
walks the dependency graph (at most `max_parallel` tasks at once) to give each task a start and finish time.
"""

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

DIR = Path(os.environ.get("TRADER_PROGRESS_DIR", str(Path.home() / "Code" / "trader-progress")))
PLAN, STATUS, EVENTS = DIR / "plan.json", DIR / "status.json", DIR / "events.jsonl"
STATUSES = ("todo", "planning", "building", "checking", "fixing", "done", "blocked")
ACTIVE = ("planning", "building", "checking", "fixing")
STALE_SECONDS = 600  # an agent with no report for this long is shown as quiet
MIN_REMAINING_MIN = 3.0  # a running task past its estimate is forecast to need at least this much more


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _iso(t: dt.datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(s: str) -> dt.datetime:
    return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.UTC)


def _read(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return default


def _write(path: Path, data: Any) -> None:
    DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(data, indent=1))
    tmp.replace(path)


def _append(event: dict[str, Any]) -> None:
    DIR.mkdir(parents=True, exist_ok=True)
    with EVENTS.open("a") as f:  # one write per line: concurrent agents never interleave
        f.write(json.dumps(event) + "\n")


def _status() -> dict[str, Any]:
    s = _read(STATUS, {})
    s.setdefault("head", {})
    s.setdefault("tasks", {})
    s.setdefault("milestones", [])
    return s


def _events() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        for line in EVENTS.read_text().splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    except FileNotFoundError:
        pass
    return out


# --- estimates and forecast -----------------------------------------------------------------------------------
def forecast(plan: dict[str, Any], status: dict[str, Any], now: dt.datetime) -> dict[str, Any]:
    """Per task: planned, adjusted and actual minutes, forecast start/finish; plus the pace and the build ETA."""
    tasks = plan.get("tasks", [])
    st = status["tasks"]
    done_plan = done_actual = 0.0
    for t in tasks:
        s = st.get(t["id"], {})
        if s.get("status") == "done" and s.get("started") and s.get("finished"):
            done_plan += float(t.get("est_min", 30))
            done_actual += (_parse(s["finished"]) - _parse(s["started"])).total_seconds() / 60
    pace = (done_actual / done_plan) if done_plan > 0 and done_actual > 0 else 1.0
    pace = min(max(pace, 0.25), 4.0)  # one odd task never swings the forecast wildly

    width = int(plan.get("max_parallel", 8))
    out: dict[str, Any] = {}
    finish: dict[str, dt.datetime] = {}
    pending: list[dict[str, Any]] = []
    running_ends: list[dt.datetime] = []
    for t in tasks:
        s = st.get(t["id"], {})
        planned = float(t.get("est_min", 30))
        row: dict[str, Any] = {"planned_min": planned, "adjusted_min": round(planned * pace, 1)}
        state = s.get("status", "todo")
        if state == "done" and s.get("finished"):
            fin = _parse(s["finished"])
            finish[t["id"]] = fin
            if s.get("started"):
                row["actual_min"] = round((fin - _parse(s["started"])).total_seconds() / 60, 1)
            row["finish"] = _iso(fin)
        elif state in ACTIVE and s.get("started"):
            elapsed = (now - _parse(s["started"])).total_seconds() / 60
            remaining = max(planned * pace - elapsed, MIN_REMAINING_MIN)
            fin = now + dt.timedelta(minutes=remaining)
            finish[t["id"]] = fin
            running_ends.append(fin)
            row.update(elapsed_min=round(elapsed, 1), remaining_min=round(remaining, 1), finish=_iso(fin))
            row["start"] = s["started"]
        else:
            pending.append(t)
        out[t["id"]] = row
    # Walk the graph: a task starts when its dependencies have finished and a slot is free.
    slots = sorted(running_ends)
    guard = 0
    while pending and guard < 10_000:
        guard += 1
        ready = [t for t in pending if all(d in finish for d in t.get("deps", []))]
        if not ready:
            break  # a cycle or an unknown dependency: leave the rest without a forecast
        ready.sort(key=lambda t: (max([finish[d] for d in t.get("deps", [])], default=now), t.get("wave", 0)))
        t = ready[0]
        pending.remove(t)
        start = max([now, *[finish[d] for d in t.get("deps", [])]])
        if len(slots) >= width:
            slots.sort()
            start = max(start, slots.pop(0))
        fin = start + dt.timedelta(minutes=float(t.get("est_min", 30)) * pace)
        finish[t["id"]] = fin
        slots.append(fin)
        out[t["id"]].update(start=_iso(start), finish=_iso(fin))
    total = sum(float(t.get("est_min", 30)) for t in tasks)
    done_ids = {t["id"] for t in tasks if st.get(t["id"], {}).get("status") == "done"}
    eta = max(finish.values()) if tasks and len(finish) == len(tasks) and len(done_ids) < len(tasks) else None
    return {
        "tasks": out,
        "pace": round(pace, 2),
        "measured_on": int(sum(1 for t in tasks if "actual_min" in out[t["id"]])),
        "eta": _iso(eta) if eta else None,
        "planned_total_min": total,
        "done": len(done_ids),
        "count": len(tasks),
    }


def snapshot() -> dict[str, Any]:
    now = _now()
    plan, status, events = _read(PLAN, {"tasks": []}), _status(), _events()
    latest: dict[str, dict[str, Any]] = {}
    first: dict[str, str] = {}  # agent -> its first report's time (when it started)
    for e in events:
        if e.get("kind") == "report":
            name = e.get("agent") or f"{e.get('task')}-{e.get('role')}"
            latest[name] = e
            first.setdefault(name, e["ts"])
    agents = []
    for name, e in latest.items():
        age = (now - _parse(e["ts"])).total_seconds()
        # running time: from the first report to now while working, else to the last report
        end = now if e.get("state") == "working" else _parse(e["ts"])
        running = (end - _parse(first[name])).total_seconds()
        task_state = status["tasks"].get(e.get("task", ""), {}).get("status")
        if e.get("state") in ("done", "failed") and age > 1800:
            continue  # finished long ago: off the live list
        agents.append(
            {
                **e,
                "name": name,
                "age_s": int(age),
                "running_s": int(running),
                "started": first[name],
                "quiet": e.get("state") == "working" and age > STALE_SECONDS,
                "task_status": task_state,
            }
        )
    agents.sort(key=lambda a: (a.get("state") != "working", a["age_s"]))
    return {
        "now": _iso(now),
        "plan": plan,
        "status": status,
        "forecast": forecast(plan, status, now),
        "agents": agents,
        "feed": events[-60:][::-1],
    }


# --- commands -------------------------------------------------------------------------------------------------
def cmd_init(a: argparse.Namespace) -> None:
    plan = json.loads(Path(a.plan).read_text())
    _write(PLAN, plan)
    status = _status()
    for t in plan["tasks"]:
        status["tasks"].setdefault(t["id"], {"status": "todo"})
    _write(STATUS, status)
    _append({"ts": _iso(_now()), "kind": "note", "msg": f"plan loaded: {plan.get('title', '')}"})
    print(f"loaded {len(plan['tasks'])} tasks into {DIR}")


def cmd_head(a: argparse.Namespace) -> None:
    status = _status()
    for item in a.fields:
        key, _, value = item.partition("=")
        status["head"][key.strip()] = value
    status["head"]["updated"] = _iso(_now())
    _write(STATUS, status)
    print("ok")


def cmd_task(a: argparse.Namespace) -> None:
    if a.status not in STATUSES:
        raise SystemExit(f"status must be one of {', '.join(STATUSES)}")
    status = _status()
    t = status["tasks"].setdefault(a.id, {"status": "todo"})
    now = _iso(_now())
    if a.status in ACTIVE and not t.get("started"):
        t["started"] = now
    if a.status == "done":
        t.setdefault("started", now)
        t["finished"] = now
    if a.status == "todo":
        t.pop("started", None)
        t.pop("finished", None)
    t["status"] = a.status
    t["updated"] = now
    if a.commit:
        t["commit"] = a.commit
    if a.note is not None:
        t["note"] = a.note
    _write(STATUS, status)
    _append({"ts": now, "kind": "task", "task": a.id, "status": a.status, "msg": a.note or ""})
    print("ok")


def cmd_report(a: argparse.Namespace) -> None:
    _append(
        {
            "ts": _iso(_now()),
            "kind": "report",
            "task": a.id,
            "role": a.role,
            "agent": a.agent or f"{a.id}-{a.role}",
            "state": a.state,
            "pct": a.pct,
            "msg": a.message[:400],
        }
    )
    print("ok")


def cmd_milestone(a: argparse.Namespace) -> None:
    status = _status()
    now = _iso(_now())
    status["milestones"].append({"ts": now, "msg": a.text})
    _write(STATUS, status)
    _append({"ts": now, "kind": "milestone", "msg": a.text})
    print("ok")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 (http.server's name)
        if self.path.startswith("/data"):
            body, ctype = json.dumps(snapshot()).encode(), "application/json"
        elif self.path in ("/", "/index.html"):
            body, ctype = (Path(__file__).with_name("progress.html")).read_bytes(), "text/html; charset=utf-8"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:  # quiet: the page polls every few seconds
        return


def cmd_serve(a: argparse.Namespace) -> None:
    server = ThreadingHTTPServer(("0.0.0.0", a.port), Handler)  # noqa: S104 (home LAN board, read-only)
    print(f"serving {DIR} on port {a.port}", flush=True)
    server.serve_forever()


def cmd_daemon(a: argparse.Namespace) -> None:
    DIR.mkdir(parents=True, exist_ok=True)
    log = (DIR / "server.log").open("a")
    proc = subprocess.Popen(  # noqa: S603 (our own script)
        [sys.executable, str(Path(__file__).resolve()), "serve", "--port", str(a.port)],
        stdout=log,
        stderr=log,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    (DIR / "server.pid").write_text(str(proc.pid))
    print(f"started pid {proc.pid} on port {a.port}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("init")
    s.add_argument("plan")
    s.set_defaults(fn=cmd_init)
    s = sub.add_parser("head")
    s.add_argument("fields", nargs="+")
    s.set_defaults(fn=cmd_head)
    s = sub.add_parser("task")
    s.add_argument("id")
    s.add_argument("status")
    s.add_argument("--commit")
    s.add_argument("--note")
    s.set_defaults(fn=cmd_task)
    s = sub.add_parser("report")
    s.add_argument("id")
    s.add_argument("role")
    s.add_argument("message")
    s.add_argument("--pct", type=int)
    s.add_argument("--state", default="working", choices=("working", "done", "blocked", "failed"))
    s.add_argument("--agent")
    s.set_defaults(fn=cmd_report)
    s = sub.add_parser("milestone")
    s.add_argument("text")
    s.set_defaults(fn=cmd_milestone)
    for name, fn in (("serve", cmd_serve), ("daemon", cmd_daemon)):
        s = sub.add_parser(name)
        s.add_argument("--port", type=int, default=8765)
        s.set_defaults(fn=fn)
    s = sub.add_parser("snapshot")
    s.set_defaults(fn=lambda a: print(json.dumps(snapshot(), indent=1)))
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
