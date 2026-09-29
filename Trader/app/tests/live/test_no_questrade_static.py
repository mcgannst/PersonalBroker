"""DB-T4 acceptance test 10: the dashboard never calls Questrade (live dashboard design D8, plan Global
Constraints).

An AST check over every module of `trader/api/livedata/` and the two routes `routers/live.py` and
`routers/control.py`: none references the `quotes`/`candles` market-data methods, the Questrade-backed helpers
(`open_positions`, `last_prices`, `position_lines`), the market-data and Questrade client classes
(`OffLoopMarketData`, `MarketDataService`, `LazyQuestrade`, `QuestradeClient`, `questrade_client`), the
modules `trader.market.data_service` and `trader.adapters.questrade.client`, or imports `httpx`. It covers all
the files from the start (DB-T5 and DB-T6 never edit it); the stubs pass trivially.

Local variables and response fields named like a helper (e.g. `RiskOut.open_positions`, a count) are not
calls, so `open_positions` is refused where it can reach the helper: imported by name, or through an import
of the module that defines it (`trader.api.routers.trading` as a module, `trader.notify.views` as a module).
"""

import ast
from pathlib import Path

import pytest

TRADER = Path(__file__).resolve().parents[2] / "trader"
LIVEDATA = TRADER / "api" / "livedata"
ROUTES = (TRADER / "api" / "routers" / "live.py", TRADER / "api" / "routers" / "control.py")
LIVEDATA_MODULES = ("periods", "books", "equity", "positions", "risk", "activity", "control", "health")

FORBIDDEN_ATTRIBUTES = frozenset(
    {
        "quotes",
        "candles",
        "candles_many",
        "last_prices",
        "position_lines",
        "OffLoopMarketData",
        "MarketDataService",
        "LazyQuestrade",
        "QuestradeClient",
        "questrade_client",
    }
)
FORBIDDEN_NAMES = FORBIDDEN_ATTRIBUTES - {"quotes", "candles", "candles_many"} | {"open_positions"}
FORBIDDEN_CLASS_NAMES = frozenset(
    {"OffLoopMarketData", "MarketDataService", "LazyQuestrade", "QuestradeClient"}
)
FORBIDDEN_MODULES = ("trader.market.data_service", "trader.adapters.questrade.client", "httpx")
# importing one of these modules whole would let `module.open_positions` / `module.last_prices` through
HELPER_MODULES = frozenset({"trader.api.routers.trading", "trader.notify.views"})
HELPER_PACKAGES = {"trader.api.routers": "trading", "trader.notify": "views"}


def _is_forbidden_module(name: str) -> bool:
    return any(name == mod or name.startswith(mod + ".") for mod in FORBIDDEN_MODULES)


def offences(source: str) -> list[str]:
    """Every forbidden reference in a module's source, as `line: what`."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        line = getattr(node, "lineno", 0)
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_forbidden_module(alias.name) or alias.name in HELPER_MODULES:
                    found.append(f"{line}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if _is_forbidden_module(module):
                found.append(f"{line}: from {module} import ...")
            for alias in node.names:
                if alias.name in FORBIDDEN_NAMES:
                    found.append(f"{line}: from {module} import {alias.name}")
                if HELPER_PACKAGES.get(module) == alias.name:
                    found.append(f"{line}: from {module} import {alias.name} (the module whole)")
                if alias.name == "*" and module in HELPER_MODULES:
                    found.append(f"{line}: from {module} import *")
        elif isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_ATTRIBUTES:
            found.append(f"{line}: .{node.attr}")
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_CLASS_NAMES:
            found.append(f"{line}: {node.id}")
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and _is_forbidden_module(node.value)
        ):
            found.append(f"{line}: {node.value!r}")
    return found


def _files() -> list[Path]:
    return sorted(LIVEDATA.glob("*.py")) + list(ROUTES)


def test_it_covers_every_livedata_module_and_both_routes() -> None:
    names = {p.stem for p in LIVEDATA.glob("*.py")}
    assert set(LIVEDATA_MODULES) <= names
    for route in ROUTES:
        assert route.is_file(), route
    assert len(_files()) >= len(LIVEDATA_MODULES) + len(ROUTES)


def test_no_live_data_module_or_route_can_reach_questrade() -> None:
    offenders = {
        str(path.relative_to(TRADER)): found
        for path in _files()
        if (found := offences(path.read_text(encoding="utf-8")))
    }
    assert not offenders, offenders


@pytest.mark.parametrize(
    "snippet",
    [
        "x = services.quotes",
        "await services.candles(1, a, b, '1m')",
        "services.market.candles_many([])",
        "from trader.api.routers.trading import open_positions",
        "from trader.api.routers import trading",
        "import trader.api.routers.trading",
        "from trader.notify.views import last_prices",
        "from trader.notify.views import position_lines",
        "from trader.notify import views",
        "import trader.notify.views as v",
        "from trader.market.data_service import OffLoopMarketData",
        "x = MarketDataService(a, b)",
        "x: LazyQuestrade | None = None",
        "from trader.adapters.questrade.client import QuestradeClient",
        "import trader.adapters.questrade.client",
        "c = deps.questrade_client",
        "import httpx",
        "from httpx import AsyncClient",
        "importlib.import_module('trader.market.data_service')",
    ],
)
def test_the_check_catches_each_forbidden_reference(snippet: str) -> None:
    assert offences(snippet), snippet


@pytest.mark.parametrize(
    "snippet",
    [
        "from trader.notify.views import dec, lines_from, open_position_rows",
        "from trader.api.routers.trading import strategy_keys",
        "from trader.api.views import killswitch_states",
        "open_positions = len(rows)",
        "RiskOut(open_positions=3)",
        "n = panel.open_positions",
        "select(m.IntradayCandle, m.CandleArchive, m.MarkBar, m.QuoteMark)",
    ],
)
def test_the_check_allows_the_read_only_helpers_and_plain_names(snippet: str) -> None:
    assert offences(snippet) == [], snippet
