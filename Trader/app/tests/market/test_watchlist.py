"""P4-T10 acceptance tests 1 and 2 (`parse_watchlist_csv`) and the storage helpers of
`trader.market.watchlist` (store, get, delete, audit)."""

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from trader.db import models as m
from trader.market.clock import FixedClock
from trader.market.watchlist import (
    MAX_BYTES,
    MAX_TICKERS,
    ParsedWatchlist,
    WatchlistError,
    delete_watchlist,
    get_watchlist,
    parse_watchlist_csv,
    store_watchlist,
)

NOW = datetime(2026, 10, 6, 21, 0, tzinfo=UTC)
DAY = date(2026, 10, 7)


# --- acceptance test 1 ------


def test_header_symbol_column_duplicates_and_invalid_rows() -> None:
    data = b"Symbol,Name\naapl,Apple\nBF-B,Brown-Forman\naapl,Apple again\n$$$,junk\n,\n"
    parsed = parse_watchlist_csv(data)
    assert parsed == ParsedWatchlist(
        tickers=("AAPL", "BF.B"),
        rejected=((4, "aapl", "duplicate"), (5, "$$$", "invalid ticker")),
    )


def test_header_column_is_found_anywhere_in_the_row() -> None:
    parsed = parse_watchlist_csv(b"Name,TICKER\nApple,msft\nNvidia, nvda \n")
    assert parsed.tickers == ("MSFT", "NVDA")
    assert parsed.rejected == ()


# --- acceptance test 2 ------


def test_headerless_single_column() -> None:
    parsed = parse_watchlist_csv(b"aapl\nmsft\n\nBRK.B\n")
    assert parsed.tickers == ("AAPL", "MSFT", "BRK.B")
    assert parsed.rejected == ()


def test_headerless_uses_the_first_column_of_every_row() -> None:
    assert parse_watchlist_csv(b"aapl,Apple\nmsft,Microsoft\n").tickers == ("AAPL", "MSFT")


