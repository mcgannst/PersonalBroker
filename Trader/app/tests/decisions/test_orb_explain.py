"""P6-T10 acceptance tests 1 and 2: `explain_orb` gives every rule with value, op, threshold and verdict, and
its first failure equals the reject reason the real `OrbSip` stored, for every candidate."""

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest

from tests.strategies.fakes import (
    FakeCatalyst,
    FakeCatalysts,
    FakeData,
    bar,
    make_ctx,
    position,
    working_entry,
)
from trader.decisions.orb_explain import CONTEXT_CHECKS, explain_orb, first_failure
from trader.decisions.types import Check
from trader.strategies.base import CandidateRecord
from trader.strategies.orb_sip import ORB_EVENT, OrbSip, OrbSipParams

BULL = {
    "start": "2026-10-06T13:30:00+00:00",
    "open": "22.00",
    "high": "22.50",
    "low": "21.90",
    "close": "22.40",
}


def by_name(checks: tuple[Check, ...]) -> dict[str, Check]:
    return {c.name: c for c in checks}


def test_1_every_check_for_a_hand_made_candidate() -> None:
    data = {
        "ticker": "NVDA",
        "rvol": "3.2",
        "rank": 1,
        "atr14": "0.61",
        "price": "22.40",
        "avg_volume": 1_400_000,
        "avg_open_vol_14d": "1000",
    }
    catalyst = {"type": "earnings_beat", "direction": "bullish", "quality": 70, "classified": True}
    checks = explain_orb(
        data, {**BULL, "volume": 3200}, OrbSipParams(), catalyst=catalyst, reject_reason=None
    )
    names = [c.name for c in checks]
    assert names == [
        "rvol",
        "rank",
        "not_held",
        "no_working_entry",
        "direction",
        "price",
        "atr14_present",
        "atr14",
        "avg_volume",
        "stop_valid",
        "catalyst_present",
        "catalyst_not_bearish",
        "catalyst_quality",
        "slot_left",
    ]
    c = by_name(checks)
    assert c["rvol"] == Check("rvol", "3.2", ">=", "1.00", True)
    assert c["rank"] == Check("rank", "1", "<=", "20", True)
    assert c["direction"] == Check("direction", "bullish", "==", "bullish", True)
    assert c["price"] == Check("price", "22.40", "between", "5..50", True)
    assert c["atr14_present"].passed is True
    assert c["atr14"] == Check("atr14", "0.61", ">=", "0.50", True)
    assert c["avg_volume"] == Check("avg_volume", "1400000", ">=", "1000000", True)
    # entry 22.50 + 0.01 = 22.51; stop 22.51 - 0.10 x 0.61 = 22.449 -> 22.4490
    assert c["stop_valid"] == Check("stop_valid", "22.4490", "between", "0..22.5100", True)
    assert c["catalyst_present"] == Check("catalyst_present", "earnings_beat", "present", None, True)
    assert c["catalyst_not_bearish"] == Check("catalyst_not_bearish", "bullish", "!=", "bearish", True)
    assert c["catalyst_quality"] == Check("catalyst_quality", "70", ">=", "50", True)
    for name in CONTEXT_CHECKS:
        assert (c[name].value, c[name].threshold, c[name].passed) == (None, None, True)
    assert first_failure(checks) is None


def test_1_each_failing_value_is_shown_with_its_threshold() -> None:
    data = {"rvol": "0.80", "rank": 3, "atr14": "0.40", "price": "60", "avg_volume": None}
    bearish = {**BULL, "open": "61", "high": "61.5", "low": "59.5", "close": "60"}
    checks = by_name(
        explain_orb(data, bearish, OrbSipParams(), catalyst=None, reject_reason="bearish_candle")
    )
    assert checks["rvol"] == Check("rvol", "0.80", ">=", "1.00", False)
    assert checks["direction"].value == "bearish" and checks["direction"].passed is False
    assert checks["price"] == Check("price", "60", "between", "5..50", False)
    assert checks["atr14"] == Check("atr14", "0.40", ">=", "0.50", False)
    assert checks["avg_volume"] == Check("avg_volume", None, ">=", "1000000", False)
    # no catalyst: present fails, the two that need its values have a missing input
    assert checks["catalyst_present"].passed is False
    assert checks["catalyst_not_bearish"].passed is None and checks["catalyst_quality"].passed is None
    # the context checks follow the stored reject reason
    assert checks["not_held"].passed is True and checks["slot_left"].passed is True


