"""The day summary of a decision journal (P6-T10).

`summarize` counts a day's rows into a `DaySummary`; `summary_text` renders it in at most 3 lines and 400
characters, e.g. `812 scanned · 14 ranked · 1 passed · 1 proposal (1 manual, median 42 s) · 2 fills (avg +0.02
vs planned) · 1 trade 1W/0L P&L +12.50 (+0.80R) · exits flatten 1` / `top rejects: rvol_below_min 790, ...`.
No time is shown, so there is no time zone to convert.

The `day` row stores the summary as `summary_to_json(s)` (all `DaySummary` fields; decimals as strings, rule
counts as `[[rule, count], ...]`, the date ISO) plus `text` (`summary_text(link=None)`) and `fingerprint`;
`summary_from_json` reads it back (P6-T12's `load_day`).
"""

import statistics
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from trader.decisions.orb_explain import dec
from trader.decisions.types import DaySummary, DecisionRowView

MAX_TEXT_CHARS = 400
MAX_LINES = 3
Q4 = Decimal("0.0001")
APPROVAL_KEYS = ("manual", "auto", "declined", "expired", "blocked")
_APPROVAL_OF = {
    "approved": "manual",
    "auto_approved": "auto",
    "declined": "declined",
    "expired": "expired",
    "blocked": "blocked",
}
SCAN_COUNT_KEYS = ("scanned", "rvol_passed", "ranked", "passed")
SUMMARY_NOTE = "summary_note"  # a row's data key whose text becomes one of the day's notes


def _ranked_counts(counts: Iterable[tuple[str, int]]) -> tuple[tuple[str, int], ...]:
    """Rule counts, highest first, then by rule name (a stable order)."""
    return tuple(sorted(((r, n) for r, n in counts if n > 0), key=lambda x: (-x[1], x[0])))


def _int(v: Any) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) else 0


def summarize(rows: Sequence[DecisionRowView], *, run_id: int, session_date: date, final: bool) -> DaySummary:
    universe_size: int | None = None
    universe_source: str | None = None
    listed = classified = 0
    scan_counts: dict[str, int] | None = None
    scan_rejects: Counter[str] = Counter()
    row_scan: Counter[str] = Counter()
    row_rejects: Counter[str] = Counter()
    signals = proposals = fills = trades = wins = losses = 0
    risk: Counter[str] = Counter()
    approvals = dict.fromkeys(APPROVAL_KEYS, 0)
    latencies: list[float] = []
    diffs: list[Decimal] = []
    pnl = Decimal(0)
    pnl_r: Decimal | None = None
    exits: Counter[str] = Counter()
    notes: list[str] = []
    for r in rows:
        d = r.data
        note = d.get(SUMMARY_NOTE)
        if isinstance(note, str) and note and r.symbol_id is None:
            notes.append(note)
        if r.stage == "universe":
            size = d.get("size")
            universe_size = size if isinstance(size, int) else universe_size
            universe_source = d.get("source") if isinstance(d.get("source"), str) else universe_source
        elif r.stage == "premarket" and r.symbol_id is not None:
            listed += 1
            classified += r.outcome == "classified"
        elif r.stage == "scan":
            if r.symbol_id is None:
                counts = d.get("counts")
                if isinstance(counts, Mapping):
                    scan_counts = scan_counts or dict.fromkeys(SCAN_COUNT_KEYS, 0)
                    for k in SCAN_COUNT_KEYS:
                        scan_counts[k] += _int(counts.get(k))
                    rejects = counts.get("rejects_by_rule")
                    if isinstance(rejects, Mapping):
                        scan_rejects.update({str(k): _int(v) for k, v in rejects.items()})
            else:
                row_scan["scanned"] += 1
                if r.outcome == "passed":
                    row_scan["passed"] += 1
                if d.get("rank") is not None and r.rule != "outside_top_n":
                    row_scan["ranked"] += 1
                if r.rule not in ("no_opening_bar", "no_baseline", "rvol_below_min"):
                    row_scan["rvol_passed"] += 1
                if r.outcome == "rejected" and r.rule:
                    row_rejects[r.rule] += 1
        elif r.stage == "signal":
            signals += 1
        elif r.stage == "risk":
            risk[r.rule or "unknown"] += 1
        elif r.stage == "proposal":
            proposals += 1
        elif r.stage == "approval":
            key = _APPROVAL_OF.get(r.outcome)
            if key is not None:
                approvals[key] += 1
            ms = d.get("decision_latency_ms")
            if r.outcome in ("approved", "declined") and isinstance(ms, int) and not isinstance(ms, bool):
                latencies.append(ms / 1000)
        elif r.stage == "fill":
            fills += 1
            diff = dec(d.get("diff_per_share"))
            if diff is not None:
                diffs.append(diff)
        elif r.stage == "exit":
            trades += 1
            p = dec(d.get("pnl")) or Decimal(0)
            pnl += p
            wins += p > 0
            losses += p < 0
            pr = dec(d.get("pnl_r"))
            if pr is not None:
                pnl_r = (pnl_r or Decimal(0)) + pr
            exits[r.rule or "other"] += 1
    counts = scan_counts if scan_counts is not None else {k: row_scan[k] for k in SCAN_COUNT_KEYS}
    rejects = scan_rejects if scan_counts is not None else row_rejects
    avg_diff = (sum(diffs, Decimal(0)) / len(diffs)).quantize(Q4, ROUND_HALF_UP) if diffs else None
    return DaySummary(
        run_id=run_id,
        session_date=session_date,
        final=final,
        universe_size=universe_size,
        universe_source=universe_source,
        premarket_listed=listed,
        premarket_classified=classified,
        scanned=counts["scanned"],
        rvol_passed=counts["rvol_passed"],
        ranked=counts["ranked"],
        passed=counts["passed"],
        rejects_by_rule=_ranked_counts(rejects.items()),
        signals=signals,
        risk_rejections=_ranked_counts(risk.items()),
        proposals=proposals,
        approvals=approvals,
        median_decision_seconds=statistics.median(latencies) if latencies else None,
        fills=fills,
        avg_fill_diff_per_share=avg_diff,
        trades=trades,
        wins=wins,
        losses=losses,
        pnl=pnl,
        pnl_r=pnl_r,
        exits_by_reason=_ranked_counts(exits.items()),
        notes=tuple(notes),
    )


