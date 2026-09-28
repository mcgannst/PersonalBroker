"""All FinViz HTML parsing lives here (SPEC §4.2 isolation). Tested against saved pages.

Page markers relied on (spike S5): table.screener_table with a <th> header row; the ticker in
td[data-boxover-ticker]; the result count, #screener-total (or a ".count-text" element) whose whole
text is "#1 / N Total", or just "0 Total" on a page that matched nothing (that page has no
table.screener_table at all; saved in tests/fixtures/finviz/raw_screener_zero.html);
table#news-table rows whose first cell is "Sep-25-26 04:18PM", "Today 06:07AM" or just "04:02PM";
a.tab-link-news headlines; the source in a span inside div.news-link-right.

The parser never raises on odd HTML. Instead it reports what it could not read (ScreenerPage.bad_rows,
NewsPage.problem) so the scraper can turn a layout change into an error instead of a silently
short result.
"""

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Literal

from selectolax.parser import HTMLParser, Node

from trader.market.clock import ET

BASE = "https://finviz.com"
# The whole count text: "#1 / 695 Total", "#21 / 695 Total", or "0 Total" (no matches).
_TOTAL_RE = re.compile(r"^(?:#[\d,]+\s*/\s*)?([\d,]+)\s+Total$")
_DATE_RE = re.compile(r"^(?:(Today)|([A-Z][a-z]{2}-\d{2}-\d{2}))?\s*(\d{1,2}:\d{2}[AP]M)$")
_BLOCK_MARKERS = ("just a moment", "cf-challenge", "captcha", "attention required")
MIN_PAGE_BYTES = 1000  # anything shorter is an empty body, not a real FinViz page
UNIVERSE_COLUMNS = ("Ticker", "Company", "Sector", "Industry", "Price", "Volume")
BLOCK_STATUSES = frozenset({403, 429, 503})
# The Earnings column of the custom view (v=152, column id 68; verified live 2026-09-27): "Sep 25/a" is
# after the close, "Sep 25/b" before the open.
EARNINGS_COLUMN = "Earnings"
EARNINGS_VIEW = 152
EARNINGS_COLUMNS = "0,1,2,68"  # No., Ticker, Company, Earnings
_EARNINGS_RE = re.compile(r"^([A-Z][a-z]{2}) (\d{1,2})/([ab])$")
_MONTHS = {
    m: i
    for i, m in enumerate(
        ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), start=1
    )
}
type EarningsTime = Literal["a", "b"]


@dataclass(frozen=True, slots=True)
class ScreenerPage:
    total: int | None  # None: no result count on the page (a layout change, never "0")
    header: list[str]
    rows: list[dict[str, str]]
    bad_rows: int = 0  # data rows whose cell count differed from the header (not in `rows`)
    has_table: bool = True  # False: the page had no table.screener_table

    @property
    def verified_empty(self) -> bool:
        """FinViz's "matched nothing" page: a count of exactly 0, no rows (good or bad), and either no
        results table or one whose header has a Ticker column. Anything else short is a problem."""
        return (
            self.total == 0
            and not self.rows
            and not self.bad_rows
            and (not self.has_table or "Ticker" in self.header)
        )


@dataclass(frozen=True, slots=True)
class UniverseRow:
    ticker: str
    company: str
    sector: str
    industry: str
    price: Decimal | None
    volume: int | None


@dataclass(frozen=True, slots=True)
class Headline:
    ts: datetime
    title: str
    source: str
    url: str


@dataclass(frozen=True, slots=True)
class NewsPage:
    headlines: list[Headline]
    problem: str | None  # set when the page looks like a layout change rather than "no news"


def to_questrade_ticker(ticker: str) -> str:
    """FinViz writes share classes with '-', Questrade with '.' (spike S4: BF-B -> BF.B)."""
    return ticker.strip().replace("-", ".")


def to_finviz_ticker(ticker: str) -> str:
    """The inverse of to_questrade_ticker: BF.B -> BF-B, BRK.B -> BRK-B."""
    return ticker.strip().replace(".", "-")


def blocked_reason(status: int, body: str) -> str | None:
    if status in BLOCK_STATUSES:
        return f"HTTP {status}"
    if len(body) < MIN_PAGE_BYTES:
        return f"empty body ({len(body)} bytes)"
    head = body[:5000].lower()
    for marker in _BLOCK_MARKERS:
        if marker in head:
            return f"interstitial: {marker}"
    return None


def _cell_text(node: Node) -> str:
    return " ".join(node.text(separator=" ").split())


def _screener_total(tree: HTMLParser) -> int | None:
    """The result count, or None when no count element's whole text reads as one."""
    for node in [*tree.css("#screener-total"), *tree.css(".count-text")]:
        m = _TOTAL_RE.match(_cell_text(node))
        if m:
            return int(m.group(1).replace(",", ""))
    return None