def test_file_over_max_bytes_is_refused() -> None:
    data = b"AAPL\n" * (300 * 1024 // 5)  # about 300 KB
    assert len(data) > MAX_BYTES
    with pytest.raises(WatchlistError, match="larger than"):
        parse_watchlist_csv(data)


def test_more_than_max_tickers_is_refused() -> None:
    names = [f"A{i:04d}" for i in range(MAX_TICKERS + 1)]
    data = "\n".join(names).encode()
    assert len(data) <= MAX_BYTES
    with pytest.raises(WatchlistError, match="more than 1000"):
        parse_watchlist_csv(data)


def test_exactly_max_tickers_is_accepted() -> None:
    names = [f"A{i:04d}" for i in range(MAX_TICKERS)]
    assert len(parse_watchlist_csv("\n".join(names).encode()).tickers) == MAX_TICKERS


@pytest.mark.parametrize("data", [b"", b"\n\n", b"Symbol\n", b"$$$\n123\n", b"\xef\xbb\xbf"])
def test_no_valid_ticker_is_refused(data: bytes) -> None:
    with pytest.raises(WatchlistError, match="No valid ticker"):
        parse_watchlist_csv(data)


# --- hardening ------


def test_bom_and_crlf_are_handled() -> None:
    parsed = parse_watchlist_csv(b"\xef\xbb\xbfSymbol\r\nAAPL\r\nMSFT\r\n")
    assert parsed.tickers == ("AAPL", "MSFT")
    assert parsed.rejected == ()


def test_bom_on_a_headerless_file_is_not_part_of_the_ticker() -> None:
    assert parse_watchlist_csv(b"\xef\xbb\xbfAAPL\r\n").tickers == ("AAPL",)


def test_bare_cr_line_endings_are_handled() -> None:
    assert parse_watchlist_csv(b"AAPL\rMSFT\r").tickers == ("AAPL", "MSFT")


def test_quoted_cells() -> None:
    assert parse_watchlist_csv(b'"Symbol","Name"\n"aapl","Apple, Inc."\n').tickers == ("AAPL",)


def test_share_class_duplicates_after_mapping() -> None:
    parsed = parse_watchlist_csv(b"BF-B\nBF.B\nbf-b\n")
    assert parsed.tickers == ("BF.B",)
    assert parsed.rejected == ((2, "BF.B", "duplicate"), (3, "bf-b", "duplicate"))


@pytest.mark.parametrize(
    "cell", ["=HYPERLINK(1)", "+AAPL", "-AAPL", "@SUM(A1)", "=1+1", "\tAAPL=1", "AAPL;rm", "TOOLONGTICKER"]
)
def test_formula_looking_and_malformed_cells_are_rejected(cell: str) -> None:
    parsed = parse_watchlist_csv(f"MSFT\n{cell}\n".encode())
    assert parsed.tickers == ("MSFT",)
    assert len(parsed.rejected) == 1
    row, _value, reason = parsed.rejected[0]
    assert (row, reason) == (2, "invalid ticker")


def test_rejected_value_is_shortened_and_printable() -> None:
    long_cell = "=" + "X" * 500
    parsed = parse_watchlist_csv(f"MSFT\n{long_cell}\nbad\x07cell\n".encode())
    values = [v for _, v, _ in parsed.rejected]
    assert len(values[0]) <= 40
    assert "\x07" not in values[1]


def test_not_utf8_is_refused() -> None:
    with pytest.raises(WatchlistError, match="UTF-8"):
        parse_watchlist_csv(b"AAPL\n\xff\xfe\n")


def test_nul_bytes_are_refused() -> None:
    with pytest.raises(WatchlistError):
        parse_watchlist_csv(b"AAPL\n\x00MSFT\n")


def test_too_many_rejected_rows_is_refused() -> None:
    data = b"AAPL\n" + b"$\n" * (MAX_TICKERS + 1)
    with pytest.raises(WatchlistError, match="rows"):
        parse_watchlist_csv(data)


# --- storage ------


def _audits(factory: sessionmaker[Session], action: str) -> list[m.AuditLog]:
    with factory() as s:
        return list(s.scalars(select(m.AuditLog).where(m.AuditLog.action == action).order_by(m.AuditLog.id)))


@pytest.mark.db
def test_store_get_replace_and_delete(db_factory: sessionmaker[Session]) -> None:
    clock = FixedClock(NOW)
    assert get_watchlist(db_factory, DAY) is None

    store_watchlist(db_factory, clock, DAY, ["AAPL", "BF.B"], "first.csv", "web:stephen")
    row = get_watchlist(db_factory, DAY)
    assert row is not None
    assert (row.tickers, row.filename, row.uploaded_by, row.uploaded_at) == (
        ["AAPL", "BF.B"],
        "first.csv",
        "web:stephen",
        NOW,
    )

    store_watchlist(db_factory, clock, DAY, ["MSFT"], None, "web:stephen")
    row = get_watchlist(db_factory, DAY)
    assert row is not None and row.tickers == ["MSFT"] and row.filename is None

    uploads = _audits(db_factory, "watchlist.upload")
    assert len(uploads) == 2 and all(a.actor == "web:stephen" for a in uploads)
    assert uploads[0].before is None
    assert uploads[0].after == {
        "session_date": "2026-10-07",
        "tickers": ["AAPL", "BF.B"],
        "count": 2,
        "filename": "first.csv",
    }
    assert uploads[1].before is not None and uploads[1].before["tickers"] == ["AAPL", "BF.B"]
    assert uploads[1].after["tickers"] == ["MSFT"]

    assert delete_watchlist(db_factory, clock, DAY, "web:stephen") is True
    assert get_watchlist(db_factory, DAY) is None
    assert delete_watchlist(db_factory, clock, DAY, "web:stephen") is False
    deletes = _audits(db_factory, "watchlist.delete")
    assert len(deletes) == 1
    assert deletes[0].before is not None and deletes[0].before["tickers"] == ["MSFT"]
    assert deletes[0].after is None
