"""The manual watchlist (SPEC §4.2 manual fallback): a CSV of tickers uploaded for one session, which the
nightly job then uses as that session's universe (source `manual`) instead of FinViz. An uploaded list
replaces FinViz for its session (no merging); deleting it goes back to FinViz (Phase 4 resolved decision 5).

CSV rules (`parse_watchlist_csv`):
- UTF-8, a BOM is ignored; CRLF, LF and CR line endings; NUL bytes refuse the file.
- The first row is a header when any of its cells is `ticker` or `symbol` (any case); that column is used.
  Otherwise the first column of every row.
- Each value is trimmed, upper-cased, mapped with `to_questrade_ticker` (`BF-B` -> `BF.B`) and must match
  `TICKER_PATTERN` (a letter, then letters, digits, `.` or `-`, at most 10 characters). So a
  formula-looking cell (`=...`, `+...`, `-...`, `@...`) is always an `invalid ticker`.
- Bad values are listed as (1-based row, value, reason) with reason `invalid ticker` or `duplicate`; the
  value is shortened to `MAX_VALUE_CHARS` printable characters. Empty rows are skipped.
- `WatchlistError` (nothing is stored): a file over `MAX_BYTES`, not UTF-8 text, more than `MAX_TICKERS`
  tickers, more than `MAX_TICKERS` rejected rows, or no valid ticker at all.
"""

import csv
import io
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from trader.adapters.finviz.parser import to_questrade_ticker
from trader.db.models import AuditLog, ManualWatchlist
from trader.db.session import session_scope
from trader.market.clock import Clock
from trader.settings_store import TICKER_PATTERN

MAX_TICKERS = 1000
MAX_BYTES = 262_144
MAX_VALUE_CHARS = 40  # a rejected value is echoed back at most this long
HEADER_NAMES = frozenset({"ticker", "symbol"})
FILENAME_MAX = 200  # manual_watchlists.filename is varchar(200)

_TICKER = re.compile(TICKER_PATTERN)


class WatchlistError(ValueError):
    """The file cannot be used as a watchlist; the message is written to be shown to Stephen."""


@dataclass(frozen=True, slots=True)
class ParsedWatchlist:
    tickers: tuple[str, ...]  # Questrade-style, upper-case, unique, in file order
    rejected: tuple[tuple[int, str, str], ...]  # (1-based row, value, reason: "invalid ticker" | "duplicate")


def _shown(value: str) -> str:
    """A rejected value as echoed back: printable characters only, shortened."""
    clean = "".join(ch if ch.isprintable() else "?" for ch in value)
    return clean if len(clean) <= MAX_VALUE_CHARS else clean[: MAX_VALUE_CHARS - 3] + "..."


def _rows(data: bytes) -> list[list[str]]:
    if len(data) > MAX_BYTES:
        raise WatchlistError(f"The file is larger than {MAX_BYTES // 1024} KB")
    if b"\x00" in data:
        raise WatchlistError("The file is not a text CSV file")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise WatchlistError("The file is not UTF-8 text") from None
    try:
        return list(csv.reader(io.StringIO(text, newline="")))
    except csv.Error:
        raise WatchlistError("The file is not a readable CSV file") from None


def parse_watchlist_csv(data: bytes) -> ParsedWatchlist:
    rows = _rows(data)
    column, start = 0, 0
    if rows:
        header = [cell.strip().lower() for cell in rows[0]]
        found = [i for i, cell in enumerate(header) if cell in HEADER_NAMES]
        if found:
            column, start = found[0], 1
    tickers: dict[str, None] = {}
    rejected: list[tuple[int, str, str]] = []
    for number, row in enumerate(rows[start:], start=start + 1):
        raw = row[column] if column < len(row) else ""
        if not raw.strip():
            continue
        ticker = to_questrade_ticker(raw.strip().upper())
        if not _TICKER.fullmatch(ticker):
            rejected.append((number, _shown(raw.strip()), "invalid ticker"))
        elif ticker in tickers:
            rejected.append((number, _shown(raw.strip()), "duplicate"))
        else:
            tickers[ticker] = None
        if len(tickers) > MAX_TICKERS:
            raise WatchlistError(f"The file has more than {MAX_TICKERS} tickers")
        if len(rejected) > MAX_TICKERS:
            raise WatchlistError(f"The file has more than {MAX_TICKERS} rejected rows")
    if not tickers:
        raise WatchlistError("No valid ticker in the file")
    return ParsedWatchlist(tuple(tickers), tuple(rejected))


def clean_filename(filename: str | None) -> str | None:
    """The base name of an uploaded file (browsers may send a path), printable, at most 200 characters."""
    if not filename:
        return None
    base = re.split(r"[\\/]", filename)[-1]
    base = "".join(ch for ch in base if ch.isprintable()).strip()
    return base[:FILENAME_MAX] or None


def _snapshot(row: ManualWatchlist | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "session_date": row.session_date.isoformat(),
        "tickers": list(row.tickers),
        "count": len(row.tickers),
        "filename": row.filename,
    }


def store_watchlist(
    factory: sessionmaker[Session],
    clock: Clock,
    session_date: date,
    tickers: Sequence[str],
    filename: str | None,
    actor: str,
) -> None:
    """Upsert the session's list (audit `watchlist.upload`)."""
    now = clock.now()
    values = {
        "tickers": list(tickers),
        "filename": clean_filename(filename),
        "uploaded_at": now,
        "uploaded_by": actor,
    }
    with session_scope(factory) as s:
        before = _snapshot(s.get(ManualWatchlist, session_date, with_for_update=True))
        stmt = insert(ManualWatchlist).values(session_date=session_date, **values)
        s.execute(stmt.on_conflict_do_update(index_elements=["session_date"], set_=values))
        after = {
            "session_date": session_date.isoformat(),
            "tickers": values["tickers"],
            "count": len(tickers),
            "filename": values["filename"],
        }
        s.add(AuditLog(ts=now, actor=actor, action="watchlist.upload", before=before, after=after))


def get_watchlist(factory: sessionmaker[Session], session_date: date) -> ManualWatchlist | None:
    with factory() as s:
        return s.get(ManualWatchlist, session_date)


def delete_watchlist(factory: sessionmaker[Session], clock: Clock, session_date: date, actor: str) -> bool:
    """True when a list was deleted (audit `watchlist.delete`)."""
    with session_scope(factory) as s:
        row = s.get(ManualWatchlist, session_date, with_for_update=True)
        if row is None:
            return False
        before = _snapshot(row)
        s.delete(row)
        s.add(AuditLog(ts=clock.now(), actor=actor, action="watchlist.delete", before=before, after=None))
        return True
