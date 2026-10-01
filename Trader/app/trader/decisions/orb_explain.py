"""Every `orb_sip` filter rule of a scan candidate with its value and threshold (P6-T10).

`explain_orb` evaluates each rule independently, in the strategy's order, with the parameters in effect at
9:35; `first_failure` maps the first failing check to the strategy's reject reason, so it equals
`candidates.reject_reason` (None for a passed candidate). It never imports `OrbSip` itself: the maths comes
from `trader.market.indicators`, the same functions the strategy calls.

Three checks depend on the engine's state at 9:35, which no row stores (open positions, working entries and
the slots left): `not_held`, `no_working_entry` and `slot_left` are taken from the candidate's stored
`reject_reason` (`already_held`, `entry_working`, `lower_rank` fail them; any later rule, or a pass, passes
them), with value and threshold None (`CONTEXT_CHECKS`; the recorder notes the source in the row's data).
Every other check is recomputed from the stored values. The catalyst checks exist only when the strategy
requires a catalyst (with `reject_bearish_catalyst` alone, only `catalyst_not_bearish`); the strategy
evaluates them only for names that survived the price, ATR, volume and stop rules, so for an earlier failure
they are shown but can't change `first_failure`.
"""

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from trader.decisions.types import Check, CheckOp
from trader.market.indicators import is_bearish, is_doji, rvol
from trader.market.types import Candle
from trader.strategies.orb_sip import NO_CATALYST, OrbSipParams

Q4 = Decimal("0.0001")
CONTEXT_CHECKS = frozenset({"not_held", "no_working_entry", "slot_left"})
_CONTEXT_FAIL = {"not_held": "already_held", "no_working_entry": "entry_working", "slot_left": "lower_rank"}
# check name -> the strategy's reject reason when it is the first failing check
RULES: dict[str, str] = {
    "rvol": "rvol_below_min",
    "rank": "outside_top_n",
    "not_held": "already_held",
    "no_working_entry": "entry_working",
    "price": "price_out_of_range",
    "atr14_present": "atr_missing",
    "atr14": "atr_below_min",
    "avg_volume": "avg_volume_below_min",
    "stop_valid": "stop_invalid",
    "catalyst_present": "catalyst_missing",
    "catalyst_not_bearish": "catalyst_bearish",
    "catalyst_quality": "catalyst_low_quality",
    "slot_left": "lower_rank",
}
DIRECTION_RULES = {"bearish": "bearish_candle", "doji": "doji", "malformed": "malformed_bar"}


def dec(v: Any) -> Decimal | None:
    """A stored number (string, int or Decimal) as a Decimal; None when missing or not a number."""
    if v is None or isinstance(v, bool):
        return None
    try:
        d = Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def _s(v: Any) -> str | None:
    return None if v is None else str(v)


def candle_from(c: Mapping[str, Any] | None) -> Candle | None:
    """The stored opening candle (`candidates.candle`, `_candle_json` shape) as a `Candle`, or None."""
    if not c:
        return None
    o, h, lo, cl = dec(c.get("open")), dec(c.get("high")), dec(c.get("low")), dec(c.get("close"))
    if o is None or h is None or lo is None or cl is None:
        return None
    try:
        start = datetime.fromisoformat(str(c.get("start")))
    except (TypeError, ValueError):
        start = datetime(1970, 1, 1, tzinfo=UTC)
    volume = c.get("volume")
    return Candle(start, start, o, h, lo, cl, int(volume) if isinstance(volume, int) else 0, None)


def direction_of(c: Candle | None, doji_body_pct_max: Decimal) -> str:
    """The strategy's `_direction`: bearish, doji, bullish, or malformed (high < low, or no candle)."""
    if c is None:
        return "malformed"
    try:
        if is_bearish(c):
            return "bearish"
        return "doji" if is_doji(c, doji_body_pct_max) else "bullish"
    except ValueError:
        return "malformed"


def levels(c: Candle | None, atr14: Decimal | None, p: OrbSipParams) -> tuple[Decimal, Decimal] | None:
    """(entry, stop_loss) exactly as the strategy computes them; None without a candle or an ATR."""
    if c is None or atr14 is None:
        return None
    entry = (c.high + p.entry_offset).quantize(Q4, ROUND_HALF_UP)
    stop_loss = (entry - p.stop_atr_fraction * atr14).quantize(Q4, ROUND_HALF_UP)
    return entry, stop_loss


def _context(name: str, reject_reason: str | None) -> Check:
    op: CheckOp = "present" if name == "slot_left" else "absent"
    return Check(name, None, op, None, reject_reason != _CONTEXT_FAIL[name])


