"""QUOTEBAR: the decision log explains the 9:35 scan with the bars the scan used. A quote-built bar
(`opening_bar_quotes`) wins over a candle stored later for the same symbol, and a new quote bar changes the
day's fingerprint (so the day is rebuilt)."""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session, sessionmaker

from tests.factories import add_run, add_symbol
from trader.db import models as m
from trader.decisions.recorder import LiveScanData, fingerprint
from trader.market import repository as repo
from trader.market.calendar import SessionCalendar
from trader.market.types import Candle
from trader.settings_store import RuntimeSettings

pytestmark = pytest.mark.db
CAL = SessionCalendar()
DAY = date(2026, 10, 6)
OPEN = CAL.session_open(DAY)
END = OPEN + timedelta(minutes=5)


def official(v: int) -> Candle:
    return Candle(OPEN, END, Decimal("21"), Decimal("21.5"), Decimal("20.9"), Decimal("21.4"), v, None)


def quote_row(sid: int) -> m.OpeningBarQuote:
    return m.OpeningBarQuote(
        session_date=DAY,
        symbol_id=sid,
        captured_at=END + timedelta(seconds=5),
        quote_time=None,
        open=Decimal("20"),
        high=Decimal("20.6"),
        low=Decimal("19.9"),
        close=Decimal("20.5"),
        quote_volume=1_400,
        volume=1_000,
        vol_factor=Decimal("0.714300"),
        factor_source="default",
    )


async def test_quote_bars_win_over_candles_and_candles_fill_the_rest(
    db_factory: sessionmaker[Session],
) -> None:
    with db_factory() as s:
        a, b, c = (add_symbol(s, t) for t in ("AAA", "BBB", "CCC"))
        s.add(quote_row(a))
        repo.upsert_intraday_candles(s, a, "5m", [official(5_000)])
        repo.upsert_intraday_candles(s, b, "5m", [official(6_000)])
        s.commit()
    bars = await LiveScanData(db_factory, CAL).stored_opening_bars(DAY, [a, b, c])
    assert set(bars) == {a, b}
    assert bars[a] == Candle(
        OPEN, END, Decimal("20.0000"), Decimal("20.6000"), Decimal("19.9000"), Decimal("20.5000"), 1_000, None
    )
    assert bars[b].volume == 6_000


def test_a_quote_bar_changes_the_fingerprint(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        run_id = add_run(s)
        a = add_symbol(s, "AAA")
        s.commit()
        before = fingerprint(s, CAL, run_id, DAY, "all", RuntimeSettings())
        s.add(quote_row(a))
        s.commit()
        after = fingerprint(s, CAL, run_id, DAY, "all", RuntimeSettings())
    assert before != after
