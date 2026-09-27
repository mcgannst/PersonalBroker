"""P2-T14 gauntlet: try to break the pre-market scan (`run_premarket`) and the `premarket` CLI.

Everything external is faked: FinViz (a fake screener, or the real scraper over an httpx MockTransport),
Questrade (FakeQuestrade) and Claude (a fake `messages.create`). The database is the testcontainers one.
"""

import asyncio
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

from tests.factories import add_symbol
from tests.fakes_questrade import FakeQuestrade
from trader.adapters.claude.catalyst import (
    BUDGET_REACHED,
    OVER_CAP,
    CatalystClassifier,
    CatalystService,
    CatalystStore,
)
from trader.adapters.finviz.parser import Headline, ScreenerPage
from trader.adapters.finviz.scraper import FinvizBlocked, FinvizScraper
from trader.adapters.questrade.client import QuestradeApiError
from trader.adapters.questrade.models import QtQuote
from trader.db import models as m
from trader.jobs import premarket as pm
from trader.jobs.premarket import PremarketDeps, run_premarket
from trader.jobs.runner import run_job
from trader.market.calendar import SessionCalendar
from trader.market.clock import FixedClock
from trader.market.data_service import MarketDataService
from trader.settings_store import RuntimeSettings

pytestmark = pytest.mark.db
CAL = SessionCalendar()
DAY, PREV = date(2026, 10, 6), date(2026, 10, 5)  # Tue, Mon
CLOCK = FixedClock(datetime(2026, 10, 6, 12, 0, tzinfo=UTC))  # 08:00 ET
FILTERS = "ind_stocksonly,sh_price_5to50,sh_avgvol_o1000,ta_averagetruerange_o0.5,geo_usa"
GOOD = {
    "catalyst_type": "earnings_beat",
    "direction": "bullish",
    "quality": 82,
    "is_confirmed": True,
    "reason": "Beat estimates and raised guidance.",
}
# ticker -> (Questrade id, pre-market last or None for no quote, prior close or None for no daily candle)
Book = dict[str, tuple[int, str | None, str | None]]
BOOK: Book = {
    "AAA": (101, "21.00", "20.00"),  # +5.00% gap
    "BBB": (102, "19.00", "20.00"),  # -5.00% gap
    "CCC": (103, "20.30", "20.00"),  # +1.50%
    "DDD": (104, "20.10", "20.00"),  # +0.50%
    "SPY": (199, "21.00", "20.00"),  # +5.00%, but SPY is never a candidate
}
SECRET = "sk-ant-api03-BREAKER-SENTINEL-DO-NOT-LEAK"


# --- fakes ---


class FakeFinviz:
    """news_date_today -> `news`, anything else -> `earnings`; either may be an exception to raise."""

    def __init__(
        self, news: list[str] | Exception | None = None, earnings: list[str] | Exception | None = None
    ) -> None:
        self.news_rows = news if news is not None else []
        self.earnings_rows = earnings if earnings is not None else []
        self.screens: list[str] = []
        self.news_calls: list[str] = []

    def screen(self, filters: str, view: int = 111, signal: str | None = None) -> ScreenerPage:
        self.screens.append(filters)
        got = self.news_rows if "news_date_today" in filters else self.earnings_rows
        if isinstance(got, Exception):
            raise got
        rows = [{"No.": str(i), "Ticker": t} for i, t in enumerate(got, start=1)]
        return ScreenerPage(len(got), ["No.", "Ticker"], rows)

    def news(self, ticker: str, today_et: date) -> list[Headline]:
        self.news_calls.append(ticker)
        return [
            Headline(datetime(2026, 10, 6, 11, 0, tzinfo=UTC), f"{ticker} headline", "Reuters", "https://x")
        ]


def _ticker_of(kwargs: dict[str, Any]) -> str:
    first = str(kwargs["messages"][0]["content"]).splitlines()[0]
    return first.removeprefix("Ticker: ").strip()


class FakeMessages:
    def __init__(self, fail: dict[str, Exception] | None = None, *, suspend: bool = False) -> None:
        self.fail = fail or {}
        self.suspend = suspend
        self.calls: list[dict[str, Any]] = []

    def tickers(self) -> list[str]:
        return [_ticker_of(c) for c in self.calls]

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.suspend:
            await asyncio.sleep(0)
        ticker = _ticker_of(kwargs)
        if ticker in self.fail:
            raise self.fail[ticker]
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=json.dumps(GOOD))],
            usage=SimpleNamespace(input_tokens=500, output_tokens=50),  # US$0.0015 per call on sonnet-5
            stop_reason="end_turn",
        )


