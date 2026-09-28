"""P5-T18 acceptance test 4: replay isolation and determinism, statically (an AST scan of `trader/replay/`).

No module under `trader/replay/` imports `anthropic`, the Telegram adapters, the notifier or the relay, or
references `CatalystClassifier`, `CatalystService`, `fire_event`, `run_job` or `run_job_async`; none calls
`.set(` on a settings store, `.update(` on a strategy registry or `ensure_defaults(`. For determinism none
imports `random`, `uuid`, `secrets` or `time` and none calls `float(`. A fresh interpreter importing the whole
package loads none of the forbidden modules either (transitively).
"""

import ast
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[2]
REPLAY_DIR = APP_DIR / "trader" / "replay"

FORBIDDEN_IMPORTS = (
    "anthropic",
    "trader.adapters.telegram",
    "trader.notify.notifier",
    "trader.notify.relay",
    "random",
    "uuid",
    "secrets",
    "time",
)
FORBIDDEN_NAMES = frozenset(
    {"CatalystClassifier", "CatalystService", "fire_event", "run_job", "run_job_async", "ensure_defaults"}
)
# Objects whose `.set(` / `.update(` would change live state: a settings store or a strategy registry.
SETTINGS_HINTS = ("setting", "store")
REGISTRY_HINTS = ("registry",)
TRANSITIVE = ("anthropic", "trader.adapters.telegram", "trader.notify.notifier", "trader.notify.relay")


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    what: str


def _forbidden_module(name: str) -> bool:
    return any(name == f or name.startswith(f + ".") for f in FORBIDDEN_IMPORTS)


def scan_source(source: str, path: str = "<source>") -> list[Finding]:
    """Every isolation or determinism violation in one module's source."""
    tree = ast.parse(source)
    out: list[Finding] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _forbidden_module(alias.name):
                    out.append(Finding(path, node.lineno, f"import {alias.name}"))
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if node.level == 0 and _forbidden_module(mod):
                out.append(Finding(path, node.lineno, f"from {mod} import ..."))
            for alias in node.names:
                if alias.name in FORBIDDEN_NAMES:
                    out.append(Finding(path, node.lineno, f"from {mod} import {alias.name}"))
                if node.level == 0 and _forbidden_module(f"{mod}.{alias.name}"):
                    out.append(Finding(path, node.lineno, f"from {mod} import {alias.name}"))
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            out.append(Finding(path, node.lineno, f"reference to {node.id}"))
        elif isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_NAMES:
            if not isinstance(node.ctx, ast.Store):
                out.append(Finding(path, node.lineno, f"reference to .{node.attr}"))
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == "float":
                out.append(Finding(path, node.lineno, "float( call"))
            if isinstance(func, ast.Attribute):
                target = ast.unparse(func.value).lower()
                if func.attr == "set" and any(h in target for h in SETTINGS_HINTS):
                    out.append(Finding(path, node.lineno, f"{ast.unparse(func)}( on a settings store"))
                if func.attr == "update" and any(h in target for h in REGISTRY_HINTS):
                    out.append(Finding(path, node.lineno, f"{ast.unparse(func)}( on a strategy registry"))
    return out


def replay_modules() -> list[Path]:
    return sorted(REPLAY_DIR.rglob("*.py"))


def test_the_scan_sees_every_replay_module() -> None:
    names = {p.name for p in replay_modules()}
    assert {"runner.py", "data.py", "setup.py", "clock.py", "candle_fill_model.py", "catalysts.py"} <= names


def test_4_no_replay_module_breaks_isolation_or_determinism() -> None:
    findings: list[Finding] = []
    for path in replay_modules():
        findings.extend(scan_source(path.read_text(), str(path.relative_to(APP_DIR))))
    assert findings == []


def test_the_scan_catches_each_kind_of_violation() -> None:
    """The scanner is not vacuous: each planted violation is reported."""
    planted = "\n".join(
        [
            "import anthropic",
            "import random",
            "from uuid import uuid4",
            "import secrets",
            "import time",
            "from trader.adapters.telegram.api import PtbTelegramApi",
            "from trader.notify import relay",
            "from trader.notify.notifier import TelegramNotifier",
            "from trader.engine.scheduler import fire_event",
            "from trader.jobs.runner import run_job, run_job_async",
            "from trader.adapters.claude.catalyst import CatalystClassifier",
            "x = CatalystService",
            "settings_store.set('risk_pct', 1, 'a')",
            "self.settings.set('approval_mode', 'auto', 'a')",
            "registry.update('orb_sip', params={}, actor='a')",
            "registry.ensure_defaults()",
            "price = float(bar.close)",
        ]
    )
    found = {f.line for f in scan_source(planted)}
    assert found == set(range(1, 18))


def test_the_scan_allows_the_legitimate_patterns() -> None:
    """The replay clock's `.set(t)`, a dict's `.update(...)`, a refusing `def set`/`def ensure_defaults`
    override and `from datetime import time` are not violations."""
    ok = "\n".join(
        [
            "from datetime import time",
            "self.clock.set(t)",
            "found.update({})",
            "class S:",
            "    def set(self, key, value, actor):",
            "        raise RuntimeError('read-only')",
            "    def ensure_defaults(self, actor='system'):",
            "        raise RuntimeError('pinned')",
        ]
    )
    assert scan_source(ok) == []


def test_importing_the_replay_package_loads_no_forbidden_module() -> None:
    """Transitively too: a fresh interpreter that imports every replay module never loads anthropic, the
    Telegram adapters, the notifier or the relay."""
    code = (
        "import sys, importlib, pkgutil, trader.replay as r\n"
        "for mod in pkgutil.iter_modules(r.__path__):\n"
        "    importlib.import_module('trader.replay.' + mod.name)\n"
        f"bad = sorted(k for k in sys.modules if k.startswith({TRANSITIVE!r}))\n"
        "print(','.join(bad))\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", code], cwd=APP_DIR, capture_output=True, text=True, timeout=120, check=True
    )
    assert done.stdout.strip() == ""
