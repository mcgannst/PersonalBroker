"""P5-T1 acceptance tests 4 and 7: the Phase 5 API schemas, `metrics_out`, and their TypeScript mirror.

The P4-T18 mirror test (tests/api/test_ts_contract.py) checks every model's fields and nullability; this file
adds the Phase 5 literal unions to its literal check and pins the wire format of the new models.
"""

import dataclasses
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, get_args

import pytest
from pydantic import ValidationError

from tests.api.test_ts_contract import ts_types, ts_unions
from trader.api import schemas
from trader.api.views import metrics_out
from trader.replay import types as replay_types
from trader.reports.metrics import OPEN_HIGH, OPEN_LOW, R_BINS, R_HIGH, R_LOW, HistogramBin, Metrics

T0 = datetime(2026, 11, 27, 21, 5, tzinfo=UTC)
WIDTH = (R_HIGH - R_LOW) / R_BINS


def _bins() -> tuple[HistogramBin, ...]:
    inner = tuple(HistogramBin(R_LOW + WIDTH * i, R_LOW + WIDTH * (i + 1), i % 3) for i in range(R_BINS))
    return (HistogramBin(OPEN_LOW, R_LOW, 1), *inner, HistogramBin(R_HIGH, OPEN_HIGH, 2))


def _metrics(**over: Any) -> Metrics:
    base: dict[str, Any] = {
        "run_id": 7,
        "date_from": date(2026, 11, 23),
        "date_to": date(2026, 11, 27),
        "trades": 5,
        "wins": 2,
        "losses": 3,
        "win_rate": Decimal("0.4000"),
        "avg_win_r": Decimal("1.2500"),
        "avg_loss_r": Decimal("-1.0000"),
        "expectancy_r": Decimal("0.1250"),
        "profit_factor": Decimal("1.0870"),
        "avg_slippage": Decimal("0.2000"),
        "avg_slippage_per_share": Decimal("0.0100"),
        "max_drawdown_pct": Decimal("0.2000"),
        "adherence_pct": Decimal("0.6667"),
        "total_pnl": Decimal("2.0000"),
        "total_fees": Decimal("0.0412"),
        "trades_without_r": 1,
        "r_histogram": _bins(),
    }
    return Metrics(**{**base, **over})


# --- test 7: literal unions in the mirror -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "literal"),
    [
        ("ReplayStatus", schemas.ReplayStatus),
        ("DataMode", schemas.DataMode),
        ("CatalystMode", schemas.CatalystMode),
        ("CommentaryStatus", schemas.CommentaryStatus),
        ("Topic", schemas.Topic),
        ("ManualJob", schemas.ManualJob),
    ],
)
def test_phase5_literals_are_mirrored(name: str, literal: Any) -> None:
    unions = ts_unions()
    assert name in unions, f"types.ts has no union {name}"
    assert unions[name] == set(get_args(literal))


def test_literal_values() -> None:
    assert set(get_args(schemas.Topic)) >= {"replays", "reports"}
    assert "weekly" in get_args(schemas.ManualJob)
    assert get_args(schemas.ReplayStatus) == ("queued", "running", "completed", "failed", "cancelled")
    assert schemas.ReplayStatus is replay_types.ReplayStatus  # one definition
    assert get_args(schemas.CommentaryStatus) == ("ok", "disabled", "budget", "rejected", "error")


def test_new_models_are_in_types_ts() -> None:
    ts = ts_types()
    for name in (
        "ReplayStrategyIn",
        "ReplayIn",
        "ReplayStrategyOut",
        "ReplayProgressOut",
        "ReplaySummaryOut",
        "ReplayOut",
        "ReplayOptionsOut",
        "WeeklyReportOut",
    ):
        assert name in ts, name
    assert {"losses", "total_fees", "avg_slippage_per_share", "trades_without_r"} <= set(ts["MetricsOut"])


# --- test 4: metrics_out and the wire format ----------------------------------------------------------------


def test_metrics_out_maps_every_field() -> None:
    mt = _metrics()
    out = metrics_out(mt)
    renamed = {"date_from": "from_date", "date_to": "to_date"}
    for f in dataclasses.fields(Metrics):
        if f.name == "r_histogram":
            continue
        assert getattr(out, renamed.get(f.name, f.name)) == getattr(mt, f.name), f.name
    assert len(out.r_histogram) == 18
    assert [(b.lo, b.hi, b.count) for b in out.r_histogram] == [(b.lo, b.hi, b.count) for b in mt.r_histogram]
    assert set(schemas.MetricsOut.model_fields) == {
        renamed.get(f.name, f.name) for f in dataclasses.fields(Metrics)
    }
    wire = out.model_dump(mode="json")
    assert wire["r_histogram"][0]["lo"] == "-Infinity" and wire["r_histogram"][-1]["hi"] == "Infinity"
    assert wire["total_fees"] == "0.0412" and wire["avg_slippage_per_share"] == "0.0100"
    assert schemas.MetricsOut.model_validate_json(out.model_dump_json()) == out


