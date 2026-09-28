import dataclasses
import json
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from tests.factories import add_symbol
from tests.fakes_questrade import FakeQuestrade
from trader.adapters.claude.catalyst import (
    OVER_CAP,
    CatalystClassifier,
    CatalystService,
    CatalystStore,
    StoredCatalyst,
)
from trader.adapters.finviz.parser import (
    EARNINGS_COLUMN,
    EARNINGS_COLUMNS,
    EARNINGS_VIEW,
    Headline,
    ScreenerPage,
    parse_screener,
)
from trader.adapters.finviz.scraper import FinvizBlocked
from trader.db import models as m
from trader.jobs.premarket import PremarketCandidate, PremarketDeps, brief_notes, format_brief, run_premarket
from trader.market.calendar import SessionCalendar
from trader.market.clock import FixedClock
from trader.market.data_service import MarketDataService
from trader.settings_store import EARNINGS_SESSION_WINDOW, LEGACY_EARNINGS_FILTER, RuntimeSettings

pytestmark = pytest.mark.db
CAL = SessionCalendar()
DAY, PREV = date(2026, 10, 6), date(2026, 10, 5)
CLOCK = FixedClock(datetime(2026, 10, 6, 12, 0, tzinfo=UTC))  # 08:00 ET
GOOD = {
    "catalyst_type": "earnings_beat",
    "direction": "bullish",
    "quality": 82,
    "is_confirmed": True,
    "reason": "Beat estimates and raised guidance.",
}
# ticker -> (Questrade id, pre-market last); every prior close is 20.00
BOOK = {
    "AAA": (101, "21.00"),
    "BBB": (102, "19.00"),
    "CCC": (103, "20.30"),
    "DDD": (104, "20.10"),
    "EEE": (105, "20.05"),
    "SPY": (199, "21.00"),
}


def page_of(filters: str, tickers: list[str], columns: str | None) -> ScreenerPage:
    """A screen's page. With `columns` (the earnings session window's v=152 screens) every row carries an
    Earnings value inside the window: after PREV's close on the prevdays5 screen, before DAY's open on
    today's."""
    if columns is None:
        return ScreenerPage(
            len(tickers), ["No.", "Ticker"], [{"No.": str(i), "Ticker": t} for i, t in enumerate(tickers)]
        )
    when = f"{PREV:%b %d}/a" if filters.endswith(",earningsdate_prevdays5") else f"{DAY:%b %d}/b"
    rows = [{"No.": str(i), "Ticker": t, "Earnings": when} for i, t in enumerate(tickers)]
    return ScreenerPage(len(tickers), ["No.", "Ticker", "Earnings"], rows)


class FakeFinviz:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.screens: list[str] = []
        self.news_calls: list[str] = []

    def screen(
        self, filters: str, view: int = 111, signal: str | None = None, *, columns: str | None = None
    ) -> ScreenerPage:
        self.screens.append(filters)
        if self.fail:
            raise FinvizBlocked("HTTP 403")
        tickers = ["AAA", "CCC", "FFF"] if "news_date_today" in filters else ["DDD"]
        return page_of(filters, tickers, columns)

    def news(self, ticker: str, today_et: date) -> list[Headline]:
        self.news_calls.append(ticker)
        return [
            Headline(datetime(2026, 10, 6, 11, 0, tzinfo=UTC), f"{ticker} headline", "Reuters", "https://x")
        ]


class FakeMessages:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=json.dumps(GOOD))],
            usage=SimpleNamespace(input_tokens=500, output_tokens=50),
            stop_reason="end_turn",
        )


@pytest.fixture
def seeded(db_factory: sessionmaker[Session]) -> dict[str, int]:
    ids: dict[str, int] = {}
    with db_factory() as s:
        for t, (qid, _) in BOOK.items():
            ids[t] = add_symbol(s, t, questrade_id=qid)
            s.add(
                m.UniverseSnapshot(
                    session_date=DAY,
                    symbol_id=ids[t],
                    price=Decimal("20"),
                    avg_volume=2_000_000,
                    atr14=Decimal("1"),
                    source="finviz",
                )
            )
            s.add(
                m.DailyCandle(
                    symbol_id=ids[t],
                    date=PREV,
                    open=Decimal("20"),
                    high=Decimal("20.5"),
                    low=Decimal("19.5"),
                    close=Decimal("20.00"),
                    volume=1,
                    vwap=None,
                )
            )
        s.commit()
    return ids