def explain_orb(
    data: Mapping[str, Any],
    candle: Mapping[str, Any] | None,
    params: OrbSipParams,
    *,
    catalyst: Mapping[str, Any] | None,
    reject_reason: str | None,
) -> tuple[Check, ...]:
    """Every rule of one ranked candidate, in the strategy's order. `data` is `candidates.data` (rvol, rank,
    atr14, avg_volume, ...), `candle` is `candidates.candle`, `catalyst` has the keys `type`, `direction`,
    `quality`, `classified` (the strategy's `_catalyst_json`), or is None when there is none."""
    p = params
    bar = candle_from(candle)
    out: list[Check] = []

    r = dec(data.get("rvol"))
    if r is None and bar is not None:
        r = rvol(bar.volume, dec(data.get("avg_open_vol_14d")))
    out.append(Check("rvol", _s(r), ">=", str(p.rvol_min), None if r is None else r >= p.rvol_min))
    rank = data.get("rank")
    rank_i = rank if isinstance(rank, int) and not isinstance(rank, bool) else None
    # extend_past_top_n (CATWIDE): a stored candidate may be ranked past top_n, up to max_rank
    rank_max = p.max_rank if p.extend_past_top_n else p.top_n
    out.append(Check("rank", _s(rank_i), "<=", str(rank_max), None if rank_i is None else rank_i <= rank_max))
    out.append(_context("not_held", reject_reason))
    out.append(_context("no_working_entry", reject_reason))

    direction = direction_of(bar, p.doji_body_pct_max)
    out.append(Check("direction", direction, "==", "bullish", direction == "bullish"))
    close = bar.close if bar is not None else dec(data.get("price"))
    out.append(
        Check(
            "price",
            _s(close),
            "between",
            f"{p.price_min}..{p.price_max}",
            None if close is None else p.price_min <= close <= p.price_max,
        )
    )
    atr14 = dec(data.get("atr14"))
    out.append(Check("atr14_present", _s(atr14), "present", None, atr14 is not None))
    out.append(Check("atr14", _s(atr14), ">=", str(p.min_atr), None if atr14 is None else atr14 >= p.min_atr))
    avg_volume = data.get("avg_volume")
    avg_i = avg_volume if isinstance(avg_volume, int) and not isinstance(avg_volume, bool) else None
    # the strategy rejects a missing average volume as below the minimum
    out.append(
        Check(
            "avg_volume",
            _s(avg_i),
            ">=",
            str(p.min_avg_volume),
            avg_i is not None and avg_i >= p.min_avg_volume,
        )
    )
    lv = levels(bar, atr14, p)
    if lv is None:
        out.append(Check("stop_valid", None, "between", None, None))
    else:
        entry, stop_loss = lv
        out.append(
            Check("stop_valid", str(stop_loss), "between", f"0..{entry}", Decimal(0) < stop_loss < entry)
        )

    if p.require_catalyst:
        present = (
            catalyst is not None
            and bool(catalyst.get("classified"))
            and str(catalyst.get("type")) not in NO_CATALYST
        )
        ctype = None if catalyst is None else _s(catalyst.get("type"))
        out.append(Check("catalyst_present", ctype, "present", None, present))
        cdir = None if catalyst is None else _s(catalyst.get("direction"))
        out.append(
            Check("catalyst_not_bearish", cdir, "!=", "bearish", None if cdir is None else cdir != "bearish")
        )
        quality = None if catalyst is None else catalyst.get("quality")
        q_i = quality if isinstance(quality, int) and not isinstance(quality, bool) else None
        out.append(
            Check(
                "catalyst_quality",
                _s(q_i),
                ">=",
                str(p.catalyst_min_quality),
                None if catalyst is None else q_i is not None and q_i >= p.catalyst_min_quality,
            )
        )
    elif p.reject_bearish_catalyst:
        # CATWIDE: only a classified bearish catalyst fails; none, unclassified, neutral or low quality pass
        cdir = None if catalyst is None else _s(catalyst.get("direction"))
        bearish = catalyst is not None and bool(catalyst.get("classified")) and cdir == "bearish"
        out.append(Check("catalyst_not_bearish", cdir, "!=", "bearish", not bearish))
    out.append(_context("slot_left", reject_reason))
    return tuple(out)


def first_failure(checks: Sequence[Check]) -> str | None:
    """The strategy's reject reason for the first failing check (a `passed` of None is not a failure), or
    None when every check passed."""
    for c in checks:
        if c.passed is False:
            if c.name == "direction":
                return DIRECTION_RULES.get(c.value or "malformed", "malformed_bar")
            return RULES.get(c.name, c.name)
    return None


def check_json(c: Check) -> dict[str, Any]:
    return {"name": c.name, "value": c.value, "op": c.op, "threshold": c.threshold, "passed": c.passed}