# --- text ---------------------------------------------------------------------------------------------------
def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _signed(d: Decimal, places: int = 2) -> str:
    return f"{d.quantize(Decimal(1).scaleb(-places), ROUND_HALF_UP):+}"


def _diff(d: Decimal) -> str:
    s = f"{d.quantize(Q4, ROUND_HALF_UP):+}"
    return s.rstrip("0").rstrip(".") if "." in s else s


def summary_text(s: DaySummary, *, link: str | None) -> str:
    """At most 3 lines and 400 characters: the scan, then what traded, then the top reject rules (and the
    link, when given)."""
    if s.scanned or s.ranked:
        line1 = f"{s.scanned} scanned · {s.ranked} ranked · {s.passed} passed"
    else:
        line1 = "no 9:35 scan recorded"
    if s.universe_size is not None:
        line1 = f"universe {s.universe_size} · " + line1
    parts: list[str] = []
    if s.proposals:
        decided = [f"{s.approvals.get(k, 0)} {k}" for k in APPROVAL_KEYS if s.approvals.get(k, 0)]
        if s.median_decision_seconds is not None:
            decided.append(f"median {s.median_decision_seconds:.0f} s")
        parts.append(_plural(s.proposals, "proposal") + (f" ({', '.join(decided)})" if decided else ""))
    else:
        parts.append("no proposals")
    if s.risk_rejections:
        parts.append("risk rejected " + ", ".join(f"{r} {n}" for r, n in s.risk_rejections))
    if s.fills:
        extra = (
            f" (avg {_diff(s.avg_fill_diff_per_share)} vs planned)"
            if s.avg_fill_diff_per_share is not None
            else ""
        )
        parts.append(_plural(s.fills, "fill") + extra)
    if s.trades:
        r = f" ({_signed(s.pnl_r)}R)" if s.pnl_r is not None else ""
        parts.append(f"{_plural(s.trades, 'trade')} {s.wins}W/{s.losses}L P&L {_signed(s.pnl)}{r}")
        parts.append("exits " + ", ".join(f"{k} {n}" for k, n in s.exits_by_reason))
    line2 = " · ".join(parts)
    tail: list[str] = []
    if s.rejects_by_rule:
        tail.append("top rejects: " + ", ".join(f"{r} {n}" for r, n in s.rejects_by_rule[:3]))
    if s.notes:
        tail.append(s.notes[0])
    if link:
        tail.append(link)
    lines = [line1, line2] + ([" · ".join(tail)] if tail else [])
    text = "\n".join(lines[:MAX_LINES])
    if len(text) > MAX_TEXT_CHARS:
        if link and link in text:  # keep the link whole: clip what comes before it
            head = text[: -len(link)].rstrip(" ·")
            room = MAX_TEXT_CHARS - len(link) - 4
            text = head[: max(room, 0)].rstrip() + "… · " + link
            if len(text) > MAX_TEXT_CHARS:
                text = text[:MAX_TEXT_CHARS]
        else:
            text = text[: MAX_TEXT_CHARS - 1].rstrip() + "…"
    return text


