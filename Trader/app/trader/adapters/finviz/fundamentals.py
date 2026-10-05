"""The FinViz quote page's snapshot table (OPTSIM T8): label -> text, and the fundamentals the options side
reads from it. Tested against saved pages; `parser.py` is not touched.

Page markers relied on (saved page `tests/fixtures/finviz/raw_quote_AAPL.html`, 2026-09): one or more
`table.snapshot-table2`, each row a label cell followed by a value cell (older layouts put several
label/value pairs in one row; both read the same here).

Like the parser, nothing here raises on odd HTML. `snapshot_problem` says why a page does not read as a
snapshot, and the scraper turns that into a FinvizError. One missing or unreadable label is not a problem:
that field is None (FinViz shows other labels for a fund), and the caller treats None as unknown.

**ASSUMPTION** (task plan risk R11, checked live at T17): the label names in `LABELS`.
"""

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from selectolax.parser import HTMLParser

from trader.adapters.finviz.parser import parse_earnings

# field -> the snapshot label it is read from
LABELS: dict[str, str] = {
    "eps_growth_yoy": "EPS Y/Y TTM",
    "debt_to_equity": "Debt/Eq",
    "book_value_per_share": "Book/sh",
    "payout_ratio": "Payout",
    "short_float": "Short Float",
    "next_earnings_date": "Earnings",
    "rsi14": "RSI (14)",
    "market_cap_usd": "Market Cap",
}
_SCALES = {"K": 3, "M": 6, "B": 9, "T": 12}
# "Oct 22 AMC" (after the close), "Oct 22 BMO" (before the open), or the date alone.
_EARNINGS_RE = re.compile(r"^([A-Z][a-z]{2} \d{1,2})(?:\s+(AMC|BMO))?$")


@dataclass(frozen=True, slots=True)
class FinvizFundamentals:
    """Ratios are fractions (0.3257 = 32.57%). None: FinViz shows "-", or the text did not read."""

    eps_growth_yoy: Decimal | None = None
    debt_to_equity: Decimal | None = None
    book_value_per_share: Decimal | None = None
    payout_ratio: Decimal | None = None
    short_float: Decimal | None = None
    next_earnings_date: date | None = None
    rsi14: Decimal | None = None
    market_cap_usd: Decimal | None = None


def parse_snapshot(html: str) -> dict[str, str]:
    """Label -> value text of every snapshot table on the page; {} when the page has none. The first
    value of a repeated label wins."""
    out: dict[str, str] = {}
    for table in HTMLParser(html).css("table.snapshot-table2"):
        for tr in table.css("tr"):
            cells = [" ".join(td.text(separator=" ").split()) for td in tr.css("td")]
            for label, value in zip(cells[::2], cells[1::2], strict=False):
                if label:
                    out.setdefault(label, value)
    return out


def snapshot_problem(raw: dict[str, str]) -> str | None:
    """Why `raw` does not read as a snapshot table (a layout change), or None when it does."""
    if not raw:
        return "no snapshot table"
    if not any(label in raw for label in LABELS.values()):
        return f"snapshot table has none of the labels {sorted(LABELS.values())}"
    return None


def _number(text: str) -> Decimal | None:
    text = text.strip().replace(",", "")
    if text in ("", "-"):
        return None
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None
    return value if value.is_finite() else None


def _percent(text: str) -> Decimal | None:
    """ "32.57%" -> 0.3257. Text without the percent sign is not a percentage: None."""
    text = text.strip()
    if not text.endswith("%"):
        return None
    value = _number(text[:-1])
    return None if value is None else value.scaleb(-2)


def _scaled(text: str) -> Decimal | None:
    """ "4977.64B" -> 4977640000000, "850.5M" -> 850500000; a plain number is taken as it is."""
    text = text.strip()
    if text[-1:] in _SCALES:
        value = _number(text[:-1])
        return None if value is None else value.scaleb(_SCALES[text[-1]])
    return _number(text)


def _next_earnings(text: str, today: date) -> date | None:
    """The date of an "Earnings" cell, when it is today or later. FinViz keeps showing the last report's
    date until the next one is known: that is not a next earnings date, so it gives None."""
    m = _EARNINGS_RE.match(text.strip())
    if m is None:
        return None
    parsed = parse_earnings(f"{m.group(1)}/{'b' if m.group(2) == 'BMO' else 'a'}", today)
    if parsed is None or parsed[0] < today:
        return None
    return parsed[0]


def to_fundamentals(raw: dict[str, str], today: date) -> FinvizFundamentals:
    """`raw` is `parse_snapshot`'s result; `today` (the ET date) resolves the year FinViz leaves out of the
    earnings date."""

    def text(field: str) -> str:
        return raw.get(LABELS[field], "")

    return FinvizFundamentals(
        eps_growth_yoy=_percent(text("eps_growth_yoy")),
        debt_to_equity=_number(text("debt_to_equity")),
        book_value_per_share=_number(text("book_value_per_share")),
        payout_ratio=_percent(text("payout_ratio")),
        short_float=_percent(text("short_float")),
        next_earnings_date=_next_earnings(text("next_earnings_date"), today),
        rsi14=_number(text("rsi14")),
        market_cap_usd=_scaled(text("market_cap_usd")),
    )
