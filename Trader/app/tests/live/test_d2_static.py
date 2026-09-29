"""DB-T10 acceptance test 5 (static, D2): the live dashboard cannot reach a trading decision.

- No decision-path module (the `DECISION_PATH` list of tests/decisions/test_static.py) imports `trader.marks`
  or `trader.api.livedata`.
- Only `trader/runtime.py`, `trader/worker.py` and `trader/marks/` import `trader.marks`.
- Allow-list: across `trader/`, the names `QuoteMark`, `MarkBar` (names, attribute access, imported names) and
  the table names `quote_marks`, `mark_bars` (in string constants other than docstrings, i.e. SQL text) appear
  only in `trader/db/models.py`, the 0008 migration, `trader/marks/publisher.py`, `trader/api/feed.py`,
  `trader/api/livedata/positions.py` and `trader/api/livedata/equity.py`; so strategies, the engine, the
  broker, market data, the jobs (the post-close archive included), reports, replay, the decision log and
  notifications can never read a bar built from quotes.
- Those readers never build a `trader.market.types.Candle` and never write the candle tables.
"""

import ast
from pathlib import Path

from tests.decisions.test_static import DECISION_PATH

APP = Path(__file__).resolve().parents[2]
TRADER = APP / "trader"

MARK_NAMES = frozenset({"QuoteMark", "MarkBar"})
MARK_TABLES = ("quote_marks", "mark_bars")
ALLOWED = frozenset(
    {
        "trader/db/models.py",
        "trader/marks/publisher.py",
        "trader/api/feed.py",
        "trader/api/livedata/positions.py",
        "trader/api/livedata/equity.py",
    }
)
ALLOWED_MIGRATION_PREFIX = "trader/db/migrations/versions/0008_"
MARKS_IMPORTERS = frozenset({"trader/runtime.py", "trader/worker.py"})
READERS = ("trader/marks/publisher.py", "trader/api/feed.py", "trader/api/livedata/positions.py",
           "trader/api/livedata/equity.py")  # fmt: skip
CANDLE_TABLE_MODELS = frozenset({"IntradayCandle", "CandleArchive", "DailyCandle"})
WRITE_CALLS = frozenset({"insert", "update", "delete", "merge", "add", "add_all", "bulk_save_objects"})


def _rel(path: Path) -> str:
    return path.relative_to(APP).as_posix()


def _all_modules() -> list[Path]:
    return sorted(p for p in TRADER.rglob("*.py") if "__pycache__" not in p.parts)


def _decision_path_files() -> list[Path]:
    out: list[Path] = []
    for rel in DECISION_PATH:
        target = TRADER / rel
        files = sorted(target.rglob("*.py")) if target.is_dir() else [target]
        assert files and all(f.exists() for f in files), rel
        out.extend(files)
    return out