def deps(
    factory: sessionmaker[Session],
    finviz: Any,
    messages: FakeMessages,
    *,
    qt: FakeQuestrade | None = None,
    lasts: dict[str, str] | None = None,
    quote_at: dict[str, datetime] | None = None,
    **settings: Any,
) -> PremarketDeps:
    """`lasts` and `quote_at` override a ticker's pre-market last and last-trade time."""
    s = RuntimeSettings(**settings)
    fq = qt if qt is not None else FakeQuestrade()
    for t, (qid, last) in BOOK.items():
        fq.add_symbol(t, qid)
        price = (lasts or {}).get(t, last)
        fq.set_quote(qid, price, price, price, (quote_at or {}).get(t, CLOCK.now()))
    client = SimpleNamespace(messages=messages)
    service = CatalystService(
        factory, CLOCK, CatalystStore(factory, CLOCK), CatalystClassifier(client, lambda: s), finviz
    )
    return PremarketDeps(factory, CLOCK, finviz, MarketDataService(factory, CLOCK, CAL, fq), service, s)


async def test_candidates_come_from_screens_and_gaps(
    db_factory: sessionmaker[Session], seeded: dict[str, int]
) -> None:
    finviz, msgs = FakeFinviz(), FakeMessages()
    out = await run_premarket(deps(db_factory, finviz, msgs), DAY)
    assert (
        out["candidates"] == 4
        and out["classified"] == 4
        and out["over_cap"] == []
        and out["screen_errors"] == []
    )
    # ranked by |gap|: AAA +5% and BBB -5% (tie -> ticker), then CCC +1.5%, DDD +0.5%; EEE isn't flagged
    assert finviz.news_calls == ["AAA", "BBB", "CCC", "DDD"]
    assert finviz.screens == [
        "ind_stocksonly,sh_price_5to50,sh_avgvol_o1000,ta_averagetruerange_o0.5,geo_usa,news_date_today",
        "ind_stocksonly,sh_price_5to50,sh_avgvol_o1000,ta_averagetruerange_o0.5,geo_usa,"
        "earningsdate_prevdays5",
        "ind_stocksonly,sh_price_5to50,sh_avgvol_o1000,ta_averagetruerange_o0.5,geo_usa,earningsdate_today",
    ]
    lines = out["brief"].splitlines()
    assert lines[0] == "Pre-market brief for 2026-10-06: 4 candidates"
    assert lines[1].startswith("AAA +5.00% [gap, news] earnings_beat, bullish, quality 82")
    assert lines[2].startswith("BBB -5.00% [gap]")
    assert lines[4].startswith("DDD +0.50% [earnings]")
    with db_factory() as s:
        rows = {r.symbol_id: r for r in s.execute(select(m.Catalyst)).scalars()}
    assert rows[seeded["DDD"]].earnings_date == DAY and rows[seeded["AAA"]].earnings_date is None
    assert (
        rows[seeded["AAA"]].gap_pct == Decimal("0.0500")
        and rows[seeded["AAA"]].headlines[0]["title"] == "AAA headline"
    )
    assert seeded["SPY"] not in rows and seeded["EEE"] not in rows


async def test_only_the_top_n_are_classified(
    db_factory: sessionmaker[Session], seeded: dict[str, int]
) -> None:
    msgs = FakeMessages()
    out = await run_premarket(deps(db_factory, FakeFinviz(), msgs, claude_premarket_max_candidates=2), DAY)
    assert out["classified"] == 2 and out["over_cap"] == ["CCC", "DDD"] and len(msgs.calls) == 2
    assert "Not classified (over cap): CCC, DDD" in out["brief"]
    with db_factory() as s:
        ccc = s.execute(select(m.Catalyst).where(m.Catalyst.symbol_id == seeded["CCC"])).scalar_one()
    assert ccc.catalyst_type == "unknown" and ccc.reason == OVER_CAP


async def test_rerunning_the_scan_makes_no_new_claude_calls(
    db_factory: sessionmaker[Session], seeded: dict[str, int]
) -> None:
    msgs = FakeMessages()
    d = deps(db_factory, FakeFinviz(), msgs)
    await run_premarket(d, DAY)
    await run_premarket(d, DAY)
    assert len(msgs.calls) == 4
    with db_factory() as s:
        assert s.execute(select(func.count()).select_from(m.Catalyst)).scalar_one() == 4


async def test_a_finviz_failure_falls_back_to_gaps(
    db_factory: sessionmaker[Session], seeded: dict[str, int]
) -> None:
    out = await run_premarket(deps(db_factory, FakeFinviz(fail=True), FakeMessages()), DAY)
    # AAA and BBB by gap alone; one error per screen (news, and each of the two earnings screens)
    assert out["candidates"] == 2 and len(out["screen_errors"]) == 3
    assert "FinViz screens failed: news: HTTP 403" in out["brief"]
    assert "earnings [before open 2026-10-06]: HTTP 403" in out["brief"]
    assert "earnings [after close 2026-10-05]: HTTP 403" in out["brief"]


