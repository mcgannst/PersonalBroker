import dataclasses
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
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
from trader.adapters.finviz.parser import Headline, ScreenerPage
from trader.adapters.finviz.scraper import FinvizBlocked
from trader.db import models as m
from trader.jobs.premarket import PremarketCandidate, PremarketDeps, brief_notes, format_brief, run_premarket
from trader.market.calendar import SessionCalendar
from trader.market.clock import FixedClock
from trader.market.data_service import MarketDataService
from trader.settings_store import RuntimeSettings

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


class FakeFinviz:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.screens: list[str] = []
        self.news_calls: list[str] = []

    def screen(self, filters: str, view: int = 111, signal: str | None = None) -> ScreenerPage:
        self.screens.append(filters)
        if self.fail:
            raise FinvizBlocked("HTTP 403")
        tickers = ["AAA", "CCC", "FFF"] if "news_date_today" in filters else ["DDD"]
        return ScreenerPage(
            len(tickers), ["No.", "Ticker"], [{"No.": str(i), "Ticker": t} for i, t in enumerate(tickers)]
        )

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
    assert out["candidates"] == 2 and len(out["screen_errors"]) == 2  # AAA and BBB by gap alone
    assert "FinViz screens failed: news: HTTP 403" in out["brief"]


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

    def screen(self, filters: str, view: int = 111, signal: str | None = None) -> ScreenerPage:
        self.screens.append(filters)
        got = self.news_rows if "news_date_today" in filters else self.earnings_rows
        return ScreenerPage(len(got), ["Ticker"], [{"Ticker": t} for t in got])


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
    assert finviz.counts == ["ind_stocksonly,sh_price_5to50,sh_avgvol_o1000,ta_averagetruerange_o0.5,geo_usa"]
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
