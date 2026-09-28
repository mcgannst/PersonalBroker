"""P6-T9 acceptance tests 3 and 4 (and the contract shapes T10/T12 build against): the decision log types,
the six `reports.decisions_*` settings, the stub modules and the API schemas."""

import dataclasses
import inspect
import subprocess
import sys
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, get_args

import pytest
from pydantic import ValidationError

from trader.api import schemas
from trader.api.forms import OTHER_GROUP, group_of, model_fields_out
from trader.decisions import export, prune, read, recorder
from trader.decisions import types as t
from trader.settings_store import RuntimeSettings

D = date(2026, 9, 28)
T0 = datetime(2026, 9, 28, 13, 35, 5, tzinfo=UTC)


# --- types ------------------------------------------------------------------------------------------------
def test_stage_order_is_every_stage_in_the_documented_order() -> None:
    assert t.STAGE_ORDER == (
        "universe",
        "premarket",
        "scan",
        "signal",
        "risk",
        "proposal",
        "approval",
        "order",
        "fill",
        "exit",
        "overlay",
        "kill_switch",
        "day",
    )
    assert set(t.STAGE_ORDER) == set(get_args(t.DecisionStage))


def test_outcomes() -> None:
    assert t.OUTCOMES == get_args(t.DecisionOutcome)
    assert set(t.OUTCOMES) == {
        "info",
        "listed",
        "classified",
        "passed",
        "rejected",
        "proposed",
        "approved",
        "auto_approved",
        "declined",
        "expired",
        "blocked",
        "submitted",
        "filled",
        "cancelled",
        "exited",
        "tripped",
        "reset",
        "error",
    }
    # every literal fits its varchar(20) column
    assert max(len(v) for v in (*t.OUTCOMES, *t.STAGE_ORDER)) <= 20


def test_constants() -> None:
    assert t.SOURCE == "decisions"
    assert t.MAX_REASON_CHARS == 500
    assert t.MAX_DATA_BYTES == 8192
    assert t.LOCK_PREFIX == "trader.decisions"
    assert set(get_args(t.CheckOp)) == {">=", "<=", "between", "==", "!=", "present", "absent"}


def test_check_is_frozen_with_slots() -> None:
    c = t.Check(name="rvol", value="3.2", op=">=", threshold="1.00", passed=True)
    with pytest.raises(dataclasses.FrozenInstanceError):
        c.passed = False  # type: ignore[misc]
    assert not hasattr(c, "__dict__")


def test_decision_record_defaults() -> None:
    r = t.DecisionRecord(session_date=D, stage="day", outcome="info", ts=T0)
    assert (r.strategy_key, r.symbol_id, r.ticker, r.rule, r.reason) == (None, None, None, None, None)
    assert r.ref == {} and r.data == {}
    other = t.DecisionRecord(session_date=D, stage="day", outcome="info", ts=T0)
    assert r.ref is not other.ref  # a fresh dict per record


def _fields(cls: type) -> list[str]:
    return [f.name for f in dataclasses.fields(cls)]


def test_dataclass_shapes() -> None:
    assert _fields(t.DaySummary) == [
        "run_id",
        "session_date",
        "final",
        "universe_size",
        "universe_source",
        "premarket_listed",
        "premarket_classified",
        "scanned",
        "rvol_passed",
        "ranked",
        "passed",
        "rejects_by_rule",
        "signals",
        "risk_rejections",
        "proposals",
        "approvals",
        "median_decision_seconds",
        "fills",
        "avg_fill_diff_per_share",
        "trades",
        "wins",
        "losses",
        "pnl",
        "pnl_r",
        "exits_by_reason",
        "notes",
    ]
    assert _fields(t.RecordResult) == ["run_id", "session_date", "skipped", "stages", "final"]
    assert set(get_args(t.RecordSkip)) == {"unchanged", "final", "disabled", "not_session"}
    assert _fields(t.RecorderDeps) == ["factory", "clock", "calendar", "settings", "scan_data"]
    assert _fields(t.DayView) == [
        "run_id",
        "run_mode",
        "session_date",
        "final",
        "recorded_at",
        "summary",
        "summary_text",
        "rows",
        "total",
    ]
    assert _fields(t.DayItem) == ["run_id", "session_date", "final", "summary_text", "proposals", "trades"]
    assert _fields(t.DecisionRowView) == [
        "id",
        "run_id",
        "session_date",
        "seq",
        "stage",
        "strategy_key",
        "symbol_id",
        "ticker",
        "outcome",
        "rule",
        "reason",
        "ts",
        "ref",
        "data",
        "recorded_at",
        "final",
    ]
    assert _fields(prune.PruneResult) == ["live_deleted", "replay_deleted"]
    for cls in (t.DecisionRecord, t.DaySummary, t.RecordResult, t.RecorderDeps, t.DayView, t.DayItem):
        assert cls.__dataclass_params__.frozen  # type: ignore[union-attr]