class DownQuestrade(FakeQuestrade):
    """Every quote request fails, as when Questrade has an outage at 08:00 ET."""

    async def quotes(self, ids: Any) -> list[QtQuote]:
        raise QuestradeApiError(503, "service unavailable")


# --- helpers ---


def seed(
    factory: sessionmaker[Session], book: Book = BOOK, day: date = DAY, prev: date = PREV
) -> dict[str, int]:
    ids: dict[str, int] = {}
    with factory() as s:
        for t, (qid, _, close) in book.items():
            ids[t] = add_symbol(s, t, questrade_id=qid)
            s.add(
                m.UniverseSnapshot(
                    session_date=day,
                    symbol_id=ids[t],
                    price=Decimal("20"),
                    avg_volume=2_000_000,
                    atr14=Decimal("1"),
                    source="finviz",
                )
            )
            if close is not None:
                s.add(
                    m.DailyCandle(
                        symbol_id=ids[t],
                        date=prev,
                        open=Decimal(close),
                        high=Decimal(close),
                        low=Decimal(close),
                        close=Decimal(close),
                        volume=1,
                        vwap=None,
                    )
                )
        s.commit()
    return ids


def make_deps(
    factory: sessionmaker[Session],
    finviz: Any,
    messages: FakeMessages,
    *,
    book: Book = BOOK,
    qt: FakeQuestrade | None = None,
    clock: FixedClock = CLOCK,
    concurrency: int = 4,
    headlines: Any = None,
    **settings: Any,
) -> PremarketDeps:
    s = RuntimeSettings(**settings)
    fq = qt if qt is not None else FakeQuestrade()
    for t, (qid, last, _) in book.items():
        fq.add_symbol(t, qid)
        if last is not None:
            fq.set_quote(qid, last, last, last, clock.now())
    client = SimpleNamespace(messages=messages, api_key=SECRET)
    service = CatalystService(
        factory,
        clock,
        CatalystStore(factory, clock),
        CatalystClassifier(client, lambda: s),
        headlines if headlines is not None else finviz,
        max_concurrency=concurrency,
    )
    return PremarketDeps(factory, clock, finviz, MarketDataService(factory, clock, CAL, fq), service, s)


def catalysts(factory: sessionmaker[Session]) -> dict[int, m.Catalyst]:
    with factory() as s:
        return {r.symbol_id: r for r in s.execute(select(m.Catalyst)).scalars()}


def brief_lines(out: dict[str, Any]) -> list[str]:
    return str(out["brief"]).splitlines()


# --- 1. FinViz "0 Total" empty screen (known LIVE finding) ---

PAD = "<!--" + "x" * 2000 + "-->"


def _full_market_page() -> str:
    return (
        f'<html><body>{PAD}<div class="count-text">#1 / 9000 Total</div>'
        '<table class="screener_table"><tr><th>No.</th><th>Ticker</th></tr>'
        '<tr><td>1</td><td data-boxover-ticker="A">A</td></tr></table></body></html>'
    )


def _zero_total_page() -> str:
    # What FinViz serves for a screen that matches nothing: a count of 0 and no results table at all.
    return f'<html><body>{PAD}<div class="count-text">0 Total</div><p>No results found.</p></body></html>'


async def test_finviz_zero_total_screen_is_empty_not_a_failure(db_factory: sessionmaker[Session]) -> None:
    """On a quiet morning FinViz's news/earnings screen legitimately matches nothing and renders a
    "0 Total" page without a table. That is an empty result, not a failed screen: the brief must not
    say "FinViz screens failed" every quiet day (the LIVE run on 2026-09-28 did exactly that)."""
    seed(db_factory)

    def handler(request: httpx.Request) -> httpx.Response:
        f = request.url.params.get("f", "")
        return httpx.Response(200, text=_full_market_page() if f == "" else _zero_total_page())

    http = httpx.Client(transport=httpx.MockTransport(handler))
    scraper = FinvizScraper(http, min_interval_s=2.0, cache_dir=None, sleep=lambda _s: None)
    try:
        news = FakeFinviz()  # headlines only; the screens go through the real scraper
        out = await run_premarket(make_deps(db_factory, scraper, FakeMessages(), headlines=news), DAY)
        assert out["screen_errors"] == [], out["screen_errors"]
        assert "FinViz screens failed" not in out["brief"]
        assert out["candidates"] == 2  # AAA and BBB by gap
        page = scraper.screen(f"{FILTERS},news_date_today")
        assert page.total == 0 and page.rows == []
    finally:
        http.close()


