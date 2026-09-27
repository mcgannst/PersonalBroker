"""Update the BUILD_STATE.md header and task board (orchestrator only).

Usage: board.py TASK STATUS ATTEMPT "STAGES" COMMIT ["Header field=value" ...]
- "-" leaves a cell unchanged; TASK "-" updates only the header.
- Header fields that don't exist yet are added. "Last updated (UTC)" is always refreshed.
"""

import datetime as dt
import re
import sys
from pathlib import Path

STATE = Path(__file__).resolve().parents[1] / "docs" / "build" / "BUILD_STATE.md"


def main(argv: list[str]) -> None:
    if len(argv) < 5:
        raise SystemExit(__doc__)
    task, status, attempt, stages, commit, *headers = argv
    lines = STATE.read_text().split("\n")

    if task != "-":
        for i, line in enumerate(lines):
            cells = line.split("|")
            if len(cells) >= 9 and cells[1].strip() == task:
                for idx, val in ((4, status), (5, attempt), (6, stages), (7, commit)):
                    if val != "-":
                        cells[idx] = f" {val} "
                lines[i] = "|".join(cells)
                break
        else:
            raise SystemExit(f"task {task} not on board")

    now = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    headers.append(f"Last updated (UTC)={now}")
    start = lines.index("## Header")
    end = lines.index("## Task board")
    for item in headers:
        key, _, value = item.partition("=")
        pat = re.compile(rf"^\| {re.escape(key)} \|")
        for i in range(start, end):
            if pat.match(lines[i]):
                lines[i] = f"| {key} | {value} |"
                break
        else:
            last = max(i for i in range(start, end) if lines[i].startswith("|"))
            lines.insert(last + 1, f"| {key} | {value} |")
            end += 1
    STATE.write_text("\n".join(lines))
    print("ok")


if __name__ == "__main__":
    main(sys.argv[1:])