def test_scan_data_protocol_and_live_scan_data() -> None:
    for name in ("universe", "open_bar_stats", "stored_opening_bars"):
        assert inspect.iscoroutinefunction(getattr(t.ScanData, name))
        assert inspect.iscoroutinefunction(getattr(recorder.LiveScanData, name))
    live: t.ScanData = recorder.LiveScanData(factory=None)  # type: ignore[arg-type]
    assert live is not None


# --- 3: settings --------------------------------------------------------------------------------------------
DEFAULTS: dict[str, tuple[str, Any]] = {
    "reports.decisions_enabled": ("reports_decisions_enabled", True),
    "reports.decisions_scan_detail": ("reports_decisions_scan_detail", "all"),
    "reports.decisions_refresh_seconds": ("reports_decisions_refresh_seconds", 60),
    "reports.decisions_retention_days": ("reports_decisions_retention_days", 400),
    "reports.decisions_replay_retention_days": ("reports_decisions_replay_retention_days", 30),
    "reports.decisions_in_summary": ("reports_decisions_in_summary", True),
}
BOUNDS: dict[str, tuple[int, int]] = {
    "reports.decisions_refresh_seconds": (15, 600),
    "reports.decisions_retention_days": (30, 3650),
    "reports.decisions_replay_retention_days": (1, 3650),
}


@pytest.mark.parametrize("key", sorted(DEFAULTS))
def test_setting_default_alias_and_group(key: str) -> None:
    field, default = DEFAULTS[key]
    assert RuntimeSettings.model_fields[field].alias == key
    value = getattr(RuntimeSettings(), field)
    assert value == default and type(value) is type(default)
    assert group_of(key) == "Reports" != OTHER_GROUP


def test_settings_appear_in_the_reports_form() -> None:
    fields = {f.name: f for f in model_fields_out(RuntimeSettings, by_alias=True)}
    for key in DEFAULTS:
        assert key in fields and group_of(key) == "Reports"
    assert fields["reports.decisions_scan_detail"].kind == "enum"
    assert fields["reports.decisions_scan_detail"].enum == ["all", "ranked"]
    assert fields["reports.decisions_enabled"].kind == "boolean"
    assert fields["reports.decisions_refresh_seconds"].minimum == "15"
    assert fields["reports.decisions_refresh_seconds"].maximum == "600"


@pytest.mark.parametrize("key", sorted(BOUNDS))
def test_setting_bounds(key: str) -> None:
    low, high = BOUNDS[key]
    field = DEFAULTS[key][0]
    assert getattr(RuntimeSettings.model_validate({key: low}), field) == low
    assert getattr(RuntimeSettings.model_validate({key: high}), field) == high
    for bad in (low - 1, high + 1):
        with pytest.raises(ValidationError):
            RuntimeSettings.model_validate({key: bad})


def test_scan_detail_is_all_or_ranked() -> None:
    assert RuntimeSettings.model_validate(
        {"reports.decisions_scan_detail": "ranked"}
    ).reports_decisions_scan_detail == ("ranked")
    with pytest.raises(ValidationError):
        RuntimeSettings.model_validate({"reports.decisions_scan_detail": "none"})


def test_a_snapshot_without_the_keys_gets_the_defaults() -> None:
    """A replay settings snapshot taken before migration 0007 lacks the keys (T10)."""
    snap = RuntimeSettings().model_dump(mode="json", by_alias=True)
    for key in DEFAULTS:
        del snap[key]
    s = RuntimeSettings.model_validate(snap)
    assert s.reports_decisions_enabled is True and s.reports_decisions_scan_detail == "all"


# --- 4: stubs ---------------------------------------------------------------------------------------------
# Every stub is implemented now: recorder, orb_explain, summary and prune by P6-T10 (test_recorder.py and
# friends), read and export by P6-T12 (test_read.py, test_export.py). Their signatures stay pinned here.


def test_stub_signatures() -> None:
    assert list(inspect.signature(recorder.record_day).parameters) == [
        "deps",
        "run_id",
        "session_date",
        "final",
        "rebuild",
    ]
    load = inspect.signature(read.load_day).parameters
    assert [p for p in load] == [
        "factory",
        "run_id",
        "session_date",
        "stage",
        "outcome",
        "ticker",
        "limit",
        "offset",
    ]
    assert load["limit"].default == 2000 and load["offset"].default == 0
    assert inspect.signature(read.list_days).parameters["limit"].default == 30


def test_csv_columns() -> None:
    cols = export.DECISION_CSV_COLUMNS
    assert cols[:12] == (
        "run_id",
        "run_mode",
        "session_date",
        "seq",
        "ts",
        "stage",
        "strategy",
        "ticker",
        "outcome",
        "rule",
        "reason",
        "checks",
    )
    assert cols[-1] == "exit_category" and len(cols) == len(set(cols)) == 35