# --- 2. one screen failing while the other works ---


async def test_one_failed_screen_does_not_lose_the_other(db_factory: sessionmaker[Session]) -> None:
    seed(db_factory)
    finviz = FakeFinviz(news=FinvizBlocked("HTTP 403"), earnings=["DDD"])
    out = await run_premarket(make_deps(db_factory, finviz, FakeMessages()), DAY)
    assert out["screen_errors"] == ["news: HTTP 403"]
    assert out["candidates"] == 3  # AAA, BBB by gap + DDD from earnings
    assert any(line.startswith("DDD +0.50% [earnings]") for line in brief_lines(out))
    assert "FinViz screens failed: news: HTTP 403" in out["brief"]
    assert "earnings:" not in out["brief"]


# --- 3. Questrade quotes failing for some or all names ---


@pytest.mark.parametrize("outage", ["some", "all"])
async def test_quote_failures_keep_screen_candidates(db_factory: sessionmaker[Session], outage: str) -> None:
    """Missing quotes mean an unknown gap, never a crashed scan: names the FinViz screens flagged must
    still be classified and briefed (gap n/a), exactly like a failed screen leaves the gaps. With all
    quotes down (a Questrade outage at 08:00 ET) the scan must still produce the news/earnings brief."""
    book: Book = dict(BOOK)
    book["CCC"] = (103, None, "20.00")  # no quote at all for CCC
    seed(db_factory, book)
    qt = DownQuestrade() if outage == "all" else FakeQuestrade()
    finviz = FakeFinviz(news=["CCC"], earnings=["DDD"])
    msgs = FakeMessages()
    out = await run_premarket(make_deps(db_factory, finviz, msgs, book=book, qt=qt), DAY)
    lines = brief_lines(out)
    assert any(line.startswith("CCC gap n/a [news]") for line in lines), lines
    if outage == "some":
        assert out["candidates"] == 4  # AAA, BBB (gaps), CCC (news, no quote), DDD (earnings)
        assert lines[1].startswith("AAA +5.00%") and lines[-1].startswith("CCC gap n/a")  # unknown last
    else:
        assert out["candidates"] == 2 and sorted(msgs.tickers()) == ["CCC", "DDD"]


# --- 4. more candidates than top-N ---


@pytest.mark.parametrize("cap", [3, 0])
async def test_over_cap_names_are_ranked_stored_and_reported(
    db_factory: sessionmaker[Session], cap: int
) -> None:
    book: Book = {
        "AAA": (101, "21.00", "20.00"),  # +5.00%
        "BBB": (102, "19.00", "20.00"),  # -5.00% (ties AAA; ticker order)
        "CCC": (103, "20.80", "20.00"),  # +4.00%
        "DDD": (104, "20.70", "20.00"),  # +3.50%
        "EEE": (105, None, "20.00"),  # news, no quote: unknown gap, ranked last
        "FFF": (106, "20.10", "20.00"),  # earnings, +0.50%
    }
    ids = seed(db_factory, book)
    finviz = FakeFinviz(news=["EEE"], earnings=["FFF"])
    msgs = FakeMessages()
    out = await run_premarket(
        make_deps(db_factory, finviz, msgs, book=book, claude_premarket_max_candidates=cap), DAY
    )
    order = ["AAA", "BBB", "CCC", "DDD", "FFF", "EEE"]
    assert out["candidates"] == 6
    assert out["over_cap"] == order[cap:]
    assert out["classified"] == cap
    assert sorted(msgs.tickers()) == order[:cap]
    assert finviz.news_calls == order[:cap]  # over-cap names never cost a headline fetch either
    assert brief_lines(out)[0] == "Pre-market brief for 2026-10-06: 6 candidates"
    assert "Not classified (over cap): " + ", ".join(order[cap:]) in out["brief"]
    rows = catalysts(db_factory)
    assert len(rows) == 6
    for t in order[cap:]:
        assert rows[ids[t]].catalyst_type == "unknown" and rows[ids[t]].reason == OVER_CAP
        assert rows[ids[t]].classified_at is None
    assert rows[ids["FFF"]].earnings_date == DAY and rows[ids["DDD"]].gap_pct == Decimal("0.0350")