async def test_no_universe_is_an_error(db_factory: sessionmaker[Session]) -> None:
    with pytest.raises(RuntimeError, match="nightly"):
        await run_premarket(deps(db_factory, FakeFinviz(), FakeMessages()), DAY)


async def test_the_cost_of_every_call_is_stored(
    db_factory: sessionmaker[Session], seeded: dict[str, int]
) -> None:
    await run_premarket(deps(db_factory, FakeFinviz(), FakeMessages()), DAY)
    store = CatalystStore(db_factory, CLOCK)
    assert store.spent(DAY) == Decimal("0.006000")  # 4 x (500 x $2/M + 50 x $10/M)
    assert store.spent(DAY + timedelta(days=1)) == 0


# --- P2-T14 fix round (attempt 2) ---


class ListFinviz(FakeFinviz):
    """Screens return the given tickers (news / earnings); optionally reports a universe-filter count."""

    def __init__(self, news: list[str], earnings: list[str] | None = None) -> None:
        super().__init__()
        self.news_rows, self.earnings_rows = news, earnings or []

    def screen(
        self, filters: str, view: int = 111, signal: str | None = None, *, columns: str | None = None
    ) -> ScreenerPage:
        self.screens.append(filters)
        got = self.news_rows if "news_date_today" in filters else self.earnings_rows
        return page_of(filters, got, columns)


class CountingFinviz(ListFinviz):
    def __init__(self, news: list[str], universe_count: int) -> None:
        super().__init__(news)
        self.universe_count = universe_count
        self.counts: list[str] = []

    def count(self, filters: str, view: int = 111) -> int:
        self.counts.append(filters)
        return self.universe_count


class FailingPriorCloses(MarketDataService):
    async def prior_closes(self, symbol_ids: Any, session_date: date) -> dict[int, Decimal]:
        raise RuntimeError("database\nunavailable")


async def test_ignored_news_filter_is_a_failed_screen_by_universe_size(
    db_factory: sessionmaker[Session], seeded: dict[str, int]
) -> None:
    finviz = ListFinviz(news=["AAA", "BBB", "CCC", "DDD", "EEE"], earnings=["DDD"])  # all 5 names
    out = await run_premarket(deps(db_factory, finviz, FakeMessages()), DAY)
    assert out["screen_errors"] == [
        "news: filter 'news_date_today' ignored (matched all 5 universe-filter names)"
    ]
    assert out["candidates"] == 3  # AAA, BBB by gap + DDD from earnings; the news rows are dropped
    assert "FinViz screens failed: news: filter 'news_date_today' ignored" in out["brief"]


async def test_ignored_filter_uses_the_screeners_universe_count(
    db_factory: sessionmaker[Session], seeded: dict[str, int]
) -> None:
    finviz = CountingFinviz(news=["AAA", "CCC", "ZZZ"], universe_count=3)
    out = await run_premarket(deps(db_factory, finviz, FakeMessages()), DAY)
    assert out["screen_errors"] == [
        "news: filter 'news_date_today' ignored (matched all 3 universe-filter names)"
    ]
    # the universe-filter count, then the market-wide cross-checks of the two empty earnings screens
    assert finviz.counts == [
        "ind_stocksonly,sh_price_5to50,sh_avgvol_o1000,ta_averagetruerange_o0.5,geo_usa",
        "earningsdate_prevdays5",
        "earningsdate_today",
    ]
    ok = CountingFinviz(news=["AAA", "CCC", "ZZZ"], universe_count=700)
    assert (await run_premarket(deps(db_factory, ok, FakeMessages()), DAY))["screen_errors"] == []


async def test_failed_prior_closes_leave_gaps_unknown_but_keep_screens(
    db_factory: sessionmaker[Session], seeded: dict[str, int]
) -> None:
    d = deps(db_factory, FakeFinviz(), FakeMessages())
    data = FailingPriorCloses(db_factory, CLOCK, CAL, FakeQuestrade())
    out = await run_premarket(dataclasses.replace(d, data=data), DAY)
    assert out["candidates"] == 3  # AAA, CCC (news), DDD (earnings); FFF isn't in the universe
    lines = out["brief"].splitlines()
    assert all(" gap n/a " in line for line in lines[1:4])
    assert lines[-1] == "Questrade quotes failed: RuntimeError: database unavailable"
    assert out["quote_error"] == "Questrade quotes failed: RuntimeError: database unavailable"


