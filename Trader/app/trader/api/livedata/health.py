"""The Control page's health and soak parts (live dashboard plan S10, design §4.5–4.6). Questrade numbers come
from the worker's heartbeat detail only (never a Questrade call). DB-T1 stub with the final signatures;
DB-T6 implements it."""

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from trader.api.deps import ApiServices
from trader.api.schemas import HealthPanelOut, OpeningBarsOut, QuestradeStatsOut, SoakSummaryOut
from trader.market.calendar import SessionCalendar


def health_panel(services: ApiServices, now: datetime) -> HealthPanelOut:
    raise NotImplementedError("DB-T6")


def questrade_stats(detail: Mapping[str, Any] | None, now: datetime) -> QuestradeStatsOut | None:
    raise NotImplementedError("DB-T6")


def opening_bars(
    detail: Mapping[str, Any] | None, calendar: SessionCalendar, now: datetime
) -> OpeningBarsOut | None:
    """The first `candle_batches` entry of today that started in [09:35, 09:40) ET, or None."""
    raise NotImplementedError("DB-T6")


def soak_summary(services: ApiServices, now: datetime) -> SoakSummaryOut:
    raise NotImplementedError("DB-T6")