def parse_screener(html: str) -> ScreenerPage:
    tree = HTMLParser(html)
    total = _screener_total(tree)
    table = tree.css_first("table.screener_table")
    if table is None:
        return ScreenerPage(total, [], [], has_table=False)
    trs = table.css("tr")
    header = [_cell_text(th) for th in trs[0].css("th")] if trs else []
    rows: list[dict[str, str]] = []
    bad_rows = 0
    for tr in trs[1:]:
        tds = tr.css("td")
        if len(tds) != len(header):
            bad_rows += 1
            continue
        rec: dict[str, str] = {}
        for name, td in zip(header, tds, strict=True):
            if name == "Ticker":
                rec[name] = (td.attributes.get("data-boxover-ticker") or _cell_text(td)).strip()
            else:
                rec[name] = _cell_text(td)
        rows.append(rec)
    return ScreenerPage(total, header, rows, bad_rows)


def parse_earnings(text: str, near: date) -> tuple[date, EarningsTime] | None:
    """An Earnings cell ("Sep 25/a" = after the close, "Sep 25/b" = before the open) as (date, "a"|"b").

    FinViz omits the year: the one of near.year - 1, near.year and near.year + 1 closest to `near` (the
    session asking) wins. Anything else (blank, "-", no /a or /b mark, an impossible date) gives None;
    the caller must count such a row, never drop it silently.
    """
    m = _EARNINGS_RE.match(text.strip())
    if m is None:
        return None
    month = _MONTHS.get(m.group(1))
    if month is None:
        return None
    day = int(m.group(2))
    candidates: list[date] = []
    for year in (near.year - 1, near.year, near.year + 1):
        try:
            candidates.append(date(year, month, day))
        except ValueError:
            continue
    if not candidates:
        return None
    when: EarningsTime = "a" if m.group(3) == "a" else "b"
    return min(candidates, key=lambda d: abs((d - near).days)), when


def _decimal(text: str) -> Decimal | None:
    """A finite Decimal, or None for blank, "-", garbage, NaN and Infinity."""
    if text in ("", "-"):
        return None
    try:
        value = Decimal(text.replace(",", ""))
    except InvalidOperation:
        return None
    return value if value.is_finite() else None


def parse_universe_row(rec: dict[str, str]) -> UniverseRow:
    volume = _decimal(rec.get("Volume", ""))
    return UniverseRow(
        ticker=to_questrade_ticker(rec["Ticker"]),
        company=rec.get("Company", ""),
        sector=rec.get("Sector", ""),
        industry=rec.get("Industry", ""),
        price=_decimal(rec.get("Price", "")),
        volume=int(volume) if volume is not None else None,
    )


def parse_news(html: str, today_et: date) -> list[Headline]:
    """The headlines only; use parse_news_page to also learn whether the layout looked wrong."""
    return parse_news_page(html, today_et).headlines


def parse_news_page(html: str, today_et: date) -> NewsPage:
    """FinViz shows the date only on each day's first row; later rows carry it forward.

    "Today" means `today_et`, which must be the ET date the page was fetched. A problem is
    reported when a real page (≥ MIN_PAGE_BYTES) has no news table, or when the table has
    headline rows but none of them parse.
    """
    table = HTMLParser(html).css_first("table#news-table")
    if table is None:
        problem = "no table#news-table" if len(html) >= MIN_PAGE_BYTES else None
        return NewsPage([], problem)
    out: list[Headline] = []
    current: date | None = None
    headline_rows = 0
    for tr in table.css("tr"):
        tds = tr.css("td")
        link = tr.css_first("a.tab-link-news")
        if link is not None:
            headline_rows += 1
        if len(tds) < 2 or link is None:
            continue
        m = _DATE_RE.match(_cell_text(tds[0]))
        if m is None:
            continue
        if m.group(1):
            current = today_et
        elif m.group(2):
            current = datetime.strptime(m.group(2), "%b-%d-%y").date()  # noqa: DTZ007
        if current is None:
            continue
        local = datetime.combine(current, datetime.strptime(m.group(3), "%I:%M%p").time(), tzinfo=ET)  # noqa: DTZ007
        src = tr.css_first("div.news-link-right span")
        href = link.attributes.get("href") or ""
        out.append(
            Headline(
                ts=local.astimezone(UTC),
                title=_cell_text(link),
                source=_cell_text(src).strip("()") if src else "",
                url=BASE + href if href.startswith("/") else href,
            )
        )
    if headline_rows and not out:
        return NewsPage([], f"none of {headline_rows} headline rows parsed")
    return NewsPage(out, None)
