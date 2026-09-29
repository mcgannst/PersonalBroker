"""The risk panel and kill-switch lights (live dashboard design §3.2). Reads only `KillSwitches.active`,
`inputs` and `blocking` (never `evaluate`, `pause` or `reset`). DB-T1 stub with the final signatures; DB-T4
implements it."""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy.orm import Session, sessionmaker

from trader.api.livedata.types import LivePositions
from trader.api.schemas import KillSwitchLightOut, RiskOut, TradingState
from trader.engine.killswitch import KillSwitches
from trader.market.calendar import SessionCalendar
from trader.settings_store import RuntimeSettings
from trader.strategies.registry import StrategyRegistry


def killswitch_lights(
    killswitches: KillSwitches,
    factory: sessionmaker[Session],
    calendar: SessionCalendar,
    settings: RuntimeSettings,
    run_id: int,
    now: datetime,
    equity: Decimal,
) -> list[KillSwitchLightOut]:
    raise NotImplementedError("DB-T4")


def trading_state(killswitches: KillSwitches, run_id: int, day: date) -> TradingState:
    raise NotImplementedError("DB-T4")


def risk_panel(
    killswitches: KillSwitches,
    registry: StrategyRegistry,
    factory: sessionmaker[Session],
    calendar: SessionCalendar,
    settings: RuntimeSettings,
    run_id: int,
    now: datetime,
    positions: LivePositions,
) -> RiskOut:
    raise NotImplementedError("DB-T4")