def test_catalyst_checks_are_absent_when_not_required() -> None:
    checks = explain_orb(
        {"rvol": "2", "rank": 1, "atr14": "1", "avg_volume": 2_000_000},
        {**BULL, "volume": 1},
        OrbSipParams(require_catalyst=False),
        catalyst=None,
        reject_reason=None,
    )
    assert not any(c.name.startswith("catalyst") for c in checks)
    assert first_failure(checks) is None


def test_first_failure_maps_names_and_skips_missing_inputs() -> None:
    assert first_failure(
        [Check("rvol", None, ">=", "1", None), Check("price", "1", "between", "5..50", False)]
    ) == ("price_out_of_range")
    assert first_failure([Check("direction", "doji", "==", "bullish", False)]) == "doji"
    assert first_failure([Check("direction", "malformed", "==", "bullish", False)]) == "malformed_bar"
    assert first_failure([]) is None


# --- 2: consistency with the real strategy ------------------------------------------------------------------
@dataclass
class Case:
    """One generated ORB scan: universe names with an opening bar and overrides."""

    names: list[dict[str, Any]]
    cats: dict[int, FakeCatalyst]
    params: dict[str, Any]
    held: tuple[int, ...] = ()
    working: tuple[int, ...] = ()
    entries_today: int = 0


def _name(sid: int, **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "sid": sid,
        "bar": ("21.00", "21.50", "20.90", "21.40", 5000 - sid * 10),  # bullish, rvol ~5
        "atr": "1.00",
        "avg_volume": 2_000_000,
        "avg_open_vol": "1000",
        "cat": FakeCatalyst(),
    }
    base.update(kw)
    return base


def _cases() -> list[Case]:
    good = [_name(i) for i in range(1, 4)]
    one_each: list[dict[str, Any]] = [
        _name(10, bar=("21.00", "21.50", "20.50", "20.60", 5000)),  # bearish
        _name(11, bar=("21.00", "21.50", "20.50", "21.01", 5000)),  # doji
        _name(12, bar=("21.00", "20.50", "21.50", "21.40", 5000)),  # malformed (high < low)
        _name(13, bar=("3.00", "3.50", "2.90", "3.40", 5000)),  # price below
        _name(14, bar=("60.00", "61.50", "59.90", "61.40", 5000)),  # price above
        _name(15, atr=None),  # atr missing
        _name(16, atr="0.30"),  # atr below
        _name(17, avg_volume=500_000),  # volume below
        _name(18, avg_volume=None),  # volume missing
        _name(19, cat=None),  # catalyst missing
        _name(20, cat=FakeCatalyst(classified=False)),  # unclassified
        _name(21, cat=FakeCatalyst(catalyst_type="none")),  # no catalyst type
        _name(22, cat=FakeCatalyst(direction="bearish")),  # bearish catalyst
        _name(23, cat=FakeCatalyst(quality=20)),  # low quality
        _name(24, cat=FakeCatalyst(quality=None)),  # no quality
        _name(25),  # passes (or lower rank)
        _name(26),
    ]
    several = [
        _name(30, bar=("21.00", "21.50", "20.50", "20.60", 5000), atr=None, cat=None),
        _name(31, bar=("3.00", "3.50", "2.90", "3.40", 5000), avg_volume=None, cat=FakeCatalyst(quality=1)),
        _name(32, atr="0.10", avg_volume=10, cat=FakeCatalyst(direction="bearish")),
        _name(33, cat=FakeCatalyst(direction="bearish", quality=5)),
    ]
    return [
        Case(good, {}, {}),
        Case(one_each, {}, {"max_positions": 2}),
        Case(one_each, {}, {"max_positions": 1, "top_n": 30}),
        Case(several, {}, {}),
        Case(good + one_each, {}, {"max_positions": 3}, held=(1,), working=(2,)),
        Case(good, {}, {"require_catalyst": False, "max_positions": 1}),
        Case(one_each, {}, {"catalyst_min_quality": 90, "doji_body_pct_max": "0.02"}),
        # a huge stop fraction: stop at or below zero -> stop_invalid
        Case(
            [_name(40, bar=("5.00", "5.20", "4.90", "5.10", 5000), atr="60")], {}, {"stop_atr_fraction": "1"}
        ),
        Case(good, {}, {"max_positions": 2}, entries_today=1),
    ]


