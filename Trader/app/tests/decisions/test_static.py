"""P6-T10 acceptance test 15 (static): no decision-path module imports `trader.decisions`; `trader/decisions/`
imports no trading, messaging or network module, never reads the wall clock, and writes only `DecisionLog`
(insert and delete). Also pins the constants the recorder copies instead of importing."""

import ast
from pathlib import Path

from trader.adapters.claude import catalyst
from trader.decisions import recorder
from trader.engine import proposals

APP = Path(__file__).resolve().parents[2]
TRADER = APP / "trader"
DECISIONS = TRADER / "decisions"

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
FORBIDDEN_MODULES = (
    "trader.engine.proposals",
    "trader.engine.orchestrator",
    "trader.broker.sim_broker",
    "trader.jobs.runner",
    "trader.notify",
    "trader.adapters.telegram",
    "trader.adapters.questrade.client",
    "trader.adapters.questrade.auth",
    "trader.adapters.finviz.scraper",
    "trader.runtime",
    "anthropic",
    "httpx",
    "telegram",
)
FORBIDDEN_NAMES = frozenset(
    {"ProposalService", "SimBroker", "Engine", "run_job", "run_job_async", "log_event"}
)
SESSION_NAMES = frozenset({"s", "session", "sess", "self.s"})


def _files(rel: str) -> list[Path]:
    p = TRADER / rel
    return sorted(p.rglob("*.py")) if p.is_dir() else [p]


def _imports(tree: ast.AST) -> list[tuple[int, str, list[str]]]:
    out: list[tuple[int, str, list[str]]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend((node.lineno, a.name, []) for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            out.append((node.lineno, node.module, [a.name for a in node.names]))
    return out


def test_15_no_decision_path_module_imports_the_decision_log() -> None:
    hits: list[str] = []
    for rel in DECISION_PATH:
        files = _files(rel)
        assert files, rel
        for f in files:
            for line, mod, names in _imports(ast.parse(f.read_text())):
                full = [mod, *(f"{mod}.{n}" for n in names)]
                if any(x == "trader.decisions" or x.startswith("trader.decisions.") for x in full):
                    hits.append(f"{f.relative_to(APP)}:{line} {mod}")
    assert hits == []


def scan_decisions_source(source: str, path: str = "<source>") -> list[str]:
    tree = ast.parse(source)
    out: list[str] = []
    for line, mod, names in _imports(tree):
        if any(mod == f or mod.startswith(f + ".") for f in FORBIDDEN_MODULES):
            out.append(f"{path}:{line} import {mod}")
        for n in names:
            if n in FORBIDDEN_NAMES or (mod == "sqlalchemy" and n == "update"):
                out.append(f"{path}:{line} import {n}")
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            out.append(f"{path}:{node.lineno} {node.id}")
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute):
            target = ast.unparse(func.value)
            if func.attr in ("now", "today", "utcnow") and target in ("datetime", "date"):
                out.append(f"{path}:{node.lineno} {target}.{func.attr}()")
            if func.attr in ("add", "add_all", "merge", "bulk_save_objects") and target in SESSION_NAMES:
                out.append(f"{path}:{node.lineno} {target}.{func.attr}()")
            if func.attr == "execute" and node.args and isinstance(node.args[0], ast.Call):
                inner = node.args[0]
                if isinstance(inner.func, ast.Name) and inner.func.id in ("insert", "delete", "update"):
                    what = ast.unparse(inner.args[0]) if inner.args else "?"
                    if what != "m.DecisionLog":
                        out.append(f"{path}:{node.lineno} {inner.func.id}({what})")
        elif isinstance(func, ast.Name):
            if func.id in ("insert", "delete", "update") and node.args:
                what = ast.unparse(node.args[0])
                if what != "m.DecisionLog":
                    out.append(f"{path}:{node.lineno} {func.id}({what})")
            if func.id == "text" and node.args and isinstance(node.args[0], ast.Constant):
                sql = str(node.args[0].value).lstrip().upper()
                if not sql.startswith("SELECT"):
                    out.append(f"{path}:{node.lineno} raw SQL {sql[:30]}")
    return out


def test_15_the_decision_package_reads_only_and_writes_only_decision_log() -> None:
    findings: list[str] = []
    files = sorted(DECISIONS.rglob("*.py"))
    assert {"recorder.py", "orb_explain.py", "summary.py", "prune.py"} <= {f.name for f in files}
    for f in files:
        findings.extend(scan_decisions_source(f.read_text(), str(f.relative_to(APP))))
    assert findings == []


def test_15_the_scanner_catches_each_violation() -> None:
    planted = "\n".join(
        [
            "from trader.engine.proposals import ProposalService",
            "from trader.broker.sim_broker import SimBroker",
            "import httpx",
            "import anthropic",
            "from trader.notify.notifier import TelegramNotifier",
            "from trader.adapters.questrade.client import QuestradeClient",
            "x = datetime.now()",
            "y = date.today()",
            "s.add(m.Trade())",
            "s.execute(insert(m.Order))",
            "delete(m.Trade)",
            "from sqlalchemy import update",
            "text('UPDATE trader.orders SET qty = 1')",
            "from trader.jobs.runner import run_job",
        ]
    )
    found = {int(f.split(":")[1].split()[0]) for f in scan_decisions_source(planted)}
    assert found == set(range(1, 15))
    ok = "\n".join(
        [
            "s.execute(delete(m.DecisionLog).where(m.DecisionLog.run_id == 1))",
            "s.execute(insert(m.DecisionLog), [])",
            "text('SELECT pg_advisory_xact_lock(hashtext(:k))')",
            "counts.update(x=1)",
            "seen.add(3)",
            "now = deps.clock.now()",
        ]
    )
    assert scan_decisions_source(ok) == []


def test_the_copied_constants_equal_their_sources() -> None:
    assert recorder.OVER_CAP_PREFIX == catalyst.OVER_CAP
    assert recorder.BUDGET_PREFIX == catalyst.BUDGET_EXCEEDED
    assert recorder.AUTO_FLATTEN_ACTOR == proposals.AUTO_FLATTEN_ACTOR