def imported_modules(tree: ast.AST) -> list[tuple[int, str]]:
    """(line, dotted name) for every import, with `from a import b` also giving `a.b`."""
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend((node.lineno, a.name) for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            out.append((node.lineno, node.module))
            out.extend((node.lineno, f"{node.module}.{a.name}") for a in node.names)
    return out


def _is_under(name: str, package: str) -> bool:
    return name == package or name.startswith(package + ".")


def _docstrings(tree: ast.AST) -> set[int]:
    """ids of the docstring constants of the module, its classes and functions."""
    out: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                if isinstance(body[0].value.value, str):
                    out.add(id(body[0].value))
    return out


def mark_references(source: str) -> list[str]:
    """Every reference to the mark models or tables in a module's code (docstrings and comments excluded)."""
    tree = ast.parse(source)
    docs = _docstrings(tree)
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in MARK_NAMES:
            hits.append(f"{node.lineno}: name {node.id}")
        elif isinstance(node, ast.Attribute) and node.attr in MARK_NAMES:
            hits.append(f"{node.lineno}: attribute {node.attr}")
        elif isinstance(node, ast.ImportFrom):
            hits.extend(f"{node.lineno}: import {a.name}" for a in node.names if a.name in MARK_NAMES)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
            hits.extend(f"{node.lineno}: text {t}" for t in MARK_TABLES if t in node.value)
    return hits


# --- import direction ---------------------------------------------------------------------------------------


def test_no_decision_path_module_imports_the_marks_or_the_live_data() -> None:
    hits: list[str] = []
    for f in _decision_path_files():
        for line, name in imported_modules(ast.parse(f.read_text(encoding="utf-8"))):
            if _is_under(name, "trader.marks") or _is_under(name, "trader.api.livedata"):
                hits.append(f"{_rel(f)}:{line} {name}")
    assert hits == []


def test_only_the_worker_composition_imports_the_marks() -> None:
    hits: list[str] = []
    importers: set[str] = set()
    for f in _all_modules():
        rel = _rel(f)
        if rel.startswith("trader/marks/"):
            continue
        for line, name in imported_modules(ast.parse(f.read_text(encoding="utf-8"))):
            if _is_under(name, "trader.marks"):
                importers.add(rel)
                if rel not in MARKS_IMPORTERS:
                    hits.append(f"{rel}:{line} {name}")
    assert hits == []
    assert importers == MARKS_IMPORTERS  # the wiring really is there


# --- the reader allow-list ----------------------------------------------------------------------------------


def test_the_mark_tables_are_referenced_only_by_the_allow_list() -> None:
    offenders: dict[str, list[str]] = {}
    referencing: set[str] = set()
    for f in _all_modules():
        rel = _rel(f)
        hits = mark_references(f.read_text(encoding="utf-8"))
        if not hits:
            continue
        referencing.add(rel)
        if rel not in ALLOWED and not rel.startswith(ALLOWED_MIGRATION_PREFIX):
            offenders[rel] = hits
    assert offenders == {}
    # every allowed reader really references them (the scan sees what it should)
    assert ALLOWED <= referencing
    assert any(r.startswith(ALLOWED_MIGRATION_PREFIX) for r in referencing)


def test_the_scanner_sees_code_and_skips_docstrings() -> None:
    source = '''
"""Module docstring about quote_marks and mark_bars."""
from trader.db.models import MarkBar
import trader.db.models as m

SQL = "SELECT close FROM trader.mark_bars"


def f():
    """Reads quote_marks."""
    # a comment about mark_bars
    return m.QuoteMark, MarkBar
'''
    hits = sorted(h.split(": ", 1)[1] for h in mark_references(source))
    assert hits == ["attribute QuoteMark", "import MarkBar", "name MarkBar", "text mark_bars"]
    assert mark_references('"""only quote_marks in a docstring"""\n') == []


# --- no candle is ever built from a mark bar ----------------------------------------------------------------


def candle_offences(source: str) -> list[str]:
    """A `trader.market.types.Candle` imported or constructed, or a write naming a candle table model."""
    tree = ast.parse(source)
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("trader.market"):
            out.extend(f"{node.lineno}: import Candle" for a in node.names if a.name == "Candle")
        elif isinstance(node, ast.Call):
            func = node.func
            name = (
                func.id
                if isinstance(func, ast.Name)
                else func.attr
                if isinstance(func, ast.Attribute)
                else ""
            )
            if name == "Candle":
                out.append(f"{node.lineno}: Candle(...)")
            elif name in WRITE_CALLS:
                for arg in ast.walk(node):
                    ref = (
                        arg.id
                        if isinstance(arg, ast.Name)
                        else arg.attr
                        if isinstance(arg, ast.Attribute)
                        else ""
                    )
                    if ref in CANDLE_TABLE_MODELS:
                        out.append(f"{node.lineno}: {name}({ref})")
    return out


def test_the_readers_never_build_a_candle_nor_write_the_candle_tables() -> None:
    offenders = {
        rel: hits for rel in READERS if (hits := candle_offences((APP / rel).read_text(encoding="utf-8")))
    }
    assert offenders == {}


def test_the_candle_scanner_sees_an_offence() -> None:
    source = """
from trader.market.types import Candle
from sqlalchemy.dialects.postgresql import insert
from trader.db import models as m

def f(bar):
    s.execute(insert(m.IntradayCandle).values(close=bar.close))
    return Candle(bar.minute_start, bar.minute_start, bar.open, bar.high, bar.low, bar.close, 0, None)
"""
    assert sorted(h.split(": ", 1)[1] for h in candle_offences(source)) == [
        "Candle(...)",
        "import Candle",
        "insert(IntradayCandle)",
    ]
