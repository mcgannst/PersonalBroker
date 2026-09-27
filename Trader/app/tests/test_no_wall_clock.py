"""Global constraint: only trader/market/clock.py may read the wall clock."""

import re
from pathlib import Path

PKG = Path(__file__).resolve().parents[1] / "trader"
PATTERN = re.compile(r"datetime\.now\(|date\.today\(|datetime\.utcnow\(")


def test_no_direct_wall_clock_reads() -> None:
    offenders = [
        str(p.relative_to(PKG))
        for p in PKG.rglob("*.py")
        if p.name != "clock.py" and PATTERN.search(p.read_text())
    ]
    assert offenders == []
