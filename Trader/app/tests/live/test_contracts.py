"""DB-T1 acceptance test 5: the live dashboard's contracts.

- Every stub of `trader/marks/{tap,publisher}.py` and `trader/api/livedata/{periods,books,equity,positions,
  risk,activity,control,health}.py` has the plan's signature (parameter names, kinds and defaults, and whether
  it is a coroutine). The signatures are checked for good: the implementing tasks (DB-T2..DB-T6) keep them.
- A stub raises `NotImplementedError` when called. Once a task implements a function (its body is no longer a
  lone `raise NotImplementedError(...)`), that check is skipped: the task's own tests take over.
- Import direction (plan Global Constraints): `trader.marks` and `trader.api.livedata` import nothing from
  `trader.engine.orchestrator` or `trader.broker.sim_broker`, and from `trader.strategies` only
  `StrategyRegistry`; no decision-path module imports `trader.marks` or `trader.api.livedata`.
"""

import ast
import asyncio
import dataclasses
import importlib
import inspect
import textwrap
from collections.abc import Callable
from pathlib import Path
from typing import Any, NamedTuple

import pytest

from trader.api.livedata import types as lt
from trader.marks import types as mt

TRADER = Path(__file__).resolve().parents[2] / "trader"
EMPTY = inspect.Parameter.empty
PK = inspect.Parameter.POSITIONAL_OR_KEYWORD
KW = inspect.Parameter.KEYWORD_ONLY


class P(NamedTuple):
    name: str
    kind: inspect._ParameterKind = PK
    default: Any = EMPTY


class Stub(NamedTuple):
    module: str
    qualname: str  # "func", "Class.method" or "Class.prop" (a property)
    params: tuple[P, ...]
    is_async: bool = False


SELF = P("self")

STUBS: tuple[Stub, ...] = (
    # trader/marks/tap.py (DB-T2)
    Stub("trader.marks.tap", "QuoteTap.quotes", (SELF, P("ids")), True),
    Stub(
        "trader.marks.tap",
        "QuoteTap.candles",
        (SELF, P("symbol_id"), P("start"), P("end"), P("interval")),
        True,
    ),
    Stub("trader.marks.tap", "QuoteTap.candles_many", (SELF, P("reqs"), P("deadline_s", KW, None)), True),
    Stub("trader.marks.tap", "QuoteTap.stats", (SELF,)),
    Stub("trader.marks.tap", "QuoteTap.drain", (SELF,)),
    Stub("trader.marks.tap", "QuoteTap.health_detail", (SELF,)),
    # trader/marks/publisher.py (DB-T2)
    Stub("trader.marks.publisher", "MarkPublisher.run", (SELF, P("stop")), True),
    Stub("trader.marks.publisher", "MarkPublisher.run_once", (SELF,), True),
    Stub("trader.marks.publisher", "MarkPublisher.health_detail", (SELF,)),
    Stub("trader.marks.publisher", "MarkPublisher.close", (SELF,)),
    # trader/api/livedata/periods.py, books.py, equity.py (DB-T3)
    Stub("trader.api.livedata.periods", "et_day_bounds", (P("d"),)),
    Stub("trader.api.livedata.periods", "session_day", (P("calendar"), P("now"))),
    Stub("trader.api.livedata.periods", "period_windows", (P("calendar"), P("now"), P("run_started_at"))),
    Stub("trader.api.livedata.periods", "claude_spent_between", (P("factory"), P("window"))),
    Stub(
        "trader.api.livedata.periods",
        "period_blocks",
        (P("factory"), P("run_id"), P("windows"), P("open_values")),
    ),
    Stub("trader.api.livedata.periods", "claude_today", (P("factory"), P("now"), P("settings"))),
    Stub("trader.api.livedata.books", "books_check", (P("factory"), P("run_id"))),
    Stub("trader.api.livedata.equity", "downsample", (P("points"), P("max_points", PK, 500))),
    Stub(
        "trader.api.livedata.equity",
        "equity_series",
        (P("factory"), P("calendar"), P("run_id"), P("now"), P("range_"), P("equity_now")),
    ),
    # trader/api/livedata/positions.py, risk.py (DB-T4)
    Stub("trader.api.livedata.positions", "mark_state", (P("observed_at"), P("now"))),
    Stub(
        "trader.api.livedata.positions", "live_positions", (P("factory"), P("run_id"), P("now"), P("expand"))
    ),
    Stub(
        "trader.api.livedata.risk",
        "killswitch_lights",
        (P("killswitches"), P("factory"), P("calendar"), P("settings"), P("run_id"), P("now"), P("equity")),
    ),
    Stub("trader.api.livedata.risk", "trading_state", (P("killswitches"), P("run_id"), P("day"))),
    Stub(
        "trader.api.livedata.risk",
        "risk_panel",
        (
            P("killswitches"),
            P("registry"),
            P("factory"),
            P("calendar"),
            P("settings"),
            P("run_id"),
            P("now"),
            P("positions"),
        ),
    ),
    # trader/api/livedata/activity.py (DB-T5)
    Stub(
        "trader.api.livedata.activity",
        "activity_feed",
        (P("factory"), P("run_id"), P("day"), P("limit", KW, 100)),
    ),
    Stub("trader.api.livedata.activity", "rejections", (P("factory"), P("run_id"), P("day"))),
    # trader/api/livedata/control.py, health.py (DB-T6)
    Stub("trader.api.livedata.control", "engine_card", (P("services"), P("run_id"), P("now"))),
    Stub("trader.api.livedata.control", "strategy_cards", (P("services"), P("run_id"))),
    Stub("trader.api.livedata.control", "schedule", (P("services"), P("now"), P("heartbeat_detail"))),
    Stub("trader.api.livedata.control", "job_summary", (P("job"), P("detail"), P("batch"))),
    Stub("trader.api.livedata.control", "error_log", (P("factory"), P("limit", PK, 200))),
    Stub("trader.api.livedata.health", "health_panel", (P("services"), P("now"))),
    Stub("trader.api.livedata.health", "questrade_stats", (P("detail"), P("now"))),
    Stub("trader.api.livedata.health", "opening_bars", (P("detail"), P("calendar"), P("now"))),
    Stub("trader.api.livedata.health", "soak_summary", (P("services"), P("now"))),
)

