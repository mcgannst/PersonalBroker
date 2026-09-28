"""List the crontab lines that fell inside a deploy's downtime window (P6-T6, plan decision D3).

    uv --directory Trader/app run python ../build/cron_gap.py --from <down> --to <up> [--container trader-dev]

`deploy.sh` prints the `down from` / `up at` stamps (UTC ISO). Any aware ISO datetime works (any offset, or a
trailing Z). Every crontab entry whose fire time falls in [from - 60 s, to + 60 s] is printed, in time order,
as the exact command to run by hand from the Mac, with the session it was for pinned:

    <ET time>  <MT time>  docker --context shared-docker-server exec <container> trader <args> <date args>

- nightly: --date <the next session after the fire's ET date> (the session it prepares)
- premarket, preopen, checkin, event, postclose: --date <the fire's ET date>; when that date is not a
  session the line reads "not a session (<date>): nothing to run"
- weekly: --date <the latest session on or before the fire's ET date> (the week just ended)
- soak-report: --through <the latest session on or before the fire's ET date> (its flags kept)
- token-refresh: no date; any other command is printed unchanged with "(no date pinned)"

"nothing skipped" when no line fell in the window. A warning is added when the window overlaps
09:15-16:30 ET on a weekday. Exit 2 on a crontab line it can't read (step values, names, other variables)
or a bad window. No database, no network: only the exchange calendar (trader.market.calendar).
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from trader.market.calendar import SessionCalendar

DEFAULT_CRONTAB = Path(__file__).resolve().parents[1] / "docker" / "crontab"
DEFAULT_CONTAINER = "trader-dev"
DOCKER = "docker --context shared-docker-server exec"
ET = ZoneInfo("America/New_York")
MT = ZoneInfo("America/Edmonton")
MARGIN = timedelta(seconds=60)
MARKET_WINDOW = (time(9, 15), time(16, 30))

SESSION_DAY_COMMANDS = frozenset({"premarket", "preopen", "checkin", "event", "postclose"})
_FIELD = re.compile(r"^\d+(-\d+)?(,\d+(-\d+)?)*$")
# (name, lowest, highest) of the five fields; day of week 7 is Sunday like 0.
_FIELDS = (
    ("minute", 0, 59),
    ("hour", 0, 23),
    ("day of month", 1, 31),
    ("month", 1, 12),
    ("day of week", 0, 7),
)


class CrontabError(ValueError):
    """A crontab line this tool can't read."""


@dataclass(frozen=True)
class CronLine:
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int] | None  # None: `*`
    months: frozenset[int]
    weekdays: frozenset[int] | None  # cron numbering, Sunday = 0; None: `*`
    command: str

    def fires_at(self, local: datetime) -> bool:
        if local.minute not in self.minutes or local.hour not in self.hours or local.month not in self.months:
            return False
        cron_weekday = (local.weekday() + 1) % 7
        day_ok = self.days is None or local.day in self.days
        weekday_ok = self.weekdays is None or cron_weekday in self.weekdays
        if self.days is not None and self.weekdays is not None:
            return day_ok or weekday_ok  # cron: either restricted field matches
        return day_ok and weekday_ok


def _field(text: str, index: int, raw: str) -> frozenset[int] | None:
    name, low, high = _FIELDS[index]
    if text == "*":
        return None
    if not _FIELD.match(text):
        raise CrontabError(f"unsupported {name} field {text!r} in line: {raw}")
    values: set[int] = set()
    for part in text.split(","):
        first, _, last = part.partition("-")
        start, end = int(first), int(last or first)
        if start > end or start < low or end > high:
            raise CrontabError(f"{name} {part!r} out of range in line: {raw}")
        values.update(range(start, end + 1))
    if index == 4 and 7 in values:
        values = (values - {7}) | {0}
    return frozenset(values)


def _all(index: int) -> frozenset[int]:
    _, low, high = _FIELDS[index]
    return frozenset(range(low, high + 1))


def parse_crontab(text: str) -> tuple[ZoneInfo, list[CronLine]]:
    """The CRON_TZ zone and the five-field lines; CrontabError names any other line."""
    zone: ZoneInfo | None = None
    lines: list[CronLine] = []
    for raw_line in text.splitlines():
        raw = raw_line.strip()
        if not raw or raw.startswith("#"):
            continue
        if raw.startswith("CRON_TZ="):
            try:
                zone = ZoneInfo(raw.split("=", 1)[1].strip())
            except (KeyError, ValueError) as exc:
                raise CrontabError(f"unknown time zone in line: {raw}") from exc
            continue
        parts = raw.split(None, 5)
        if len(parts) < 6:
            raise CrontabError(f"not a five-field cron line: {raw}")
        fields = [_field(parts[i], i, raw) for i in range(5)]
        lines.append(
            CronLine(
                minutes=fields[0] or _all(0),
                hours=fields[1] or _all(1),
                days=fields[2],
                months=fields[3] or _all(3),
                weekdays=fields[4],
                command=parts[5].strip(),
            )
        )
    if zone is None:
        raise CrontabError("the crontab has no CRON_TZ line")
    return zone, lines


