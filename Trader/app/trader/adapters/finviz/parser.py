"""All FinViz HTML parsing lives here (SPEC §4.2 isolation). Tested against saved pages.

Page markers relied on (spike S5): table.screener_table with a <th> header row; the ticker in
td[data-boxover-ticker]; ".count-text" containing "#1 / N Total"; table#news-table rows whose
first cell is "Sep-25-26 04:18PM", "Today 06:07AM" or just "04:02PM"; a.tab-link-news headlines;
the source in a span inside div.news-link-right.
"""

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from selectolax.parser import HTMLParser, Node

# TODO(P1-T4): import ET from trader.market.clock once that module is on trunk.
ET = ZoneInfo("America/New_York")

BASE = "https://finviz.com"
_TOTAL_RE = re.compile(r"/\s*([\d,]+)\s*Total")
_DATE_RE = re.compile(r"^(?:(Today)|([A-Z][a-z]{2}-\d{2}-\d{2}))?\s*(\d{1,2}:\d{2}[AP]M)$")
_BLOCK_MARKERS = ("just a moment", "cf-challenge", "captcha", "attention required")


@dataclass(frozen=True, slots=True)
class ScreenerPage:
    total: int
    header: list[str]
    rows: list[dict[str, str]]


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


def to_questrade_ticker(ticker: str) -> str:
    """FinViz writes share classes with '-', Questrade with '.' (spike S4: BF-B -> BF.B)."""
    return ticker.strip().replace("-", ".")


def blocked_reason(status: int, body: str) -> str | None:
    if status in (403, 429, 503):
        return f"HTTP {status}"
    if len(body) < 1000:
        return f"empty body ({len(body)} bytes)"
    head = body[:5000].lower()
    for marker in _BLOCK_MARKERS:
        if marker in head:
            return f"interstitial: {marker}"
    return None


def _cell_text(node: Node) -> str:
    return " ".join(node.text(separator=" ").split())


def parse_screener(html: str) -> ScreenerPage:
    tree = HTMLParser(html)
    total = 0
    for node in tree.css(".count-text"):
        m = _TOTAL_RE.search(node.text())
        if m:
            total = int(m.group(1).replace(",", ""))
            break
    table = tree.css_first("table.screener_table")
    if table is None:
        return ScreenerPage(total, [], [])
    trs = table.css("tr")
    header = [_cell_text(th) for th in trs[0].css("th")] if trs else []
    rows: list[dict[str, str]] = []
    for tr in trs[1:]:
        tds = tr.css("td")
        if len(tds) != len(header):
            continue
        rec: dict[str, str] = {}
        for name, td in zip(header, tds, strict=True):
            if name == "Ticker":
                rec[name] = (td.attributes.get("data-boxover-ticker") or _cell_text(td)).strip()
            else:
                rec[name] = _cell_text(td)
        rows.append(rec)
    return ScreenerPage(total, header, rows)


def _decimal(text: str) -> Decimal | None:
    try:
        return Decimal(text.replace(",", "")) if text not in ("", "-") else None
    except InvalidOperation:
        return None


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
    """FinViz shows the date only on each day's first row; later rows carry it forward."""
    table = HTMLParser(html).css_first("table#news-table")
    if table is None:
        return []
    out: list[Headline] = []
    current: date | None = None
    for tr in table.css("tr"):
        tds = tr.css("td")
        link = tr.css_first("a.tab-link-news")
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
    return out