# Signature-only: constructors and the routes (their bodies are checked by DB-T2, DB-T5 and DB-T6).
SIGNATURES: tuple[Stub, ...] = (
    Stub("trader.marks.tap", "QuoteTap.__init__", (SELF, P("inner"), P("clock"))),
    Stub(
        "trader.marks.publisher",
        "MarkPublisher.__init__",
        (SELF, P("deps"), P("interval_s", KW, 2.0), P("sleep", KW, asyncio.sleep)),
    ),
    Stub(
        "trader.api.routers.live",
        "get_live",
        (P("services"), P("response"), P("range_", PK, "today"), P("expand", PK, None)),
        True,
    ),
    Stub("trader.api.routers.control", "get_control", (P("services"),), True),
)


def _resolve(stub: Stub) -> tuple[Any, Callable[..., Any]]:
    """(the owning class or None, the function; a property's getter)."""
    obj: Any = importlib.import_module(stub.module)
    owner = None
    for part in stub.qualname.split("."):
        owner = obj if inspect.isclass(obj) else owner
        obj = inspect.getattr_static(obj, part) if inspect.isclass(obj) else getattr(obj, part)
    if isinstance(obj, property):
        assert obj.fget is not None
        return owner, obj.fget
    return owner, obj


def _is_stub(fn: Callable[..., Any]) -> bool:
    """The body (after an optional docstring) is one `raise NotImplementedError(...)`."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    node = tree.body[0]
    assert isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    body = [n for n in node.body if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))]
    if len(body) != 1 or not isinstance(body[0], ast.Raise) or body[0].exc is None:
        return False
    exc = body[0].exc
    target = exc.func if isinstance(exc, ast.Call) else exc
    return isinstance(target, ast.Name) and target.id == "NotImplementedError"


def _instance(owner: type) -> Any:
    """A stub instance built without real dependencies (the stubs only store them)."""
    if owner.__name__ == "MarkPublisher":
        from trader.marks.publisher import MarkPublisherDeps

        deps = MarkPublisherDeps(factory=None, clock=None, tap=None, run_id=lambda: None)  # type: ignore[arg-type]
        return owner(deps)
    return owner(None, None)


@pytest.mark.parametrize(
    "stub", STUBS + SIGNATURES, ids=lambda s: f"{s.module.rsplit('.', 1)[-1]}.{s.qualname}"
)
def test_signature_matches_the_plan(stub: Stub) -> None:
    _, fn = _resolve(stub)
    got = tuple(P(p.name, p.kind, p.default) for p in inspect.signature(fn).parameters.values())
    assert got == stub.params
    assert inspect.iscoroutinefunction(fn) == stub.is_async


@pytest.mark.parametrize("stub", STUBS, ids=lambda s: f"{s.module.rsplit('.', 1)[-1]}.{s.qualname}")
def test_a_stub_raises_not_implemented(stub: Stub) -> None:
    owner, fn = _resolve(stub)
    if not _is_stub(fn):
        pytest.skip(f"{stub.qualname} is implemented (its task's tests cover it)")
    args = [None] * sum(1 for p in stub.params if p.default is EMPTY and p.name != "self")
    with pytest.raises(NotImplementedError):
        if owner is not None:
            instance = _instance(owner)
            if isinstance(inspect.getattr_static(owner, stub.qualname.split(".")[-1]), property):
                getattr(instance, stub.qualname.split(".")[-1])
                return
            result = getattr(instance, fn.__name__)(*args)
        else:
            result = fn(*args)
        if inspect.iscoroutine(result):
            asyncio.run(result)


def test_the_publisher_deps_fields() -> None:
    from trader.marks.publisher import MarkPublisherDeps

    fields = [(f.name, f.default) for f in dataclasses.fields(MarkPublisherDeps)]
    assert fields == [
        ("factory", dataclasses.MISSING),
        ("clock", dataclasses.MISSING),
        ("tap", dataclasses.MISSING),
        ("run_id", dataclasses.MISSING),
        ("event", None),
    ]
    assert MarkPublisherDeps.__dataclass_params__.frozen  # type: ignore[attr-defined]


def test_the_final_constants() -> None:
    assert (mt.PUBLISH_INTERVAL_S, mt.MAX_OBSERVATIONS_PER_SYMBOL, mt.MAX_TAP_SYMBOLS) == (2.0, 30, 1000)
    assert (mt.MAX_CANDLE_BATCHES, mt.MARK_BARS_KEEP_DAYS, mt.PUBLISH_STATEMENT_TIMEOUT_MS) == (5, 10, 5000)
    assert mt.MARKS_SOURCE == "marks"
    assert (lt.MARK_STALE_SECONDS, lt.HEARTBEAT_BADGE_SECONDS, lt.SPARK_POINTS, lt.EQUITY_MAX_POINTS) == (
        30,
        60,
        60,
        500,
    )
    assert (lt.FILL_MARKERS_MAX, lt.ACTIVITY_LIMIT, lt.REJECTION_TICKERS_MAX, lt.EXPAND_MAX) == (
        200,
        100,
        50,
        3,
    )
    assert (lt.ERRORS_SHOWN, lt.SOAK_CACHE_SECONDS, lt.PART_MESSAGE_CHARS, lt.LIVE_MAX_STATEMENTS) == (
        200,
        60.0,
        120,
        80,
    )
    assert str(lt.NEAR_STOP_R) == "0.25"
    for cls in (
        mt.ObservedQuote,
        mt.CandleBatch,
        mt.PublishStep,
        lt.PeriodWindow,
        lt.OpenValue,
        lt.LivePositions,
    ):
        assert cls.__dataclass_params__.frozen and hasattr(cls, "__slots__")  # type: ignore[attr-defined]


# --- import direction ---------------------------------------------------------------------------------------

NEW_PACKAGES = ("marks", "api/livedata")
FORBIDDEN_MODULES = ("trader.engine.orchestrator", "trader.broker.sim_broker")
STRATEGIES_ALLOWED = {"StrategyRegistry"}
DECISION_PATH = (
    "strategies",
    "engine",
    "broker",
    "market",
    "jobs/nightly.py",
    "jobs/premarket.py",
    "adapters/claude/catalyst.py",
    "settings_store.py",
)


def _imports(path: Path) -> list[tuple[str, set[str], int]]:
    """(module, imported names, line) of every import in a file; `import x.y` gives (x.y, {""})."""
    out: list[tuple[str, set[str], int]] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.module:
            out.append((node.module, {a.name for a in node.names}, node.lineno))
        elif isinstance(node, ast.Import):
            out.extend((a.name, {""}, node.lineno) for a in node.names)
    return out


def _files(*roots: str) -> list[Path]:
    files: list[Path] = []
    for root in roots:
        target = TRADER / root
        files.extend([target] if target.suffix == ".py" else sorted(target.rglob("*.py")))
    return files


def test_the_new_packages_import_only_read_only_names_from_the_decision_path() -> None:
    offenders: list[str] = []
    files = _files(*NEW_PACKAGES)
    assert len(files) >= 14, files
    for path in files:
        for module, names, line in _imports(path):
            where = f"{path.relative_to(TRADER)}:{line} {module}"
            if module.startswith(FORBIDDEN_MODULES):
                offenders.append(where)
            if module.startswith("trader.strategies") and not names <= STRATEGIES_ALLOWED:
                offenders.append(f"{where} {sorted(names - STRATEGIES_ALLOWED)}")
    assert not offenders, offenders


def test_no_decision_path_module_imports_the_new_packages() -> None:
    offenders = [
        f"{path.relative_to(TRADER)}:{line} {module}"
        for path in _files(*DECISION_PATH)
        for module, names, line in _imports(path)
        if module.startswith(("trader.marks", "trader.api.livedata"))
        or (module in ("trader", "trader.api") and names & {"marks", "livedata"})
    ]
    assert not offenders, offenders
