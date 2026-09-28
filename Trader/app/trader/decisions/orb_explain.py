"""Every `orb_sip` filter rule of a scan candidate with its value and threshold (stub from P6-T9; P6-T10
implements it).

`explain_orb` evaluates each rule independently, in the strategy's order, with the parameters in effect at
9:35; `first_failure` maps the first failing check to the strategy's reject reason, so it equals
`candidates.reject_reason` (None for a passed candidate). It never imports `OrbSip` itself.
"""

from collections.abc import Mapping, Sequence
from typing import Any

from trader.decisions.types import Check
from trader.strategies.orb_sip import OrbSipParams


def explain_orb(
    data: Mapping[str, Any],
    candle: Mapping[str, Any] | None,
    params: OrbSipParams,
    *,
    catalyst: Mapping[str, Any] | None,
    reject_reason: str | None,
) -> tuple[Check, ...]:
    raise NotImplementedError("P6-T10: explain_orb")


def first_failure(checks: Sequence[Check]) -> str | None:
    raise NotImplementedError("P6-T10: first_failure")
