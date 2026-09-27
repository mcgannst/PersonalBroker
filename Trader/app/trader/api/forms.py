"""Form descriptors (`FieldOut`) for runtime settings and strategy parameters, so the web renders forms
without parsing JSON Schema quirks, and the Settings page's groups.

Stub (P4-T1): T8 implements the functions; `SETTING_GROUPS` is the plan's table.
"""

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel

from trader.api.schemas import FieldOut

# DB key, or key prefix ending in "." or "_", -> the Settings page group.
SETTING_GROUPS: Mapping[str, str] = {
    "approval_mode": "Approvals",
    "starting_cash": "Account",
    "starting_cash_currency": "Account",
    "account_currency": "Account",
    "fx.": "Account",
    "cash_account_mode": "Account",
    "markets_enabled": "Account",
    "risk_pct": "Risk",
    "slippage_buffer": "Risk",
    "no_entry_before_close_minutes": "Risk",
    "quote_poll_seconds": "Fill model",
    "stale_quote_seconds": "Fill model",
    "slippage_": "Fill model",
    "fees.": "Fill model",
    "proposal_ttl_": "Proposals",
    "stop_escalation_seconds": "Proposals",
    "auto_flatten_on_expiry": "Proposals",
    "killswitch.": "Kill switches",
    "claude.": "Claude",
    "premarket.": "Screening",
    "universe.": "Screening",
    "finviz.": "Screening",
    "open_bar.": "Screening",
    "worker.": "Worker and Telegram",
    "scheduler.": "Worker and Telegram",
    "telegram.": "Worker and Telegram",
    "preopen.": "Worker and Telegram",
    "postclose.": "Worker and Telegram",
    "web.": "Web app",
}


def field_out(name: str, schema: Mapping[str, Any], annotation: Any, default: Any) -> FieldOut:
    raise NotImplementedError("P4-T8")


def model_fields_out(model: type[BaseModel], *, by_alias: bool) -> list[FieldOut]:
    raise NotImplementedError("P4-T8")


def group_of(key: str) -> str:
    """The group of a setting's DB key (an exact entry first, then the longest matching prefix)."""
    raise NotImplementedError("P4-T8")
