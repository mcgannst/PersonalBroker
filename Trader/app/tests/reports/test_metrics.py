"""P5-T2 acceptance tests 1-6: `trader.reports.metrics` (hand calculations, the view, ranges, histogram)."""

from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from tests.factories import add_run, add_symbol
from tests.reports import add_trade
from trader.db import models as m
from trader.reports.metrics import (
    OPEN_HIGH,
    OPEN_LOW,
    R_HIGH,
    R_LOW,
    Metrics,
    TradeRow,
    compute_metrics,
    max_drawdown,
    metrics_from_rows,
    r_histogram,
)

D1, D2, D3, D4, D5 = (date(2026, 10, d) for d in (5, 6, 7, 8, 9))
Q4 = Decimal("0.0001")


def _row(pnl: str, r: str | None, qty: int = 10, slip: str = "0", fees: str = "0") -> TradeRow:
    return TradeRow(
        pnl=Decimal(pnl),
        pnl_r=None if r is None else Decimal(r),
        qty=qty,
        slippage_total=Decimal(slip),
        fees_total=Decimal(fees),
    )


def _metrics(
    trades: Sequence[TradeRow] = (), equity: Sequence[str] = (), answers: Sequence[bool | None] = ()
) -> Metrics:
    return metrics_from_rows(7, None, None, trades, [Decimal(e) for e in equity], answers)


# --- 1. hand calculation ------------------------------------------------------------------------------------


def test_hand_calculation_of_five_trades() -> None:
    mt = _metrics(
        [
            _row("20", "2", fees="1.0000"),
            _row("-10", "-1", fees="1.0000"),
            _row("5", "0.5", fees="0.5000"),
            _row("-10", "-1"),
            _row("-3", None, fees="0.2500"),
        ]
    )
    assert (mt.run_id, mt.date_from, mt.date_to) == (7, None, None)
    assert (mt.trades, mt.wins, mt.losses) == (5, 2, 3)
    assert mt.win_rate == Decimal("0.4000") and str(mt.win_rate) == "0.4000"
    assert mt.expectancy_r == Decimal("0.1250")  # over the four trades with an R
    assert mt.avg_win_r == Decimal("1.2500")
    assert mt.avg_loss_r == Decimal("-1.0000")  # the null-R loss is not averaged
    assert mt.profit_factor == Decimal("1.0870")  # 25 / 23
    assert mt.total_pnl == Decimal("2")
    assert mt.total_fees == Decimal("2.7500")
    assert mt.trades_without_r == 1
    assert sum(b.count for b in mt.r_histogram) == 4


def test_ratios_round_half_up_to_four_places() -> None:
    mt = _metrics([_row("1", "0.0001"), _row("-1", "0.0000")])
    assert mt.expectancy_r == Decimal("0.0001")  # 0.00005 rounds up
    neg = _metrics([_row("-1", "-0.0001"), _row("-1", "0.0000")])
    assert neg.expectancy_r == Decimal("-0.0001")  # away from zero, as Postgres round()
    third = _metrics([_row("1", "1"), _row("-1", "-1"), _row("-1", "-1")])
    assert third.win_rate == Decimal("0.3333") and third.expectancy_r == Decimal("-0.3333")


def test_profit_factor_edges() -> None:
    assert _metrics([_row("5", "0.5"), _row("0", "0")]).profit_factor is None  # no losing P&L
    only_losses = _metrics([_row("-5", "-0.5")])
    assert only_losses.profit_factor == Decimal("0.0000") and only_losses.wins == 0
    assert only_losses.avg_win_r is None and only_losses.avg_loss_r == Decimal("-0.5000")
    zero = _metrics([_row("0", "0")])
    assert (zero.wins, zero.losses) == (0, 1)  # pnl <= 0 is a loss, as the view


# --- 2. drawdown --------------------------------------------------------------------------------------------


def test_max_drawdown() -> None:
    eq = [Decimal(v) for v in ("100", "110", "99", "105", "88")]
    assert max_drawdown(eq) == Decimal("0.2000")  # (110 - 88) / 110
    assert max_drawdown([Decimal("100")]) == Decimal("0.0000")
    assert max_drawdown([]) is None
    assert max_drawdown([Decimal("100"), Decimal("120"), Decimal("130")]) == Decimal("0")
    assert _metrics(equity=("100", "110", "99", "105", "88")).max_drawdown_pct == Decimal("0.2000")
    assert _metrics().max_drawdown_pct is None


# --- 3. adherence and slippage ------------------------------------------------------------------------------


def test_adherence_and_slippage() -> None:
    mt = _metrics(
        [_row("1", "0.1", qty=10, slip="0.30"), _row("-1", "-0.1", qty=30, slip="0.10")],
        answers=[True, False, True, None],
    )
    assert mt.adherence_pct == Decimal("0.6667")
    assert mt.avg_slippage == Decimal("0.2000")
    assert mt.avg_slippage_per_share == Decimal("0.0100")
    assert _metrics(answers=[None, None]).adherence_pct is None