def _parse_moment(value: str, flag: str) -> datetime:
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{flag} {value!r} is not an ISO datetime") from exc
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError(f"{flag} {value!r} has no UTC offset (use e.g. 2026-10-05T20:00:00-04:00 or ...Z)")
    return moment.astimezone(UTC)


def fires_between(
    zone: ZoneInfo, lines: Sequence[CronLine], start: datetime, end: datetime
) -> list[tuple[datetime, int]]:
    """(fire time in UTC, line index) for every fire in [start, end], in time order then crontab order."""
    fires: list[tuple[datetime, int]] = []
    minute = start.replace(second=0, microsecond=0)
    if minute < start:
        minute += timedelta(minutes=1)
    while minute <= end:
        local = minute.astimezone(zone)
        for index, line in enumerate(lines):
            if line.fires_at(local):
                fires.append((minute, index))
        minute += timedelta(minutes=1)
    return fires


def _latest_session(calendar: SessionCalendar, day: date) -> date:
    return day if calendar.is_session(day) else calendar.previous_session(day)


def pinned_command(calendar: SessionCalendar, command: str, fire_day: date) -> tuple[str | None, str]:
    """(the command with its date args, or None when there is nothing to run, and a note)."""
    words = command.split()
    if len(words) < 2 or words[0] != "trader":
        return command, " (no date pinned)"
    name = words[1]
    if name == "token-refresh":
        return command, ""
    if name == "nightly":
        return f"{command} --date {calendar.next_session(fire_day).isoformat()}", ""
    if name in SESSION_DAY_COMMANDS:
        if not calendar.is_session(fire_day):
            return None, f"not a session ({fire_day.isoformat()}): nothing to run"
        return f"{command} --date {fire_day.isoformat()}", ""
    if name == "weekly":
        return f"{command} --date {_latest_session(calendar, fire_day).isoformat()}", ""
    if name == "soak-report":
        return f"{command} --through {_latest_session(calendar, fire_day).isoformat()}", ""
    return command, " (no date pinned)"


def overlaps_market_hours(start: datetime, end: datetime) -> bool:
    """True when [start, end] overlaps 09:15-16:30 ET on a Monday-Friday."""
    day = start.astimezone(ET).date()
    last = end.astimezone(ET).date()
    while day <= last:
        if day.weekday() < 5:
            opens = datetime.combine(day, MARKET_WINDOW[0], ET)
            closes = datetime.combine(day, MARKET_WINDOW[1], ET)
            if start < closes and end > opens:
                return True
        day += timedelta(days=1)
    return False


def _stamp(moment: datetime, zone: ZoneInfo, with_date: bool) -> str:
    local = moment.astimezone(zone)
    return local.strftime("%Y-%m-%d %H:%M %Z" if with_date else "%H:%M %Z")


def report(
    crontab_text: str, start: datetime, end: datetime, container: str, calendar: SessionCalendar
) -> list[str]:
    zone, lines = parse_crontab(crontab_text)
    out: list[str] = []
    for moment, index in fires_between(zone, lines, start - MARGIN, end + MARGIN):
        fire_day = moment.astimezone(ET).date()
        command, note = pinned_command(calendar, lines[index].command, fire_day)
        when = f"{_stamp(moment, ET, True)}  {_stamp(moment, MT, True)}"
        if command is None:
            out.append(f"{when}  {note} [{lines[index].command}]")
        else:
            out.append(f"{when}  {DOCKER} {container} {command}{note}")
    if not out:
        out.append("nothing skipped")
    if overlaps_market_hours(start, end):
        out.append("warning: the window overlaps 09:15-16:30 ET on a weekday")
    return out


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="List the cron lines that fell inside a downtime window.")
    parser.add_argument("--from", dest="start", required=True, help="window start, aware ISO datetime")
    parser.add_argument("--to", dest="end", required=True, help="window end, aware ISO datetime")
    parser.add_argument(
        "--crontab", default=str(DEFAULT_CRONTAB), help="crontab file (default: docker/crontab)"
    )
    parser.add_argument("--container", default=DEFAULT_CONTAINER, help="trader-dev or trader")
    args = parser.parse_args(argv)
    try:
        start = _parse_moment(args.start, "--from")
        end = _parse_moment(args.end, "--to")
        if end < start:
            raise ValueError("--to is before --from")
        text = Path(args.crontab).read_text()
        lines = report(text, start, end, args.container, SessionCalendar())
    except (ValueError, OSError) as exc:
        print(f"cron_gap: {exc}", file=sys.stderr)
        return 2
    for line in lines:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