# --- the day row's JSON -------------------------------------------------------------------------------------
def _pairs(v: tuple[tuple[str, int], ...]) -> list[list[Any]]:
    return [[r, n] for r, n in v]


def summary_to_json(s: DaySummary) -> dict[str, Any]:
    return {
        "run_id": s.run_id,
        "session_date": s.session_date.isoformat(),
        "final": s.final,
        "universe_size": s.universe_size,
        "universe_source": s.universe_source,
        "premarket_listed": s.premarket_listed,
        "premarket_classified": s.premarket_classified,
        "scanned": s.scanned,
        "rvol_passed": s.rvol_passed,
        "ranked": s.ranked,
        "passed": s.passed,
        "rejects_by_rule": _pairs(s.rejects_by_rule),
        "signals": s.signals,
        "risk_rejections": _pairs(s.risk_rejections),
        "proposals": s.proposals,
        "approvals": dict(s.approvals),
        "median_decision_seconds": s.median_decision_seconds,
        "fills": s.fills,
        "avg_fill_diff_per_share": None
        if s.avg_fill_diff_per_share is None
        else str(s.avg_fill_diff_per_share),
        "trades": s.trades,
        "wins": s.wins,
        "losses": s.losses,
        "pnl": str(s.pnl),
        "pnl_r": None if s.pnl_r is None else str(s.pnl_r),
        "exits_by_reason": _pairs(s.exits_by_reason),
        "notes": list(s.notes),
    }


def _pairs_from(v: Any) -> tuple[tuple[str, int], ...]:
    out: list[tuple[str, int]] = []
    for item in v or ():
        if isinstance(item, list | tuple) and len(item) == 2:
            out.append((str(item[0]), _int(item[1])))
    return tuple(out)


def summary_from_json(d: Mapping[str, Any]) -> DaySummary:
    """The `DaySummary` stored on a `day` row (`summary_to_json`)."""
    median = d.get("median_decision_seconds")
    return DaySummary(
        run_id=_int(d.get("run_id")),
        session_date=date.fromisoformat(str(d["session_date"])),
        final=bool(d.get("final")),
        universe_size=d.get("universe_size") if isinstance(d.get("universe_size"), int) else None,
        universe_source=d.get("universe_source") if isinstance(d.get("universe_source"), str) else None,
        premarket_listed=_int(d.get("premarket_listed")),
        premarket_classified=_int(d.get("premarket_classified")),
        scanned=_int(d.get("scanned")),
        rvol_passed=_int(d.get("rvol_passed")),
        ranked=_int(d.get("ranked")),
        passed=_int(d.get("passed")),
        rejects_by_rule=_pairs_from(d.get("rejects_by_rule")),
        signals=_int(d.get("signals")),
        risk_rejections=_pairs_from(d.get("risk_rejections")),
        proposals=_int(d.get("proposals")),
        approvals={k: _int((d.get("approvals") or {}).get(k)) for k in APPROVAL_KEYS},
        median_decision_seconds=median
        if isinstance(median, int | float) and not isinstance(median, bool)
        else None,
        fills=_int(d.get("fills")),
        avg_fill_diff_per_share=dec(d.get("avg_fill_diff_per_share")),
        trades=_int(d.get("trades")),
        wins=_int(d.get("wins")),
        losses=_int(d.get("losses")),
        pnl=dec(d.get("pnl")) or Decimal(0),
        pnl_r=dec(d.get("pnl_r")),
        exits_by_reason=_pairs_from(d.get("exits_by_reason")),
        notes=tuple(str(n) for n in d.get("notes") or ()),
    )