def test_brief_collapses_and_caps_every_interpolated_text() -> None:
    c = PremarketCandidate(1, "AAA", "AAA Inc", None, ("news",))
    cat = StoredCatalyst(1, "unknown", "neutral", None, None, "err:\n" + "x" * 500, None, Decimal(0), False)
    brief = format_brief(DAY, [c], {1: cat}, [], ["news: HTTP 403\nForged line", "e" * 400])
    lines = brief.splitlines()
    assert len(lines) == 3
    assert lines[1].startswith("AAA gap n/a [news] unknown (err: xxx") and len(lines[1]) < 260
    assert lines[2].startswith("FinViz screens failed: news: HTTP 403 Forged line; eee")
    assert len(lines[2]) < 260
    assert brief_notes(["a\nb", "  ", "c" * 1000]) == ["a b", "c" * 399 + "…"]


# --- P2-REVIEW: earnings window is "after the previous session's close OR before today's open" (Stephen,
# 2026-09-27); since the 2026-09-27 fix, two session-window screens (prevdays5 + today, Earnings column) ---


class ByFilterFinviz(FakeFinviz):
    """Each screen returns the tickers mapped to its last filter token; a mapped exception is raised."""

    def __init__(self, by_token: dict[str, list[str] | Exception]) -> None:
        super().__init__()
        self.by_token = by_token

    def screen(
        self, filters: str, view: int = 111, signal: str | None = None, *, columns: str | None = None
    ) -> ScreenerPage:
        self.screens.append(filters)
        got = self.by_token.get(filters.split(",")[-1], [])
        if isinstance(got, Exception):
            raise got
        return page_of(filters, got, columns)


def test_news_filter_may_hold_alternatives() -> None:
    assert RuntimeSettings.model_validate({"premarket.news_filter": "news_date_today|news_date_prevdays2"})


async def test_both_earnings_screens_are_unioned_with_their_report_dates(
    db_factory: sessionmaker[Session], seeded: dict[str, int]
) -> None:
    finviz = ByFilterFinviz(
        {
            "earningsdate_prevdays5": ["DDD", "EEE"],
            "earningsdate_today": ["EEE", "CCC"],
        }
    )
    out = await run_premarket(deps(db_factory, finviz, FakeMessages()), DAY)
    assert out["screen_errors"] == []
    assert out["candidates"] == 5  # AAA, BBB by gap; CCC, DDD, EEE from the two earnings screens
    with db_factory() as s:
        rows = {r.symbol_id: r for r in s.execute(select(m.Catalyst)).scalars()}
    # after yesterday's close -> the previous session; before today's open (or both) -> today
    assert rows[seeded["DDD"]].earnings_date == PREV
    assert rows[seeded["EEE"]].earnings_date == DAY and rows[seeded["CCC"]].earnings_date == DAY
    assert rows[seeded["AAA"]].earnings_date is None


async def test_one_failed_earnings_screen_keeps_the_other(
    db_factory: sessionmaker[Session], seeded: dict[str, int]
) -> None:
    finviz = ByFilterFinviz(
        {"earningsdate_prevdays5": FinvizBlocked("HTTP 403"), "earningsdate_today": ["DDD"]}
    )
    out = await run_premarket(deps(db_factory, finviz, FakeMessages()), DAY)
    assert out["screen_errors"] == ["earnings [after close 2026-10-05]: HTTP 403"]
    assert any(line.startswith("DDD +0.50% [earnings]") for line in out["brief"].splitlines())


async def test_gap_threshold_uses_the_unrounded_gap(
    db_factory: sessionmaker[Session], seeded: dict[str, int]
) -> None:
    """+2.996% rounds to 0.0300 for display, but is below the 3% threshold."""
    finviz = ListFinviz(news=[])
    out = await run_premarket(deps(db_factory, finviz, FakeMessages(), lasts={"CCC": "20.5992"}), DAY)
    assert out["candidates"] == 2  # AAA and BBB only
    assert not any(line.startswith("CCC") for line in out["brief"].splitlines())


async def test_a_quote_older_than_the_prior_close_is_stale(
    db_factory: sessionmaker[Session], seeded: dict[str, int]
) -> None:
    before_close = datetime(2026, 10, 5, 19, 59, tzinfo=UTC)  # 15:59 ET on the prior session
    finviz = ListFinviz(news=["AAA"])
    out = await run_premarket(deps(db_factory, finviz, FakeMessages(), quote_at={"AAA": before_close}), DAY)
    lines = out["brief"].splitlines()
    assert lines[1].startswith("BBB -5.00% [gap]")
    assert any(line.startswith("AAA gap n/a [news]") for line in lines)