# --- 5. the Claude budget running out mid-run ---


async def test_budget_exhausted_mid_run(db_factory: sessionmaker[Session]) -> None:
    """Budget for exactly two calls (2 x US$0.0015). The rest are stored unknown with the budget reason,
    shown as unknown in the brief, and one error alert is raised."""
    ids = seed(db_factory)
    finviz = FakeFinviz(news=["CCC", "DDD"])
    msgs = FakeMessages()
    d = make_deps(db_factory, finviz, msgs, concurrency=1, claude_daily_budget_usd=Decimal("0.003"))
    out = await run_premarket(d, DAY)
    assert out["candidates"] == 4 and out["over_cap"] == []
    assert len(msgs.calls) == 2 and out["classified"] == 2
    assert msgs.tickers() == ["AAA", "BBB"]  # the biggest movers get the budget
    rows = catalysts(db_factory)
    for t in ("CCC", "DDD"):
        assert rows[ids[t]].catalyst_type == "unknown" and "budget" in (rows[ids[t]].reason or "")
    lines = brief_lines(out)
    assert lines[3].startswith("CCC +1.50% [news] unknown (") and "budget" in lines[3]
    assert CatalystStore(db_factory, CLOCK).spent(DAY) == Decimal("0.003000")
    with db_factory() as s:
        alerts = s.execute(
            select(func.count())
            .select_from(m.EventLog)
            .where(m.EventLog.level == "error", m.EventLog.message.startswith(BUDGET_REACHED))
        ).scalar_one()
    assert alerts == 1


# --- 6. a Claude exception for one name ---


async def test_claude_error_for_one_name_does_not_stop_the_others(db_factory: sessionmaker[Session]) -> None:
    ids = seed(db_factory)
    finviz = FakeFinviz(news=["CCC", "DDD"])
    msgs = FakeMessages(fail={"BBB": TimeoutError("request timed out")}, suspend=True)
    out = await run_premarket(make_deps(db_factory, finviz, msgs), DAY)
    assert out["candidates"] == 4 and out["classified"] == 3
    assert sorted(msgs.tickers()) == ["AAA", "BBB", "CCC", "DDD"]
    rows = catalysts(db_factory)
    assert rows[ids["BBB"]].catalyst_type == "unknown" and rows[ids["BBB"]].classified_at is None
    assert all(rows[ids[t]].catalyst_type == "earnings_beat" for t in ("AAA", "CCC", "DDD"))
    bbb = next(line for line in brief_lines(out) if line.startswith("BBB "))
    assert bbb.startswith("BBB -5.00% [gap] unknown (") and "TimeoutError" in bbb


# --- 7. names outside the universe, SPY, share classes and duplicates across the lists ---


async def test_outside_universe_spy_and_duplicates(db_factory: sessionmaker[Session]) -> None:
    book: Book = dict(BOOK)
    book["BF.B"] = (107, "20.00", "20.00")  # flat: only the screens can flag it
    ids = seed(db_factory, book)
    finviz = FakeFinviz(news=["AAA", "ZZZ", "SPY", "BF-B", "AAA", "aaa "], earnings=["AAA", "BF-B", "QQQ"])
    msgs = FakeMessages()
    out = await run_premarket(make_deps(db_factory, finviz, msgs, book=book), DAY)
    assert out["candidates"] == 3  # AAA (gap+news+earnings), BBB (gap), BF.B (news+earnings)
    lines = brief_lines(out)
    assert lines[1].startswith("AAA +5.00% [earnings, gap, news]")
    assert any(line.startswith("BF.B +0.00% [earnings, news]") for line in lines)
    assert not any(line.split(" ")[0] in {"ZZZ", "SPY", "QQQ", "aaa"} for line in lines[1:])
    assert sorted(msgs.tickers()) == ["AAA", "BBB", "BF.B"]  # one call per name, never twice
    assert sorted(finviz.news_calls) == ["AAA", "BBB", "BF.B"]
    rows = catalysts(db_factory)
    assert set(rows) == {ids["AAA"], ids["BBB"], ids["BF.B"]}
    assert rows[ids["AAA"]].earnings_date == DAY