def test_empty_rows_give_zeros_and_nones() -> None:
    mt = _metrics()
    assert (mt.trades, mt.wins, mt.losses, mt.trades_without_r) == (0, 0, 0, 0)
    assert mt.total_pnl == 0 and mt.total_fees == 0
    for name in (
        "win_rate",
        "avg_win_r",
        "avg_loss_r",
        "expectancy_r",
        "profit_factor",
        "avg_slippage",
        "avg_slippage_per_share",
        "max_drawdown_pct",
        "adherence_pct",
    ):
        assert getattr(mt, name) is None, name
    assert len(mt.r_histogram) == 18 and all(b.count == 0 for b in mt.r_histogram)


def test_no_float_in_metrics() -> None:
    mt = _metrics([_row("20", "2", slip="0.3"), _row("-10", "-1", slip="0.1")], ("100", "90"), [True, False])
    for name in Metrics.__slots__:
        assert not isinstance(getattr(mt, name), float), name


# --- 6. histogram -------------------------------------------------------------------------------------------


def _bin_of(value: str) -> tuple[Decimal, Decimal]:
    hist = r_histogram([Decimal(value)])
    hit = [b for b in hist if b.count == 1]
    assert len(hit) == 1
    return hit[0].lo, hit[0].hi


def test_r_histogram_bins() -> None:
    bins = r_histogram([])
    assert len(bins) == 18
    assert (bins[0].lo, bins[0].hi) == (OPEN_LOW, R_LOW)
    assert (bins[-1].lo, bins[-1].hi) == (R_HIGH, OPEN_HIGH)
    assert [str(b.lo) for b in bins[1:3]] == ["-3.0", "-2.5"]
    for left, right in zip(bins, bins[1:], strict=False):
        assert left.hi == right.lo
    assert _bin_of("2") == (Decimal("2.0"), Decimal("2.5"))
    assert _bin_of("-1") == (Decimal("-1.0"), Decimal("-0.5"))
    assert _bin_of("-7") == (OPEN_LOW, R_LOW)
    assert _bin_of("5") == (R_HIGH, OPEN_HIGH)
    assert _bin_of("-3") == (Decimal("-3.0"), Decimal("-2.5"))
    assert _bin_of("4.9999") == (Decimal("4.5"), Decimal("5.0"))
    assert _bin_of("-3.0001") == (OPEN_LOW, R_LOW)
    counts = r_histogram([Decimal("2"), Decimal("-1"), Decimal("-1"), Decimal("-7"), Decimal("5")])
    assert sum(b.count for b in counts) == 5 and len(counts) == 18


# --- 4 and 5. from the database -----------------------------------------------------------------------------


def _snap(run_id: int, ts: datetime, equity: Decimal, peak: Decimal) -> m.EquitySnapshot:
    dd = ((peak - equity) / peak).quantize(Q4, ROUND_HALF_UP)  # as SimBroker.snapshot_equity
    return m.EquitySnapshot(
        run_id=run_id,
        ts=ts,
        equity=equity,
        cash=equity,
        settled_cash=equity,
        peak_equity=peak,
        drawdown_pct=dd,
    )


def _add_equity(s: Session, run_id: int, points: Sequence[tuple[datetime, str]]) -> None:
    peak: Decimal | None = None
    for ts, value in points:
        eq = Decimal(value)
        peak = eq if peak is None else max(peak, eq)
        s.add(_snap(run_id, ts, eq, peak))


def _at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC)


def _seed(factory: sessionmaker[Session]) -> tuple[int, int]:
    """A run with seven trades over five sessions (one without R, awkward roundings), a journal and equity
    snapshots written the way the broker writes them; plus another run whose rows must never count."""
    with factory() as s:
        run = add_run(s, mode="replay", status="completed")
        other = add_run(s, mode="replay", status="completed")
        sym = add_symbol(s, "AAA")
        add_trade(s, run, sym, D1, "20.0000", "2.0000", slippage="0.0100", fees="1.0000", qty=10)
        add_trade(s, run, sym, D2, "-10.0000", "-1.0000", slippage="0.0300", fees="1.0000", qty=20)
        add_trade(s, run, sym, D2, "3.3300", "0.3333", slippage="0.0333", fees="0.5000", qty=30)
        add_trade(s, run, sym, D3, "-6.6700", "-0.6667", slippage="0.0001", qty=7)
        add_trade(s, run, sym, D4, "0.0000", "0.0000", slippage="0.0000", qty=5)
        add_trade(s, run, sym, D4, "-3.0000", None, slippage="0.0500", fees="0.2500", qty=11)
        add_trade(s, run, sym, D5, "7.1234", "0.7123", slippage="0.0123", qty=13)
        add_trade(s, other, sym, D3, "-99.0000", "-9.9000")
        _add_equity(
            s,
            run,
            [
                (_at(D1, 20), "10000"),
                (_at(D2, 20), "10500"),
                (_at(D3, 3, 30), "10100"),  # 23:30 ET on D2
                (_at(D3, 20), "9900"),
                (_at(D4, 20), "10200"),
                (_at(D5, 20), "10050"),
            ],
        )
        _add_equity(s, other, [(_at(D1, 20), "1000"), (_at(D2, 20), "1")])
        s.add(m.Journal(run_id=run, session_date=D1, rules_followed=True))
        s.add(m.Journal(run_id=run, session_date=D2, rules_followed=False))
        s.add(m.Journal(run_id=run, session_date=D3, rules_followed=True))
        s.add(m.Journal(run_id=run, session_date=D4, rules_followed=None))
        s.add(m.Journal(run_id=other, session_date=D4, rules_followed=False))
        s.commit()
    return run, other


