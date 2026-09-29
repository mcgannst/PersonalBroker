"""The risk panel and kill-switch lights (live dashboard design §3.2; DB-T4). Reads only
`KillSwitches.active`, `inputs` and `blocking` (never `evaluate`, `pause` or `reset`).

- Kill-switch lights: the four `SWITCHES` in order, with the labels, `needs_web_reset` and `clears` of
  `api.views.killswitch_states` (the Settings/System wording), the open trip's value and threshold, and the
  live value of each automatic switch from `KillSwitches.inputs` at the equity at marks, so the page shows how
  close each one is before it trips.
- Risk panel: open risk to the stops, the entry slots of the enabled entry strategies and today's use of them,
  and a cap of `risk_pct` x equity per slot.

"Today" is the session day of the live dashboard (plan S5): today's ET date when it is a session, else the
latest session before it (a weekend or holiday shows the last session). Read-only and synchronous.
"""

from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from trader.api.livedata.periods import session_day
from trader.api.livedata.types import LivePositions
from trader.api.schemas import KillSwitchLightOut, KillSwitchUnit, RiskOut, TradingState
from trader.api.views import killswitch_states
from trader.broker.ledger import Ledger
from trader.broker.types import AccountState
from trader.db import models as m
from trader.engine.killswitch import KillSwitches
from trader.market.calendar import SessionCalendar
from trader.market.clock import et_date
from trader.settings_store import RuntimeSettings
from trader.strategies.registry import StrategyRegistry

Q4 = Decimal("0.0001")
CENTS = Decimal("0.01")
DEFAULT_MAX_POSITIONS = 1  # an entry strategy without a `max_positions` param holds one position a day


def killswitch_lights(
    killswitches: KillSwitches,
    factory: sessionmaker[Session],
    calendar: SessionCalendar,
    settings: RuntimeSettings,
    run_id: int,
    now: datetime,
    equity: Decimal,
) -> list[KillSwitchLightOut]:
    """The four switches in `SWITCHES` order: tripped or not (with the open trip's value and threshold) and
    the live value against the threshold, from `KillSwitches.inputs` with an account at `equity`."""
    day = session_day(calendar, now)
    states = killswitch_states(killswitches, factory, run_id, day)
    with factory() as s:
        balances = Ledger(calendar).balances(s, run_id, et_date(now))
    account = AccountState(
        total_cash=balances.total,
        settled_cash=balances.settled,
        buying_power=balances.buying_power(settings.cash_account_mode),
        positions_value=equity - balances.total,
        equity=equity,
    )
    inputs = killswitches.inputs(run_id, day, account, calendar.session_open(day))
    lights: list[KillSwitchLightOut] = []
    for st in states:
        live: dict[str, Any]
        unit: KillSwitchUnit
        if st.switch == "daily_loss_pct":
            unit = "pct"
            live = {"value": -inputs.daily_pnl_pct, "threshold": settings.killswitch_daily_loss_pct}
        elif st.switch == "max_drawdown_pct":
            unit = "pct"
            live = {"value": inputs.drawdown_pct, "threshold": settings.killswitch_max_drawdown_pct}
        elif st.switch == "expectancy":
            unit = "r"
            live = {
                "value": inputs.expectancy_r,
                "threshold": settings.killswitch_expectancy_threshold_r,
                "count": inputs.closed_trades,
                "count_min": settings.killswitch_expectancy_min_trades,
            }
        else:  # manual_pause: on or off, nothing to measure
            unit = "none"
            live = {}
        lights.append(
            KillSwitchLightOut(
                switch=st.switch,
                label=st.label,
                tripped=st.tripped,
                tripped_at=st.tripped_at,
                unit=unit,
                trip_value=st.value,
                trip_threshold=st.threshold,
                automatic=st.automatic,
                needs_web_reset=st.needs_web_reset,
                clears=st.clears,
                **live,
            )
        )
    return lights


def trading_state(killswitches: KillSwitches, run_id: int, day: date) -> TradingState:
    """`paused` when a manual pause is active, else `blocked` when an automatic switch blocks entries, else
    `running`."""
    active = [a.switch for a in killswitches.active(run_id, day)]
    if "manual_pause" in active:
        return "paused"
    return "blocked" if active else "running"


def _max_positions(params: Any) -> int:
    value = params.get("max_positions", DEFAULT_MAX_POSITIONS) if isinstance(params, dict) else None
    try:
        return max(0, int(value)) if value is not None else DEFAULT_MAX_POSITIONS
    except (TypeError, ValueError):
        return DEFAULT_MAX_POSITIONS


def _entry_slots(registry: StrategyRegistry) -> tuple[list[str], int]:
    """The enabled entry strategies and the sum of their `max_positions`. A plug-in without settings yet is
    skipped (as the engine skips it)."""
    keys: list[str] = []
    slots = 0
    for key in registry.keys():
        if getattr(registry.plugin_class(key), "kind", None) != "entry":
            continue
        try:
            cfg = registry.current(key)
        except KeyError:
            continue
        if cfg.enabled:
            keys.append(key)
            slots += _max_positions(cfg.params)
    return keys, slots


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
    """Open risk (Σ of max(0, (entry - stop) x qty) over positions with a stop: a stop trailed above the entry
    risks nothing), the slots, the equity at marks and the kill-switch lights."""
    day = session_day(calendar, now)
    equity = positions.equity_at_marks
    lights = killswitch_lights(killswitches, factory, calendar, settings, run_id, now, equity)
    open_risk = sum(
        (max(Decimal(0), (p.entry - p.stop) * p.qty) for p in positions.positions if p.stop is not None),
        Decimal(0),
    )
    keys, slots_max = _entry_slots(registry)
    slots_used = 0
    if keys:
        with factory() as s:
            slots_used = int(
                s.execute(
                    select(func.count())
                    .select_from(m.Position)
                    .where(
                        m.Position.run_id == run_id,
                        m.Position.session_date == day,
                        m.Position.strategy_config_id.in_(
                            select(m.StrategyConfig.id).where(m.StrategyConfig.strategy_key.in_(keys))
                        ),
                    )
                ).scalar_one()
            )
    cap = (settings.risk_pct * equity * slots_max).quantize(CENTS, ROUND_HALF_UP) if slots_max else None
    return RiskOut(
        killswitches=lights,
        equity=equity,
        open_risk=open_risk.quantize(Q4, ROUND_HALF_UP),
        open_risk_cap=cap,
        slots_used=slots_used,
        slots_max=slots_max,
        open_positions=len(positions.positions),
    )