Scan = tuple[OrbSipParams, OrbSip, Any, Any, dict[int, Mapping[str, Any]]]


def _build(case: Case) -> Scan:
    data = FakeData()
    cats: dict[int, FakeCatalyst] = {}
    for n in case.names:
        o, h, lo, c, v = n["bar"]
        data.add(
            n["sid"],
            f"T{n['sid']}",
            bar(o, h, lo, c, v),
            atr=n["atr"],
            avg_volume=n["avg_volume"],
            avg_open_vol=n["avg_open_vol"],
        )
        if n["cat"] is not None:
            cats[n["sid"]] = n["cat"]
    params = OrbSipParams(**case.params)
    strategy = OrbSip(params)
    ctx = make_ctx(
        data,
        params,
        FakeCatalysts(cats),
        positions=[position(100 + i, sid) for i, sid in enumerate(case.held)],
        orders=[working_entry(200 + i, sid) for i, sid in enumerate(case.working)],
        entries_today=case.entries_today,
    )
    event = next(e for e in strategy.schedule(ctx.calendar) if e.key == ORB_EVENT)
    as_json = {
        sid: {
            "type": c.catalyst_type,
            "direction": c.direction,
            "quality": c.quality,
            "classified": c.classified,
        }
        for sid, c in cats.items()
    }
    return params, strategy, ctx, event, as_json


EXPECTED_REASONS: dict[int, set[str | None]] = {
    1: {
        "bearish_candle",
        "doji",
        "malformed_bar",
        "price_out_of_range",
        "atr_missing",
        "atr_below_min",
        "avg_volume_below_min",
        "catalyst_missing",
        "catalyst_bearish",
        "catalyst_low_quality",
        None,
    },
    2: {"lower_rank", None},
    4: {"already_held", "entry_working"},
    7: {"stop_invalid"},
}


@pytest.mark.parametrize("index", range(len(_cases())))
async def test_2_first_failure_equals_the_strategys_reject_reason(index: int) -> None:
    params, strategy, ctx, event, catalysts = _build(_cases()[index])
    await strategy.on_event(ctx, event)
    records: list[CandidateRecord] = ctx.candidates
    assert records  # every case ranks something
    reasons: set[str | None] = set()
    for rec in records:
        catalyst = rec.data.get("catalyst") or catalysts.get(rec.symbol_id)
        checks = explain_orb(rec.data, rec.candle, params, catalyst=catalyst, reject_reason=rec.reject_reason)
        failing = [c for c in checks if c.passed is False]
        assert first_failure(checks) == rec.reject_reason, (rec.data.get("ticker"), failing)
        assert (rec.reject_reason is None) == rec.passed
        reasons.add(rec.reject_reason)
    assert EXPECTED_REASONS.get(index, set()) <= reasons


def test_2_the_generated_set_covers_the_context_and_stop_rules() -> None:
    """Held, working and stop_invalid are produced by cases 4 and 7 (checked by the parametrized test)."""
    cases = _cases()
    assert cases[4].held and cases[4].working
    assert cases[7].params["stop_atr_fraction"] == "1"
    assert Decimal(cases[7].names[0]["atr"]) > Decimal("5")