VIEW_KEYS = (
    "trades",
    "wins",
    "win_rate",
    "expectancy_r",
    "avg_win_r",
    "avg_loss_r",
    "profit_factor",
    "avg_slippage",
    "max_drawdown_pct",
    "adherence_pct",
)


@pytest.mark.db
def test_whole_run_equals_v_trade_metrics(db_factory: sessionmaker[Session]) -> None:
    run, other = _seed(db_factory)
    for run_id in (run, other):
        mt = compute_metrics(db_factory, run_id)
        with db_factory() as s:
            view = s.execute(
                text("SELECT * FROM trader.v_trade_metrics WHERE run_id = :r"), {"r": run_id}
            ).one()
        for key in VIEW_KEYS:
            assert getattr(mt, key) == getattr(view, key), (run_id, key)
    mt = compute_metrics(db_factory, run)
    assert (mt.trades, mt.wins, mt.losses, mt.trades_without_r) == (7, 3, 4, 1)
    assert mt.max_drawdown_pct == Decimal("0.0571")  # (10500 - 9900) / 10500
    assert mt.adherence_pct == Decimal("0.6667")
    assert mt.total_pnl == Decimal("10.7834")
    assert mt.total_fees == Decimal("2.7500")
    assert mt.avg_slippage_per_share == (Decimal("0.1357") / 96).quantize(Q4, ROUND_HALF_UP)
    assert (mt.date_from, mt.date_to) == (None, None)


@pytest.mark.db
def test_empty_run_equals_the_view(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        run = add_run(s, mode="replay", status="completed")
        s.commit()
    mt = compute_metrics(db_factory, run)
    with db_factory() as s:
        view = s.execute(text("SELECT * FROM trader.v_trade_metrics WHERE run_id = :r"), {"r": run}).one()
    for key in VIEW_KEYS:
        assert getattr(mt, key) == getattr(view, key), key
    assert mt.total_pnl == 0 and mt.trades_without_r == 0


@pytest.mark.db
def test_ranges(db_factory: sessionmaker[Session]) -> None:
    run, _ = _seed(db_factory)
    rest = compute_metrics(db_factory, run, D2, None)  # excludes the first trade (+2R on D1)
    assert (rest.date_from, rest.date_to) == (D2, None)
    assert (rest.trades, rest.wins, rest.losses) == (6, 2, 4)
    # R over D2..D5: -1, 0.3333, -0.6667, 0, 0.7123 -> -0.6211 / 5
    assert rest.expectancy_r == (Decimal("-0.6211") / 5).quantize(Q4, ROUND_HALF_UP)
    assert rest.total_pnl == Decimal("-9.2166")
    # equity by ET date from D2: 10500, 10100 (23:30 ET on D2), 9900, 10200, 10050; the peak starts at 10500
    assert rest.max_drawdown_pct == Decimal("0.0571")
    assert rest.adherence_pct == Decimal("0.5000")  # D2 no, D3 yes, D4 unanswered

    later = compute_metrics(db_factory, run, D3, D5)
    # snapshots from D3 (ET): 9900, 10200, 10050; the running peak starts at 9900, never the old 10500
    assert later.max_drawdown_pct == Decimal("0.0147")  # (10200 - 10050) / 10200
    assert later.trades == 4

    d2_only = compute_metrics(db_factory, run, D2, D2)
    assert d2_only.trades == 2
    assert d2_only.max_drawdown_pct == Decimal("0.0381")  # 10500 then 10100 at 23:30 ET

    empty = compute_metrics(db_factory, run, date(2026, 11, 2), date(2026, 11, 6))
    assert (empty.trades, empty.wins, empty.losses) == (0, 0, 0)
    assert empty.win_rate is None and empty.expectancy_r is None and empty.profit_factor is None
    assert empty.max_drawdown_pct is None and empty.adherence_pct is None
    assert empty.total_pnl == 0 and all(b.count == 0 for b in empty.r_histogram)