# --- 8. early close and holidays ---


async def test_early_close_day_uses_the_session_before_the_holiday(db_factory: sessionmaker[Session]) -> None:
    """Friday 2026-11-27 closes at 13:00 ET and follows Thanksgiving: the prior close is Wednesday's."""
    day, prev = date(2026, 11, 27), date(2026, 11, 25)
    assert CAL.is_session(day) and not CAL.is_session(date(2026, 11, 26))
    clock = FixedClock(datetime(2026, 11, 27, 13, 0, tzinfo=UTC))  # 08:00 ET
    seed(db_factory, day=day, prev=prev)
    out = await run_premarket(make_deps(db_factory, FakeFinviz(), FakeMessages(), clock=clock), day)
    assert out["candidates"] == 2
    assert brief_lines(out)[1].startswith("AAA +5.00% [gap]")


@pytest.mark.parametrize(
    ("args", "now"),
    [
        (["premarket", "--date", "2026-11-26"], datetime(2026, 11, 26, 13, 0, tzinfo=UTC)),  # Thanksgiving
        (["premarket"], datetime(2026, 11, 26, 13, 0, tzinfo=UTC)),  # Thanksgiving, default ET date
        (["premarket"], datetime(2026, 10, 10, 12, 0, tzinfo=UTC)),  # Saturday
    ],
)
def test_cli_does_nothing_on_a_holiday_or_weekend(
    monkeypatch: pytest.MonkeyPatch, args: list[str], now: datetime
) -> None:
    import trader.bootstrap
    import trader.jobs.runner
    from trader.cli import app

    core = SimpleNamespace(
        settings=SimpleNamespace(load=RuntimeSettings),
        clock=FixedClock(now),
        calendar=SessionCalendar(),
        env=SimpleNamespace(anthropic_api_key=None),
    )
    monkeypatch.setattr(trader.bootstrap, "build_core", lambda *a, **k: core)

    def no_job(*a: Any, **k: Any) -> Any:
        raise AssertionError("run_job must not be called on a non-session day")

    monkeypatch.setattr(trader.jobs.runner, "run_job", no_job)
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    assert "not a trading session, nothing to do" in result.output


# --- 9. a re-run on the same date: skipped, vs --force ---


def test_rerun_is_skipped_and_force_makes_no_new_claude_calls(db_factory: sessionmaker[Session]) -> None:
    seed(db_factory)
    finviz = FakeFinviz(news=["CCC"])
    msgs = FakeMessages()
    d = make_deps(db_factory, finviz, msgs, claude_premarket_max_candidates=2)

    def job() -> dict[str, Any]:
        return asyncio.run(run_premarket(d, DAY))

    first = run_job(db_factory, CLOCK, "premarket", DAY, job)
    assert first.status == "succeeded" and first.detail["classified"] == 2
    calls, news = len(msgs.calls), len(finviz.news_calls)
    spent = CatalystStore(db_factory, CLOCK).spent(DAY)

    second = run_job(db_factory, CLOCK, "premarket", DAY, job)
    assert second.status == "skipped" and second.detail == {"reason": "already succeeded"}
    assert len(finviz.screens) == 2  # the skipped run did no work at all

    forced = run_job(db_factory, CLOCK, "premarket", DAY, job, force=True)
    assert forced.status == "succeeded"
    assert forced.detail["brief"] == first.detail["brief"]
    assert len(msgs.calls) == calls and len(finviz.news_calls) == news  # classified rows are reused
    assert CatalystStore(db_factory, CLOCK).spent(DAY) == spent
    with db_factory() as s:
        assert s.execute(select(func.count()).select_from(m.Catalyst)).scalar_one() == 3
        runs = s.execute(select(m.JobRun.status).where(m.JobRun.job == "premarket")).scalars().all()
    assert sorted(runs) == ["succeeded", "succeeded"]


# --- 10. gap maths: zero or missing prior close, zero last, the threshold, Decimal only ---


