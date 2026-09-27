"""Global constraint: only trader/market/clock.py may read the wall clock.

AST-based: every call is resolved through the module's imports (including aliases such as
`from datetime import datetime as dt` or `from time import time as now`) to a fully qualified
name, which is checked against the banned wall-clock readers below. Only CALLS are flagged.
A bare reference such as `wall=time.time` (a default argument in the FinViz scraper, used to
compare against file mtimes and injectable in tests) is allowed on purpose.
"""

import ast
from pathlib import Path

PKG = Path(__file__).resolve().parents[1] / "trader"
ALLOWED = PKG / "market" / "clock.py"

BANNED = {
    "datetime.datetime.now",
    "datetime.datetime.utcnow",
    "datetime.datetime.today",
    "datetime.date.today",
    "time.time",
    "time.time_ns",
    "pandas.Timestamp.now",
    "pandas.Timestamp.utcnow",
    "pandas.Timestamp.today",
}


def _aliases(tree: ast.AST) -> dict[str, str]:
    alias: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.asname:
                    alias[a.asname] = a.name
                else:
                    top = a.name.split(".")[0]
                    alias[top] = top
        elif isinstance(node, ast.ImportFrom) and node.module:
            for a in node.names:
                alias[a.asname or a.name] = f"{node.module}.{a.name}"
    return alias


def _qualified(func: ast.expr, alias: dict[str, str]) -> str | None:
    parts: list[str] = []
    while isinstance(func, ast.Attribute):
        parts.insert(0, func.attr)
        func = func.value
    if not isinstance(func, ast.Name):
        return None
    return ".".join([alias.get(func.id, func.id), *parts])


def wall_clock_calls(src: str) -> list[int]:
    tree = ast.parse(src)
    alias = _aliases(tree)
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _qualified(node.func, alias) in BANNED
    ]


def test_no_direct_wall_clock_reads() -> None:
    offenders = {
        str(p.relative_to(PKG)): lines
        for p in PKG.rglob("*.py")
        if p != ALLOWED and (lines := wall_clock_calls(p.read_text()))
    }
    assert offenders == {}


def test_detector_catches_all_forms() -> None:
    probe = "\n".join(
        [
            "import time",
            "import datetime",
            "import pandas as pd",
            "from datetime import datetime as dt, date",
            "from time import time as now",
            "dt.now()",
            "dt.utcnow()",
            "dt.today()",
            "date.today()",
            "datetime.datetime.now()",
            "pd.Timestamp.now()",
            "pd.Timestamp.today()",
            "time.time()",
            "now()",
        ]
    )
    assert len(wall_clock_calls(probe)) == 9


def test_detector_allows_references_and_non_clock_calls() -> None:
    probe = "\n".join(
        [
            "import time",
            "from datetime import datetime",
            "from sqlalchemy import func",
            "def f(wall=time.time, mono=time.monotonic):",
            "    return wall",
            "datetime.strptime('x', '%Y')",
            "datetime.fromtimestamp(0)",
            "func.now()",
            "time.sleep(1)",
        ]
    )
    assert wall_clock_calls(probe) == []


def test_only_the_exact_clock_module_is_exempt() -> None:
    # The clock module itself does read the wall clock, so the detector must see it there.
    assert wall_clock_calls(ALLOWED.read_text()) != []
