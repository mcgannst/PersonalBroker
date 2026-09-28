"""P6-T6: tests of the promotion tools in Trader/build (loaded by path; no network, no real database)."""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

BUILD_DIR = Path(__file__).resolve().parents[3] / "build"


def load_build_script(name: str) -> ModuleType:
    """Import Trader/build/<name>.py by path under a private module name (registered in sys.modules, which
    dataclasses need while the module executes)."""
    module_name = f"build_{name}_under_test"
    spec = importlib.util.spec_from_file_location(module_name, BUILD_DIR / f"{name}.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module