async def test_budget_hit_adds_a_summary_line(
    db_factory: sessionmaker[Session], seeded: dict[str, int]
) -> None:
    d = deps(db_factory, FakeFinviz(), FakeMessages(), claude_daily_budget_usd=Decimal("0.003"))
    out = await run_premarket(d, DAY)
    assert out["budget_hit"] == ["CCC", "DDD"]
    assert out["brief"].splitlines()[-1] == "Claude daily budget reached: 2 not classified (CCC, DDD)"


async def test_warnings_are_logged_and_in_the_brief(
    db_factory: sessionmaker[Session], seeded: dict[str, int]
) -> None:
    warn = "WARNING: forced run outside the pre-market window"
    out = await run_premarket(deps(db_factory, FakeFinviz(), FakeMessages()), DAY, warnings=[warn])
    assert out["brief"].splitlines()[-1] == warn and out["warnings"] == [warn]
    with db_factory() as s:
        ev = s.execute(select(m.EventLog).where(m.EventLog.source == "job.premarket")).scalar_one()
    assert ev.level == "warning" and ev.data["warnings"] == [warn]


def test_premarket_deps_are_frozen(db_factory: sessionmaker[Session]) -> None:
    d = deps(db_factory, FakeFinviz(), FakeMessages())
    with pytest.raises(dataclasses.FrozenInstanceError):
        d.settings = RuntimeSettings()  # type: ignore[misc]


# --- FIX (2026-09-27): the earnings window is the previous TRADING session after the close + today before
# the open. FinViz's "yesterday" is the previous calendar day (on Sunday 2026-09-27 earningsdate_yesterday
# and earningsdate_yesterdayafter matched 0 names market-wide while Friday had reporters), so on a Monday
# or after a holiday earningsdate_yesterdayafter silently missed the previous session's reporters. ---

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "finviz"
UNIVERSE_FILTERS = "ind_stocksonly,sh_price_5to50,sh_avgvol_o1000,ta_averagetruerange_o0.5,geo_usa"
PREV5, TODAY = "earningsdate_prevdays5", "earningsdate_today"


def live_rows(*names: str) -> list[dict[str, str]]:
    return [r for n in names for r in parse_screener((FIX / n).read_text(encoding="utf-8")).rows]


def earnings_page(rows: Sequence[dict[str, str] | tuple[str, str]]) -> ScreenerPage:
    """A parsed v=152 page: real parsed rows, or (ticker, Earnings) pairs."""
    recs = [
        r if isinstance(r, dict) else {"No.": "1", "Ticker": r[0], "Company": "", "Earnings": r[1]}
        for r in rows
    ]
    return ScreenerPage(len(recs), ["No.", "Ticker", "Company", EARNINGS_COLUMN], recs)


EMPTY = ScreenerPage(0, [], [], has_table=False)  # FinViz's verified "0 Total" page
# Names that no screen matches: they keep a screen's count below the universe size, which would otherwise
# read as "FinViz ignored the filter".
FILLER = [f"ZZ{c}" for c in "ABCDEFGHIJ"]


class WindowFinviz(FakeFinviz):
    """Screens keyed by their last filter token (news -> `news`); records (token, view, columns)."""

    def __init__(
        self,
        pages: dict[str, ScreenerPage | Exception],
        news: list[str] | None = None,
        market_counts: dict[str, int] | None = None,
    ) -> None:
        super().__init__()
        self.pages, self.news_rows = pages, news or []
        self.calls: list[tuple[str, int, str | None]] = []
        self.market_counts = market_counts
        self.counts: list[str] = []

    def screen(
        self, filters: str, view: int = 111, signal: str | None = None, *, columns: str | None = None
    ) -> ScreenerPage:
        self.screens.append(filters)
        token = filters.split(",")[-1]
        self.calls.append((token, view, columns))
        if token == "news_date_today":
            return ScreenerPage(len(self.news_rows), ["Ticker"], [{"Ticker": t} for t in self.news_rows])
        got = self.pages.get(token, EMPTY)
        if isinstance(got, Exception):
            raise got
        return got


class CountingWindowFinviz(WindowFinviz):
    def count(self, filters: str, view: int = 111) -> int:
        self.counts.append(filters)
        if filters == UNIVERSE_FILTERS:
            return 700
        assert self.market_counts is not None
        return self.market_counts[filters]


def seed_session(factory: sessionmaker[Session], session: date, tickers: list[str]) -> dict[str, int]:
    """A universe for `session` (plus SPY) with a 20.00 close on the previous session."""
    prev = CAL.previous_session(session)
    ids: dict[str, int] = {}
    with factory() as s:
        for i, t in enumerate([*tickers, *FILLER, "SPY"]):
            ids[t] = add_symbol(s, t, questrade_id=1000 + i)
            s.add(
                m.UniverseSnapshot(
                    session_date=session,
                    symbol_id=ids[t],
                    price=Decimal("20"),
                    avg_volume=2_000_000,
                    atr14=Decimal("1"),
                    source="finviz",
                )
            )
            s.add(
                m.DailyCandle(
                    symbol_id=ids[t],
                    date=prev,
                    open=Decimal("20"),
                    high=Decimal("20"),
                    low=Decimal("20"),
                    close=Decimal("20.00"),
                    volume=1,
                    vwap=None,
                )
            )
        s.commit()
    return ids


