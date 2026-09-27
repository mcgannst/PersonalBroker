"""P4-T10 acceptance test 5: the nightly job uses an uploaded watchlist as the session's universe (source
`manual`) instead of FinViz (SPEC §4.2 manual fallback, resolved decision 5: it replaces FinViz, no merge;
deleting it goes back to FinViz). The fakes are the P1 nightly tests' own."""

from datetime import UTC, date, datetime

import pytest
from sqlalchemy.orm import Session, sessionmaker

from tests.jobs.test_nightly import CLOCK, TARGET, FakeFinviz, FakeMarket, StableMarket, deps, snapshot
from trader.adapters.finviz.scraper import FinvizBlocked
from trader.jobs.nightly import NightlyDegenerate, run_nightly
from trader.market.clock import FixedClock
from trader.market.watchlist import delete_watchlist, store_watchlist

pytestmark = pytest.mark.db
UPLOADED = FixedClock(datetime(2026, 9, 27, 18, 0, tzinfo=UTC))


def _upload(factory: sessionmaker[Session], day: date, tickers: list[str]) -> None:
    store_watchlist(factory, UPLOADED, day, tickers, "list.csv", "web:stephen")


async def test_manual_watchlist_replaces_finviz(db_factory: sessionmaker[Session]) -> None:
    _upload(db_factory, TARGET, ["AAPL", "BF.B"])
    finviz = FakeFinviz(["NVDA", "AMD"])
    detail = await run_nightly(deps(db_factory, finviz), TARGET)
    assert finviz.calls == 0
    assert detail["source"] == "manual"
    assert detail["universe"] == 3
    snap = snapshot(db_factory, TARGET)
    assert set(snap) == {"AAPL", "BF.B", "SPY"}  # exactly the list plus SPY: no FinViz names merged in
    assert {src for src, _ in snap.values()} == {"manual"}
    assert all(price is not None for _, price in snap.values())  # the last daily close


async def test_without_a_watchlist_finviz_is_used(db_factory: sessionmaker[Session]) -> None:
    _upload(db_factory, date(2026, 9, 29), ["AAPL"])  # another session's list does not apply
    finviz = FakeFinviz(["NVDA"])
    detail = await run_nightly(deps(db_factory, finviz), TARGET)
    assert finviz.calls == 1
    assert detail["source"] == "finviz"
    assert set(snapshot(db_factory, TARGET)) == {"NVDA", "SPY"}


async def test_manual_list_works_while_finviz_is_blocked(db_factory: sessionmaker[Session]) -> None:
    """The point of the fallback: FinViz is down and there is no stored universe to fall back on."""
    _upload(db_factory, TARGET, ["MSFT"])
    detail = await run_nightly(deps(db_factory, FakeFinviz(None, FinvizBlocked("HTTP 403"))), TARGET)
    assert detail["source"] == "manual"
    assert "fallback_from" not in detail


async def test_manual_list_still_obeys_the_degenerate_rules(db_factory: sessionmaker[Session]) -> None:
    # 19 names + SPY = 20 wanted; 2 unknown = 10% > 5%
    _upload(db_factory, TARGET, [f"T{i:03d}" for i in range(19)])
    finviz = FakeFinviz(["NVDA"])
    market = FakeMarket(unknown=frozenset({"T000", "T001"}))
    with pytest.raises(NightlyDegenerate, match="unresolved"):
        await run_nightly(deps(db_factory, finviz, market), TARGET)
    assert finviz.calls == 0
    assert snapshot(db_factory, TARGET) == {}


async def test_upload_then_delete_switches_a_rerun_between_manual_and_finviz(
    db_factory: sessionmaker[Session],
) -> None:
    market = StableMarket()
    await run_nightly(deps(db_factory, FakeFinviz(["NVDA"]), market), TARGET)
    assert {src for src, _ in snapshot(db_factory, TARGET).values()} == {"finviz"}

    _upload(db_factory, TARGET, ["AAPL"])  # a forced re-run after the upload replaces the FinViz day
    await run_nightly(deps(db_factory, FakeFinviz(["NVDA"]), market), TARGET)
    snap = snapshot(db_factory, TARGET)
    assert set(snap) == {"AAPL", "SPY"} and {src for src, _ in snap.values()} == {"manual"}

    assert delete_watchlist(db_factory, CLOCK, TARGET, "web:stephen")
    detail = await run_nightly(deps(db_factory, FakeFinviz(["NVDA"]), market), TARGET)
    assert detail["source"] == "finviz"
    assert set(snapshot(db_factory, TARGET)) == {"NVDA", "SPY"}
