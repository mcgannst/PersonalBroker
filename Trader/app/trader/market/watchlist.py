"""The manual watchlist (SPEC §4.2 manual fallback): a CSV of tickers uploaded for one session, which the
nightly job then uses as that session's universe (source `manual`) instead of FinViz.

Stub (P4-T1): T10 implements the functions.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from sqlalchemy.orm import Session, sessionmaker

from trader.db.models import ManualWatchlist
from trader.market.clock import Clock

MAX_TICKERS = 1000
MAX_BYTES = 262_144


@dataclass(frozen=True, slots=True)
class ParsedWatchlist:
    tickers: tuple[str, ...]  # Questrade-style, upper-case, unique, in file order
    rejected: tuple[tuple[int, str, str], ...]  # (1-based row, value, reason: "invalid ticker" | "duplicate")


def parse_watchlist_csv(data: bytes) -> ParsedWatchlist:
    raise NotImplementedError("P4-T10")


def store_watchlist(
    factory: sessionmaker[Session],
    clock: Clock,
    session_date: date,
    tickers: Sequence[str],
    filename: str | None,
    actor: str,
) -> None:
    """Upsert the session's list (audit `watchlist.upload`)."""
    raise NotImplementedError("P4-T10")


def get_watchlist(factory: sessionmaker[Session], session_date: date) -> ManualWatchlist | None:
    raise NotImplementedError("P4-T10")


def delete_watchlist(factory: sessionmaker[Session], clock: Clock, session_date: date, actor: str) -> bool:
    """True when a list was deleted (audit `watchlist.delete`)."""
    raise NotImplementedError("P4-T10")