def window_deps(
    factory: sessionmaker[Session],
    finviz: Any,
    tickers: list[str],
    calendar: SessionCalendar = CAL,
    **settings: Any,
) -> PremarketDeps:
    """No gaps (every last equals the 20.00 close): only the screens flag names."""
    s = RuntimeSettings(**settings)
    fq = FakeQuestrade()
    for i, t in enumerate([*tickers, *FILLER, "SPY"]):
        fq.add_symbol(t, 1000 + i)
        fq.set_quote(1000 + i, "20.00", "20.00", "20.00", CLOCK.now())
    service = CatalystService(
        factory,
        CLOCK,
        CatalystStore(factory, CLOCK),
        CatalystClassifier(SimpleNamespace(messages=FakeMessages()), lambda: s),
        finviz,
    )
    return PremarketDeps(
        factory, CLOCK, finviz, MarketDataService(factory, CLOCK, calendar, fq), service, s, calendar
    )


def earnings_by_ticker(factory: sessionmaker[Session], ids: dict[str, int]) -> dict[str, date | None]:
    with factory() as s:
        rows = list(s.execute(select(m.Catalyst)).scalars())
    names = {v: k for k, v in ids.items()}
    return {names[r.symbol_id]: r.earnings_date for r in rows}


def test_default_earnings_setting_is_the_session_window_and_the_old_default_maps_to_it() -> None:
    assert RuntimeSettings().premarket_earnings_filter == EARNINGS_SESSION_WINDOW == "session_window"
    old = RuntimeSettings.model_validate({"premarket.earnings_filter": LEGACY_EARNINGS_FILTER})
    assert old.premarket_earnings_filter == LEGACY_EARNINGS_FILTER  # still a valid stored value
    for bad in ("a||b", "|a", "a|", "a|B"):
        with pytest.raises(ValueError):
            RuntimeSettings.model_validate({"premarket.earnings_filter": bad})
    with pytest.raises(ValueError):  # the universe filters stay one list: no alternatives there
        RuntimeSettings.model_validate({"universe.finviz_filters": "geo_usa|sh_price_5to50"})


async def test_monday_takes_fridays_after_close_reporters(db_factory: sessionmaker[Session]) -> None:
    monday, friday = date(2026, 9, 28), date(2026, 9, 25)
    tickers = ["FRA", "FRB", "SAT", "SUN", "MOB", "MOA", "OLD", "COST", "TBN"]
    ids = seed_session(db_factory, monday, tickers)
    # Real rows from Sunday's prevdays5 page (COST Sep 24/a, TBN Sep 25/b: both outside the window) plus
    # the Friday/weekend cases the live week didn't have.
    real = [r for r in live_rows("raw_screener_earnings_prevdays5.html") if r["Ticker"] in ("COST", "TBN")]
    prev5 = earnings_page(
        [
            *real,
            ("FRA", "Sep 25/a"),
            ("FRB", "Sep 25/b"),
            ("SAT", "Sep 26/b"),
            ("SUN", "Sep 27/a"),
            ("OLD", "Sep 24/a"),
        ]
    )
    today = earnings_page([("MOB", "Sep 28/b"), ("MOA", "Sep 28/a")])
    finviz = WindowFinviz({PREV5: prev5, TODAY: today})
    out = await run_premarket(window_deps(db_factory, finviz, tickers), monday)
    assert out["screen_errors"] == []
    got = earnings_by_ticker(db_factory, ids)
    # Friday after the close, anything over the weekend, and Monday before the open
    assert got == {"FRA": friday, "SAT": date(2026, 9, 26), "SUN": date(2026, 9, 27), "MOB": monday}
    assert ("earningsdate_yesterdayafter", 111, None) not in finviz.calls
    assert finviz.calls[1:] == [
        (PREV5, EARNINGS_VIEW, EARNINGS_COLUMNS),
        (TODAY, EARNINGS_VIEW, EARNINGS_COLUMNS),
    ]
    assert [s["label"] for s in out["screens"]] == [
        "news",
        "earnings [after close 2026-09-25]",
        "earnings [before open 2026-09-28]",
    ]
    assert out["screens"][1] == {
        "label": "earnings [after close 2026-09-25]",
        "filter": PREV5,
        "rows": 7,
        "matched": ["FRA", "SAT", "SUN"],
    }


