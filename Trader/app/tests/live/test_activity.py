"""DB-T5 (pure parts): the activity feed's text helpers, the summary-only scan counts, the route's part
message, `expand` parsing and the `Server-Timing` value. The database behaviour is in `test_activity_db.py`
and the route in `tests/api/test_live_route.py`."""

from datetime import UTC, date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from trader.api.livedata import activity, periods
from trader.api.livedata.activity import _Item, _order_price, _summary_scan_counts, _switch_value
from trader.api.routers.live import expand_ids, part_message, server_timing
from trader.db import models as m

TS = datetime(2026, 10, 6, 13, 36, tzinfo=UTC)  # 09:36 ET, 07:36 MT


def _order(order_type: str, stop: str | None = None, limit: str | None = None) -> m.Order:
    return m.Order(
        order_type=order_type,
        stop_price=Decimal(stop) if stop else None,
        limit_price=Decimal(limit) if limit else None,
    )


def test_order_price_text_by_order_type() -> None:
    assert _order_price(_order("stop", stop="182.4000")) == " @ 182.40"
    assert _order_price(_order("limit", limit="10.1234")) == " @ 10.1234"
    assert _order_price(_order("stop_limit", stop="10.50", limit="10.60")) == " @ 10.50 limit 10.60"
    assert _order_price(_order("market")) == ""
    assert _order_price(_order("stop")) == ""  # no price stored: nothing invented


def test_kill_switch_values_read_as_percent_or_r() -> None:
    assert _switch_value("daily_loss_pct", Decimal("0.052")) == "5.2%"
    assert _switch_value("max_drawdown_pct", Decimal("0.05")) == "5.0%"
    assert _switch_value("expectancy", Decimal("-0.2")) == "-0.20 R"


def test_mt_times_in_texts() -> None:
    assert activity._mt(TS, activity.DEFAULT_DISPLAY_TZ) == "07:36"
    # the zone's rules come from tzdata, not an offset; the default is the configured zone's default
    assert activity.DEFAULT_DISPLAY_TZ.key == "America/Edmonton"
    assert activity._mt(TS, ZoneInfo("America/Toronto")) == "09:36"


def test_signed_amounts() -> None:
    assert activity._signed(Decimal("-12.5")) == "-12.50"
    assert activity._signed(Decimal("12.5")) == "+12.50"
    assert activity._signed(Decimal("0")) == "0.00"


def test_free_text_is_masked_and_cut() -> None:
    assert activity._clean("login failed password=hunter2 now") == "login failed password=[REDACTED] now"
    assert activity._clean("x" * 500, 120) == "x" * 120
    assert activity._clean(None) == ""


def test_an_item_carries_its_chip_and_id() -> None:
    out = _Item(
        TS, "kill_switch_tripped", 7, "Kill switch daily loss tripped", tone="warn", link="/control"
    ).out()
    assert (out.id, out.chip, out.tone, out.link, out.ticker, out.amount) == (
        "kill_switch_tripped:7",
        "alerts",
        "warn",
        "/control",
        None,
        None,
    )
    assert _Item(TS, "scan", 1, "scan").out().chip == "scan"
    assert _Item(TS, "fill", 1, "f").out().chip == "trades"
    assert _Item(TS, "proposal_expired", 1, "p").out().chip == "proposals"


def test_summary_counts_only_for_a_summary_only_scan() -> None:
    ranked = {"scan_detail": "ranked", "counts": {"rejects_by_rule": {"rvol_below_min": 400, "": 2, "x": 0}}}
    assert _summary_scan_counts([ranked]) == {"rvol_below_min": 400, "unknown": 2}
    assert _summary_scan_counts([{"scan_detail": "all", "counts": {"rejects_by_rule": {"a": 1}}}]) is None
    assert _summary_scan_counts([{"scan_detail": "ranked"}, "junk", None]) is None
    assert _summary_scan_counts([]) is None


@pytest.mark.parametrize(
    ("raw", "ids"),
    [
        (None, frozenset()),
        ("12", frozenset({12})),
        ("12,15", frozenset({12, 15})),
        ("1,2,3", frozenset({1, 2, 3})),
    ],
)
def test_expand_ids(raw: str | None, ids: frozenset[int]) -> None:
    assert expand_ids(raw) == ids


def test_part_message_is_typed_masked_and_short() -> None:
    msg = part_message(RuntimeError("boom token=abc123def " + "y" * 300))
    assert msg.startswith("RuntimeError: boom token=[REDACTED] ")
    assert "abc123def" not in msg
    assert len(msg) == len("RuntimeError: ") + 120


def test_server_timing_lists_app_first_then_each_part() -> None:
    value = server_timing(12.345, [("positions", 1.0), ("activity", 2.25)])
    assert value == "app;dur=12.3, positions;dur=1.0, activity;dur=2.2"


def test_the_day_bounds_come_from_the_periods_module(monkeypatch: pytest.MonkeyPatch) -> None:
    """`activity_feed` reads its ET day bounds through `periods.et_day_bounds` (one definition, S5)."""
    seen: list[date] = []

    def bounds(d: date) -> tuple[datetime, datetime]:
        seen.append(d)
        raise LookupError("stop here")

    monkeypatch.setattr(periods, "et_day_bounds", bounds)
    with pytest.raises(LookupError):
        activity.activity_feed(None, 1, date(2026, 10, 6))  # type: ignore[arg-type]
    assert seen == [date(2026, 10, 6)]
