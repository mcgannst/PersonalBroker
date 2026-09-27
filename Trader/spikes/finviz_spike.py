"""FinViz fetch + parse spike (Phase 0, S5).

Reusable pieces:
  FinvizClient        polite HTTP client (>=1s between requests, desktop UA, status log)
  parse_screener(html) -> (total_count, header, rows[dict])
  screen(client, filters, view=111, signal=None) -> (total, rows)   pages r=1,21,41,...
  parse_quote_news(html, ref_date) -> [dict(ts, date_raw, headline, source, url)]

Depends only on requests + lxml (both present in system python3 here).
Structural hooks the parser relies on (risk if FinViz changes markup):
  screener: table.screener_table, first <tr> = <th> header, rows tr.styled-row,
            ticker from td[@data-boxover-ticker], total from .count-text "#1 / N Total"
  quote:    table#news-table, rows <tr> with td[0]=date/time, a.tab-link-news = headline,
            source in a span inside div.news-link-right (e.g. "(Reuters)")
"""
from __future__ import annotations

import csv
import re
import time
from datetime import date, datetime

import lxml.html
import requests

BASE = "https://finviz.com"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")


class FinvizClient:
    def __init__(self, min_interval: float = 1.1, user_agent: str | None = UA):
        self.min_interval = min_interval
        self.s = requests.Session()
        if user_agent is not None:
            self.s.headers["User-Agent"] = user_agent
            self.s.headers["Accept"] = "text/html,application/xhtml+xml"
            self.s.headers["Accept-Language"] = "en-US,en;q=0.9"
        self._last = 0.0
        self.log: list[tuple[str, int, int, float]] = []  # url, status, bytes, secs

    def get(self, path_or_url: str, headers: dict | None = None) -> requests.Response:
        wait = self.min_interval - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        url = path_or_url if path_or_url.startswith("http") else BASE + path_or_url
        t0 = time.monotonic()
        r = self.s.get(url, headers=headers, timeout=20)
        self._last = time.monotonic()
        self.log.append((url, r.status_code, len(r.content), self._last - t0))
        return r


def looks_blocked(r: requests.Response) -> str | None:
    """Return a reason string if the response looks like a block/interstitial."""
    if r.status_code in (403, 429, 503):
        return f"HTTP {r.status_code}"
    if len(r.content) < 1000:  # empty UA -> silent 200 with 0-byte body
        return f"empty body ({len(r.content)} bytes)"
    t = r.text[:5000].lower()
    for marker in ("just a moment", "cf-challenge", "captcha", "attention required"):
        if marker in t:
            return f"interstitial:{marker}"
    return None


# ---------------------------------------------------------------- screener
_TOTAL_RE = re.compile(r"/\s*([\d,]+)\s*Total")


def _num(s: str):
    s = s.strip().replace(",", "")
    if s in ("", "-"):
        return None
    mult = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}
    try:
        if s.endswith("%"):
            return float(s[:-1])
        if s[-1] in mult:
            return float(s[:-1]) * mult[s[-1]]
        return float(s)
    except ValueError:
        return s


def parse_screener(html: str):
    doc = lxml.html.fromstring(html)
    total = 0
    for el in doc.xpath('//*[contains(@class,"count-text")]'):
        m = _TOTAL_RE.search(el.text_content())
        if m:
            total = int(m.group(1).replace(",", ""))
            break
    tables = doc.xpath('//table[contains(@class,"screener_table")]')
    if not tables:
        return total, [], []
    trs = tables[0].xpath("./tr|./tbody/tr|./thead/tr")
    header = [th.text_content().strip() for th in trs[0].xpath("./th")]
    rows = []
    for tr in trs[1:]:
        tds = tr.xpath("./td")
        if len(tds) != len(header):
            continue
        rec = {}
        for h, td in zip(header, tds):
            if h == "Ticker":
                tick = td.get("data-boxover-ticker")
                if not tick:
                    a = td.xpath('.//a[contains(@class,"company-ticker")]')
                    tick = a[0].text_content().strip() if a else td.text_content().strip()
                rec[h] = tick
            else:
                rec[h] = td.text_content().strip()
        rows.append(rec)
    return total, header, rows


def screen(client: FinvizClient, filters: str, view: int = 111, signal: str | None = None,
           max_pages: int = 100):
    """Fetch every page of a screen. Returns (stated_total, header, rows)."""
    rows, header, total = [], [], None
    for page in range(max_pages):
        r_ = 1 + 20 * page
        q = f"/screener.ashx?v={view}&f={filters}&r={r_}"
        if signal:
            q += f"&s={signal}"
        resp = client.get(q)
        why = looks_blocked(resp)
        if why:
            raise RuntimeError(f"blocked on {q}: {why}")
        t, header, page_rows = parse_screener(resp.text)
        total = t if total is None else total
        rows.extend(page_rows)
        if not page_rows or len(rows) >= total:
            break
    return total or 0, header, rows


def normalise_overview(rec: dict) -> dict:
    """v=111 row -> typed dict for universe.csv."""
    return {
        "ticker": rec.get("Ticker"),
        "company": rec.get("Company"),
        "sector": rec.get("Sector"),
        "industry": rec.get("Industry"),
        "country": rec.get("Country"),
        "market_cap": _num(rec.get("Market Cap", "")),
        "pe": _num(rec.get("P/E", "")),
        "price": _num(rec.get("Price", "")),
        "change_pct": _num(rec.get("Change %", rec.get("Change", ""))),
        "volume": _num(rec.get("Volume", "")),
    }


# ---------------------------------------------------------------- quote news
_DATE_RE = re.compile(r"^(?:(Today)|([A-Z][a-z]{2}-\d{2}-\d{2}))\s*(\d{1,2}:\d{2}[AP]M)?$")


def parse_quote_news(html: str, ref_date: date | None = None, limit: int | None = None):
    """Parse #news-table. FinViz shows the date only on the first row of each day
    ("Sep-26-26 08:00AM" or "Today 08:00AM"); later rows show only "08:00AM", so we
    carry the last-seen date forward. Times are US/Eastern (naive here)."""
    ref_date = ref_date or date.today()
    doc = lxml.html.fromstring(html)
    tbl = doc.xpath('//table[@id="news-table"]')
    if not tbl:
        return []
    out, cur_date = [], None
    for tr in tbl[0].xpath(".//tr"):
        tds = tr.xpath("./td")
        a = tr.xpath('.//a[contains(@class,"tab-link-news")]')
        if len(tds) < 2 or not a:
            continue
        raw = " ".join(tds[0].text_content().split())
        m = _DATE_RE.match(raw)
        if m and m.group(1):
            cur_date = ref_date
        elif m and m.group(2):
            cur_date = datetime.strptime(m.group(2), "%b-%d-%y").date()
        tstr = m.group(3) if m else raw
        ts = None
        if cur_date and tstr:
            ts = datetime.combine(cur_date, datetime.strptime(tstr, "%I:%M%p").time())
        src_el = tr.xpath('.//div[contains(@class,"news-link-right")]//span')
        source = src_el[0].text_content().strip().strip("()") if src_el else ""
        href = a[0].get("href", "")
        if href.startswith("/"):
            href = BASE + href
        out.append({"ts": ts.isoformat() if ts else None, "date_raw": raw,
                    "headline": a[0].text_content().strip(), "source": source, "url": href})
        if limit and len(out) >= limit:
            break
    return out


def write_csv(path: str, rows: list[dict]):
    if not rows:
        return
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