async def test_day_after_a_holiday_takes_the_session_before_the_holiday(
    db_factory: sessionmaker[Session],
) -> None:
    tuesday = date(2026, 9, 8)  # Monday 2026-09-07 is Labor Day
    tickers = ["FRI", "HOL", "FRB", "TUB"]
    ids = seed_session(db_factory, tuesday, tickers)
    prev5 = earnings_page([("FRI", "Sep 04/a"), ("HOL", "Sep 07/a"), ("FRB", "Sep 04/b")])
    finviz = WindowFinviz({PREV5: prev5, TODAY: earnings_page([("TUB", "Sep 08/b")])})
    out = await run_premarket(window_deps(db_factory, finviz, tickers), tuesday)
    assert out["screen_errors"] == []
    assert earnings_by_ticker(db_factory, ids) == {
        "FRI": date(2026, 9, 4),
        "HOL": date(2026, 9, 7),
        "TUB": tuesday,
    }
    assert out["screens"][1]["label"] == "earnings [after close 2026-09-04]"


async def test_normal_tuesday_splits_after_close_and_before_open_on_live_rows(
    db_factory: sessionmaker[Session],
) -> None:
    """The live week of 2026-09-21: Monday's /a and Tuesday's /b reporters count; Monday's /b and
    Tuesday's /a don't."""
    tuesday = date(2026, 9, 22)
    week = live_rows("raw_screener_earnings_thisweek_p1.html", "raw_screener_earnings_thisweek_p2.html")
    tickers = sorted(r["Ticker"] for r in week)
    ids = seed_session(db_factory, tuesday, tickers)
    prev5 = earnings_page([r for r in week if r["Earnings"].split("/")[0] in ("Sep 21", "Sep 22")])
    today = earnings_page([r for r in week if r["Earnings"].startswith("Sep 22/")])
    finviz = WindowFinviz({PREV5: prev5, TODAY: today})
    out = await run_premarket(window_deps(db_factory, finviz, tickers), tuesday)
    assert out["screen_errors"] == []
    got = earnings_by_ticker(db_factory, ids)
    assert got == {
        "ABVX": date(2026, 9, 21),
        "ANAB": date(2026, 9, 21),
        "AZO": tuesday,
        "HERE": tuesday,
        "MLKN": tuesday,
        "THO": tuesday,
    }
    assert "EBF" not in got and "KBH" not in got  # Monday before the open, Tuesday after the close


async def test_empty_earnings_pages_are_an_empty_result(db_factory: sessionmaker[Session]) -> None:
    monday = date(2026, 9, 28)
    seed_session(db_factory, monday, ["AAA"])
    finviz = WindowFinviz({PREV5: EMPTY, TODAY: EMPTY})
    out = await run_premarket(window_deps(db_factory, finviz, ["AAA"]), monday)
    assert out["screen_errors"] == [] and out["candidates"] == 0
    assert [s["rows"] for s in out["screens"]] == [0, 0, 0]


async def test_an_empty_catalyst_screen_is_cross_checked_market_wide(
    db_factory: sessionmaker[Session],
) -> None:
    monday = date(2026, 9, 28)
    seed_session(db_factory, monday, ["AAA"])
    counts = {"news_date_today": 250, PREV5: 0, TODAY: 12}
    finviz = CountingWindowFinviz({PREV5: EMPTY, TODAY: EMPTY}, market_counts=counts)
    out = await run_premarket(window_deps(db_factory, finviz, ["AAA"]), monday)
    assert finviz.counts == ["news_date_today", PREV5, TODAY]
    assert out["screen_errors"] == [
        "earnings [after close 2026-09-25]: cross-check: FinViz lists no 'earningsdate_prevdays5' "
        "matches market-wide either, so the filter may be broken"
    ]
    assert "FinViz screens failed: earnings [after close 2026-09-25]: cross-check" in out["brief"]


async def test_an_unreadable_earnings_value_is_counted_not_dropped(db_factory: sessionmaker[Session]) -> None:
    monday = date(2026, 9, 28)
    ids = seed_session(db_factory, monday, ["AAA", "BBB", "CCC"])
    prev5 = earnings_page([("AAA", "Sep 25"), ("BBB", "Sep 25/a"), ("CCC", "-"), ("ZZZ", "soon")])
    finviz = WindowFinviz({PREV5: prev5, TODAY: EMPTY})
    out = await run_premarket(window_deps(db_factory, finviz, ["AAA", "BBB", "CCC"]), monday)
    assert out["screen_errors"] == [
        "earnings [after close 2026-09-25]: 3 rows with an unreadable Earnings value "
        "(AAA 'Sep 25', CCC '-', ZZZ 'soon')"
    ]
    assert earnings_by_ticker(db_factory, ids) == {"BBB": date(2026, 9, 25)}