async def test_gap_edge_cases_and_decimal_only(
    db_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    book: Book = {
        "ZPC": (201, "21.00", "0"),  # prior close 0: no gap, never a division by zero
        "NPC": (202, "21.00", None),  # no prior close stored
        "ZLS": (203, "0", "20.00"),  # last 0 (no pre-market trade)
        "EXA": (204, "20.60", "20.00"),  # exactly +3.00%: flagged (>=)
        "UND": (205, "20.59", "20.00"),  # +2.95%: not flagged
        "HLF": (206, "20.333", "20.00"),  # +1.665% -> 0.0167 with ROUND_HALF_UP
        "NEG": (207, "19.40", "20.00"),  # exactly -3.00%: flagged
    }
    ids = seed(db_factory, book)
    captured: dict[str, Any] = {}
    real = pm.format_brief

    def spy(session_date: date, top: Any, cats: Any, over: Any, errors: Any) -> str:
        captured["candidates"] = [*top, *over]
        return real(session_date, top, cats, over, errors)

    monkeypatch.setattr(pm, "format_brief", spy)
    finviz = FakeFinviz(news=["ZPC", "NPC", "HLF"], earnings=["ZLS"])
    out = await run_premarket(make_deps(db_factory, finviz, FakeMessages(), book=book), DAY)

    by = {c.ticker: c for c in captured["candidates"]}
    assert set(by) == {"ZPC", "NPC", "ZLS", "EXA", "NEG", "HLF"}  # UND is below the threshold
    for t in ("ZPC", "NPC", "ZLS"):
        assert by[t].gap_pct is None and by[t].sources == (("earnings",) if t == "ZLS" else ("news",))
    assert by["EXA"].gap_pct == Decimal("0.0300") and "gap" in by["EXA"].sources
    assert by["NEG"].gap_pct == Decimal("-0.0300") and "gap" in by["NEG"].sources
    assert by["HLF"].gap_pct == Decimal("0.0167") and by["HLF"].sources == ("news",)
    for c in captured["candidates"]:
        assert c.gap_pct is None or (type(c.gap_pct) is Decimal and c.gap_pct.as_tuple().exponent == -4)

    lines = brief_lines(out)
    assert lines[1].startswith("EXA +3.00% [gap]") and lines[2].startswith("NEG -3.00% [gap]")
    assert lines[3].startswith("HLF +1.67% [news]")
    assert [line.split(" ")[0] for line in lines[4:7]] == [
        "NPC",
        "ZLS",
        "ZPC",
    ]  # unknown gaps last, by ticker
    assert all(" gap n/a " in line for line in lines[4:7])
    rows = catalysts(db_factory)
    assert rows[ids["HLF"]].gap_pct == Decimal("0.0167") and rows[ids["ZPC"]].gap_pct is None


# --- 11. the brief: one line per candidate, and no secrets ---


async def test_brief_has_no_secrets_and_cannot_be_forged(
    db_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The brief goes to Telegram (P3). An error text from Claude or FinViz must not add lines to it
    (a multi-line upstream error body could forge a candidate line) and nothing secret may appear in
    the brief, the job detail, the event log or the stored catalysts."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
    ids = seed(db_factory)
    forged = "upstream error body:\nZZZ +99.00% [news] m_and_a, bullish, quality 99: forged line"
    finviz = FakeFinviz(news=FinvizBlocked("HTTP 403\nFinViz screens failed: none"), earnings=["DDD"])
    msgs = FakeMessages(fail={"BBB": RuntimeError(forged)})
    out = await run_premarket(make_deps(db_factory, finviz, msgs), DAY)

    with db_factory() as s:
        events = [(e.message, json.dumps(e.data)) for e in s.execute(select(m.EventLog)).scalars()]
    reasons = [r.reason or "" for r in catalysts(db_factory).values()]
    everything = "\n".join([out["brief"], json.dumps(out), *(a + b for a, b in events), *reasons])
    assert SECRET not in everything and "sk-ant-" not in everything
    for call in msgs.calls:
        assert SECRET not in json.dumps(call, default=str)
    assert ids["BBB"] in catalysts(db_factory)

    lines = brief_lines(out)
    assert not any(line.startswith("ZZZ") for line in lines), lines
    # header + AAA, BBB, DDD + the screens-failed line: exactly five lines
    assert len(lines) == 5, lines