def test_metrics_out_of_an_empty_run() -> None:
    empty = _metrics(
        trades=0,
        wins=0,
        losses=0,
        win_rate=None,
        avg_win_r=None,
        avg_loss_r=None,
        expectancy_r=None,
        profit_factor=None,
        avg_slippage=None,
        avg_slippage_per_share=None,
        max_drawdown_pct=None,
        adherence_pct=None,
        total_pnl=Decimal(0),
        total_fees=Decimal(0),
        trades_without_r=0,
    )
    wire = metrics_out(empty).model_dump(mode="json")
    assert wire["win_rate"] is None and wire["total_pnl"] == "0" and wire["losses"] == 0


def test_metrics_out_without_the_new_fields_has_their_defaults() -> None:
    old = schemas.MetricsOut(run_id=1, trades=0, wins=0, total_pnl=Decimal(0), r_histogram=[])
    assert (old.losses, old.total_fees, old.avg_slippage_per_share, old.trades_without_r) == (
        0,
        Decimal(0),
        None,
        0,
    )


def _replay_out() -> schemas.ReplayOut:
    metrics = metrics_out(_metrics())
    return schemas.ReplayOut(
        id=12,
        label="P5 check (biased universe)",
        status="completed",
        date_from=date(2026, 11, 23),
        date_to=date(2026, 11, 27),
        created_at=T0,
        finished_at=T0,
        data_mode="offline",
        catalyst_mode="stored",
        half_spread_bps=Decimal("5"),
        overrides={"risk_pct": "0.01"},
        strategies=[
            schemas.ReplayStrategyOut(
                key="orb_sip",
                config_id=31,
                revision=2,
                version="1.0.0",
                scope="replay",
                enabled=True,
                params={"top_n": 10},
            )
        ],
        progress=schemas.ReplayProgressOut(
            sessions_total=4,
            sessions_done=4,
            current_date=date(2026, 11, 27),
            trades=5,
            forced_closes=0,
            biased_days=[date(2026, 11, 23)],
            missing_opening_bars=0,
            missing_minute_bars=1,
            questrade_requests=0,
        ),
        biased=True,
        cancel_requested=False,
        metrics=metrics,
        live_metrics=metrics,
        events=[],
    )


def test_replay_out_wire_format() -> None:
    wire = _replay_out().model_dump(mode="json")
    assert wire["half_spread_bps"] == "5"
    assert wire["date_from"] == "2026-11-23" and wire["progress"]["biased_days"] == ["2026-11-23"]
    assert wire["progress"]["current_date"] == "2026-11-27"
    assert wire["created_at"] == "2026-11-27T21:05:00Z"
    assert wire["metrics"]["expectancy_r"] == "0.1250" and wire["metrics"]["total_pnl"] == "2.0000"
    assert wire["strategies"][0]["scope"] == "replay"
    assert schemas.ReplayOut.model_validate_json(_replay_out().model_dump_json()) == _replay_out()


def test_replay_in_defaults_and_bounds() -> None:
    body = schemas.ReplayIn.model_validate({"date_from": "2026-11-23", "date_to": "2026-11-27"})
    assert body.overrides == {} and body.strategies == {} and body.offline is False and body.label is None
    full = schemas.ReplayIn.model_validate(
        {
            "date_from": "2026-11-23",
            "date_to": "2026-11-27",
            "label": "x" * 200,
            "overrides": {"risk_pct": "0.01"},
            "strategies": {"orb_sip": {"params": {"top_n": 10}}},
            "offline": True,
        }
    )
    assert full.strategies["orb_sip"].params == {"top_n": 10} and full.strategies["orb_sip"].enabled is None
    for bad in ({"label": "x" * 201}, {"offline": "yes"}, {"date_to": "not a date"}):
        with pytest.raises(ValidationError):
            schemas.ReplayIn.model_validate({"date_from": "2026-11-23", "date_to": "2026-11-27", **bad})


def test_summary_options_and_weekly_report_wire_format() -> None:
    summary = schemas.ReplaySummaryOut(
        id=12,
        status="running",
        date_from=date(2026, 11, 23),
        date_to=date(2026, 11, 27),
        created_at=T0,
        data_mode="full",
        biased=False,
        trades=0,
    )
    assert summary.model_dump(mode="json")["expectancy_r"] is None
    options = schemas.ReplayOptionsOut(
        override_keys=sorted(replay_types.REPLAY_OVERRIDE_KEYS),
        max_sessions=130,
        latest_allowed=date(2026, 11, 27),
        questrade_from=date(2026, 9, 3),
        busy=False,
        offline_now=True,
    )
    assert options.model_dump(mode="json")["archive_from"] is None
    report = schemas.WeeklyReportOut(
        week_start=date(2026, 11, 23),
        week_ending=date(2026, 11, 27),
        run_id=1,
        created_at=T0,
        updated_at=T0,
        commentary_status="budget",
        cost_usd=Decimal("0.000000"),
        facts={"week": {"sessions": 4}},
    )
    wire = report.model_dump(mode="json")
    assert wire["cost_usd"] == "0.000000" and wire["commentary"] is None and wire["telegram_status"] is None
    with pytest.raises(ValidationError):
        schemas.WeeklyReportOut.model_validate({**wire, "commentary_status": "maybe"})