FORBIDDEN_MODULES = (
    "trader.engine.orchestrator",
    "trader.engine.proposals",
    "trader.broker.sim_broker",
    "trader.jobs.runner",
    "trader.notify.notifier",
    "trader.notify.relay",
    "trader.adapters.questrade.client",
    "trader.adapters.questrade.auth",
    "trader.runtime",
    "anthropic",
    "httpx",
)


def test_the_package_imports_cleanly_without_trading_or_network_modules() -> None:
    """`trader.decisions` loads only read-only types from decision-path packages (OrbSipParams, market
    types): importing it in a fresh interpreter pulls in no engine, broker, job runner, notifier, Questrade,
    Claude or HTTP client module."""
    code = (
        "import sys\n"
        "import trader.decisions, trader.decisions.types, trader.decisions.recorder\n"
        "import trader.decisions.orb_explain, trader.decisions.summary, trader.decisions.prune\n"
        "import trader.decisions.read, trader.decisions.export\n"
        "print('\\n'.join(sorted(sys.modules)))\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    loaded = set(out.split())
    bad = sorted(m for m in loaded for f in FORBIDDEN_MODULES if m == f or m.startswith(f + "."))
    assert bad == []


# --- API schemas (the TS mirror is checked by tests/api/test_ts_contract.py) --------------------------------
def test_api_schema_shapes() -> None:
    assert list(schemas.CheckOut.model_fields) == ["name", "value", "op", "threshold", "passed"]
    assert list(schemas.RuleCountOut.model_fields) == ["rule", "count"]
    assert list(schemas.DecisionRowOut.model_fields) == [
        "seq",
        "stage",
        "strategy_key",
        "symbol_id",
        "ticker",
        "outcome",
        "rule",
        "reason",
        "ts",
        "ref",
        "checks",
        "data",
    ]
    assert list(schemas.DecisionSummaryOut.model_fields) == [
        "text",
        "universe_size",
        "premarket_listed",
        "premarket_classified",
        "scanned",
        "rvol_passed",
        "ranked",
        "passed",
        "rejects_by_rule",
        "signals",
        "risk_rejections",
        "proposals",
        "approvals",
        "median_decision_seconds",
        "fills",
        "avg_fill_diff_per_share",
        "trades",
        "wins",
        "losses",
        "pnl",
        "pnl_r",
        "exits_by_reason",
        "notes",
    ]
    assert list(schemas.DecisionDayOut.model_fields) == [
        "run_id",
        "run_mode",
        "session_date",
        "final",
        "recorded_at",
        "summary",
        "rows",
        "total",
    ]
    assert list(schemas.DecisionDayItemOut.model_fields) == [
        "run_id",
        "session_date",
        "final",
        "summary_text",
        "proposals",
        "trades",
    ]
    assert list(schemas.DecisionDaysOut.model_fields) == ["days"]
    assert schemas.DecisionStage is t.DecisionStage and schemas.DecisionOutcome is t.DecisionOutcome


def test_decision_row_out_serialises_utc_and_refuses_unknown_literals() -> None:
    row = schemas.DecisionRowOut(
        seq=1,
        stage="scan",
        outcome="rejected",
        ts=T0,
        ref={"candidate_id": 5},
        checks=[schemas.CheckOut(name="rvol", value="0.80", op=">=", threshold="1.00", passed=False)],
        data={"rank": None},
    )
    body = row.model_dump(mode="json")
    assert body["ts"] == "2026-09-28T13:35:05Z"
    assert body["strategy_key"] is None and body["checks"][0]["passed"] is False
    with pytest.raises(ValidationError):
        schemas.DecisionRowOut(seq=1, stage="nope", outcome="info", ts=T0, ref={}, checks=[], data={})


def test_summary_money_is_a_string() -> None:
    s = schemas.DecisionSummaryOut(
        text="812 scanned",
        universe_size=812,
        premarket_listed=0,
        premarket_classified=0,
        scanned=812,
        rvol_passed=14,
        ranked=14,
        passed=1,
        rejects_by_rule=[schemas.RuleCountOut(rule="rvol_below_min", count=790)],
        signals=1,
        risk_rejections=[],
        proposals=1,
        approvals={"manual": 1},
        median_decision_seconds=42.0,
        fills=2,
        avg_fill_diff_per_share=Decimal("0.02"),
        trades=1,
        wins=1,
        losses=0,
        pnl=Decimal("12.5000"),
        pnl_r=Decimal("0.8"),
        exits_by_reason=[schemas.RuleCountOut(rule="flatten", count=1)],
        notes=[],
    ).model_dump(mode="json")
    assert s["pnl"] == "12.5000" and s["avg_fill_diff_per_share"] == "0.02" and s["pnl_r"] == "0.8"