async def test_a_page_without_the_earnings_column_is_a_failed_screen(
    db_factory: sessionmaker[Session],
) -> None:
    monday = date(2026, 9, 28)
    seed_session(db_factory, monday, ["AAA"])
    no_col = ScreenerPage(1, ["No.", "Ticker"], [{"No.": "1", "Ticker": "AAA"}])
    finviz = WindowFinviz({PREV5: no_col, TODAY: FinvizBlocked("HTTP 403")})
    out = await run_premarket(window_deps(db_factory, finviz, ["AAA"]), monday)
    assert out["screen_errors"] == [
        "earnings [after close 2026-09-25]: FinViz page has no 'Earnings' column (header ['No.', 'Ticker'])",
        "earnings [before open 2026-09-28]: HTTP 403",
    ]
    assert out["candidates"] == 0


async def test_rows_dated_outside_the_filters_range_are_reported(db_factory: sessionmaker[Session]) -> None:
    """prevdays5 reaches back 4 calendar days (live 2026-09-27: Sep 23..27); today's screen is today."""
    monday = date(2026, 9, 28)
    ids = seed_session(db_factory, monday, ["AAA", "BBB"])
    finviz = WindowFinviz(
        {
            PREV5: earnings_page([("AAA", "Sep 10/a"), ("BBB", "Sep 25/a")]),
            TODAY: earnings_page([("CCC", "Sep 25/a")]),
        }
    )
    out = await run_premarket(window_deps(db_factory, finviz, ["AAA", "BBB"]), monday)
    assert out["screen_errors"] == [
        "earnings [after close 2026-09-25]: 1 rows dated outside 2026-09-24..2026-09-28 (AAA 'Sep 10/a'): "
        "FinViz's filter may have changed meaning",
        "earnings [before open 2026-09-28]: 1 rows dated outside 2026-09-28..2026-09-28 (CCC 'Sep 25/a'): "
        "FinViz's filter may have changed meaning",
    ]
    assert earnings_by_ticker(db_factory, ids) == {"BBB": date(2026, 9, 25)}


class GapCalendar(SessionCalendar):
    """Pretends the previous session is 5 calendar days back, beyond FinViz's prevdays5 reach."""

    def previous_session(self, d: date) -> date:
        return d - timedelta(days=5) if d == date(2026, 9, 28) else super().previous_session(d)


async def test_a_previous_session_beyond_prevdays5_is_reported(db_factory: sessionmaker[Session]) -> None:
    monday = date(2026, 9, 28)
    seed_session(db_factory, monday, ["AAA"])
    finviz = WindowFinviz({PREV5: earnings_page([("AAA", "Sep 24/a")])})
    out = await run_premarket(window_deps(db_factory, finviz, ["AAA"], calendar=GapCalendar()), monday)
    assert out["screen_errors"][0] == (
        "earnings [after close 2026-09-23]: the previous session is 5 days back, beyond "
        "'earningsdate_prevdays5' (4 days): its after-close reporters can't be screened"
    )


async def test_the_old_default_setting_runs_the_session_window(db_factory: sessionmaker[Session]) -> None:
    monday = date(2026, 9, 28)
    ids = seed_session(db_factory, monday, ["FRA"])
    finviz = WindowFinviz({PREV5: earnings_page([("FRA", "Sep 25/a")])})
    d = window_deps(db_factory, finviz, ["FRA"], premarket_earnings_filter=LEGACY_EARNINGS_FILTER)
    out = await run_premarket(d, monday)
    assert [c[0] for c in finviz.calls] == ["news_date_today", PREV5, TODAY]
    assert out["screen_errors"] == [] and earnings_by_ticker(db_factory, ids) == {"FRA": date(2026, 9, 25)}


async def test_explicit_filter_lists_still_run_as_plain_screens(db_factory: sessionmaker[Session]) -> None:
    monday = date(2026, 9, 28)
    ids = seed_session(db_factory, monday, ["AAA"])
    plain = ScreenerPage(1, ["Ticker"], [{"Ticker": "AAA"}])
    finviz = WindowFinviz({"earningsdate_thisweek": plain})
    d = window_deps(db_factory, finviz, ["AAA"], premarket_earnings_filter="earningsdate_thisweek")
    out = await run_premarket(d, monday)
    assert finviz.calls == [("news_date_today", 111, None), ("earningsdate_thisweek", 111, None)]
    assert [s["label"] for s in out["screens"]] == ["news", "earnings"]
    assert earnings_by_ticker(db_factory, ids) == {"AAA": monday}
