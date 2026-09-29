"""Replay contracts (P5-T1): statuses, the request, the pinned strategies, progress, the stored run,
errors, and the protocols the runner drives (broker, engine, market).

`runs.params` of a replay (written by `trader.replay.runner.create_replay`, read by `load_replay_run`):

    {"kind": "replay", "date_from": "YYYY-MM-DD", "date_to": "YYYY-MM-DD", "label": str | None,
     "data_mode": "full" | "offline", "catalyst_mode": "stored" | "unknown", "half_spread_bps": "5",
     "settings": RuntimeSettings.model_dump(mode="json", by_alias=True),
     "overrides": {...}, "strategies": [PinnedStrategy.to_json(), ...], "code_version": str}

`runs.progress` holds `ReplayProgress.to_json()`. Nothing here reads the wall clock or draws randomness.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal, Protocol, Self

from sqlalchemy.orm import Session, sessionmaker

from trader.broker.types import FillEvent, OrderSpec, OrderView, PositionView
from trader.db import models as m
from trader.market.types import Candle
from trader.settings_store import RuntimeSettings
from trader.strategies.base import MarketDataView

if TYPE_CHECKING:  # the engine imports the world; the protocol only needs the name
    from trader.engine.orchestrator import EventResult

ReplayStatus = Literal["queued", "running", "completed", "failed", "cancelled"]
DataMode = Literal["full", "offline"]
CatalystMode = Literal["stored", "unknown"]
ConfigScope = Literal["live", "replay"]

REPLAY_STATUSES: tuple[ReplayStatus, ...] = ("queued", "running", "completed", "failed", "cancelled")
ACTIVE_STATUSES: tuple[ReplayStatus, ...] = ("queued", "running")

# The runtime settings a replay request may override (DB keys, SPEC §8 "what if" replays).
REPLAY_OVERRIDE_KEYS: frozenset[str] = frozenset(
    {
        "starting_cash",
        "risk_pct",
        "cash_account_mode",
        "no_entry_before_close_minutes",
        "slippage_min",
        "slippage_bps",
        "fees.commission",
        "fees.sec_rate",
        "replay.half_spread_bps",
        "replay.catalyst_mode",
        "killswitch.daily_loss_pct",
        "killswitch.max_drawdown_pct",
        "killswitch.expectancy_min_trades",
        "killswitch.expectancy_threshold_r",
    }
)


# --- errors -------------------------------------------------------------------------------------------------
class ReplayError(Exception):
    """Base of the replay errors."""


class ReplayInvalid(ReplayError):
    """A replay request that can't run. `errors` lists (field path, message), for example
    ("overrides.risk_pct", "..."). A message never echoes the input value."""

    def __init__(self, errors: list[tuple[str, str]]) -> None:
        self.errors = list(errors)
        super().__init__("; ".join(f"{loc}: {msg}" for loc, msg in self.errors) or "invalid replay request")


class ReplayBusy(ReplayError):
    """Another replay is queued or running (one at a time)."""


class ReplayNotFound(ReplayError):
    """No replay run with that id (a live run is not a replay)."""


# --- request and pinned strategies --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class StrategyOverride:
    enabled: bool | None = None
    params: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ReplayRequest:
    date_from: date
    date_to: date
    label: str | None = None
    overrides: Mapping[str, Any] = field(default_factory=dict)  # keys in REPLAY_OVERRIDE_KEYS
    strategies: Mapping[str, StrategyOverride] = field(default_factory=dict)
    offline: bool = False


@dataclass(frozen=True, slots=True)
class PinnedStrategy:
    """One plug-in's config row pinned at creation: the live row, or a `replay`-scoped override row."""

    key: str
    config_id: int
    revision: int
    version: str
    scope: ConfigScope
    enabled: bool
    params: Mapping[str, Any]

    def to_json(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "config_id": self.config_id,
            "revision": self.revision,
            "version": self.version,
            "scope": self.scope,
            "enabled": self.enabled,
            "params": dict(self.params),
        }

    @classmethod
    def from_json(cls, d: Mapping[str, Any]) -> Self:
        return cls(
            key=str(d["key"]),
            config_id=int(d["config_id"]),
            revision=int(d["revision"]),
            version=str(d["version"]),
            scope=d["scope"],
            enabled=bool(d["enabled"]),
            params=dict(d.get("params") or {}),
        )


# --- progress -----------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ReplayProgress:
    """Written after every session. Holds no wall-clock field, so two runs of the same inputs match."""

    sessions_total: int = 0
    sessions_done: int = 0
    current_date: date | None = None
    trades: int = 0
    forced_closes: int = 0
    biased_days: tuple[date, ...] = ()
    missing_opening_bars: int = 0
    missing_minute_bars: int = 0
    questrade_requests: int = 0

    def to_json(self) -> dict[str, Any]:
        return {
            "sessions_total": self.sessions_total,
            "sessions_done": self.sessions_done,
            "current_date": self.current_date.isoformat() if self.current_date is not None else None,
            "trades": self.trades,
            "forced_closes": self.forced_closes,
            "biased_days": [d.isoformat() for d in self.biased_days],
            "missing_opening_bars": self.missing_opening_bars,
            "missing_minute_bars": self.missing_minute_bars,
            "questrade_requests": self.questrade_requests,
        }

    @classmethod
    def from_json(cls, d: Mapping[str, Any] | None) -> Self:
        if not d:
            return cls()
        current = d.get("current_date")
        return cls(
            sessions_total=int(d.get("sessions_total", 0)),
            sessions_done=int(d.get("sessions_done", 0)),
            current_date=date.fromisoformat(current) if current else None,
            trades=int(d.get("trades", 0)),
            forced_closes=int(d.get("forced_closes", 0)),
            biased_days=tuple(date.fromisoformat(x) for x in d.get("biased_days", ())),
            missing_opening_bars=int(d.get("missing_opening_bars", 0)),
            missing_minute_bars=int(d.get("missing_minute_bars", 0)),
            questrade_requests=int(d.get("questrade_requests", 0)),
        )


# --- the stored run -----------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ReplayRun:
    id: int
    label: str | None
    status: ReplayStatus
    date_from: date
    date_to: date
    data_mode: DataMode
    catalyst_mode: CatalystMode
    half_spread_bps: Decimal
    settings: RuntimeSettings  # the snapshot (overrides applied, approval_mode "auto")
    strategies: tuple[PinnedStrategy, ...]
    overrides: Mapping[str, Any]
    progress: ReplayProgress
    created_at: datetime  # runs.started_at: the row's creation (wall clock)
    finished_at: datetime | None
    error: str | None
    cancel_requested: bool


def _snapshot_settings(snapshot: Mapping[str, Any]) -> RuntimeSettings:
    """A run's frozen settings. SIZECAP: a snapshot taken before `max_position_pct` existed ran without a
    per-stock cap, so it loads as 1 (no cap beyond cash) and replays exactly as it did."""
    if "max_position_pct" not in snapshot:
        snapshot = {**snapshot, "max_position_pct": "1"}
    return RuntimeSettings.model_validate(snapshot)


def replay_run_from_row(row: m.Run) -> ReplayRun:
    """The `ReplayRun` of a replay `runs` row (params shape in the module docstring)."""
    p: Mapping[str, Any] = row.params or {}
    return ReplayRun(
        id=row.id,
        label=row.label,
        status=row.status,  # type: ignore[arg-type]  # a replay row only ever holds a ReplayStatus
        date_from=date.fromisoformat(p["date_from"]),
        date_to=date.fromisoformat(p["date_to"]),
        data_mode=p["data_mode"],
        catalyst_mode=p["catalyst_mode"],
        half_spread_bps=Decimal(str(p["half_spread_bps"])),
        settings=_snapshot_settings(p.get("settings") or {}),
        strategies=tuple(PinnedStrategy.from_json(s) for s in p.get("strategies", ())),
        overrides=dict(p.get("overrides") or {}),
        progress=ReplayProgress.from_json(row.progress),
        created_at=row.started_at,
        finished_at=row.finished_at,
        error=row.error,
        cancel_requested=bool(row.cancel_requested),
    )


def load_replay_run(factory: sessionmaker[Session], run_id: int) -> ReplayRun:
    """Read one replay run. `ReplayNotFound` for an unknown id or a run that is not a replay."""
    with factory() as s:
        row = s.get(m.Run, run_id)
        if row is None or row.mode != "replay":
            raise ReplayNotFound(f"replay {run_id} not found")
        return replay_run_from_row(row)


# --- what the runner drives ---------------------------------------------------------------------------------
class ReplayBroker(Protocol):
    """The part of `SimBroker` the runner uses (working orders, the forced close)."""

    def working_symbol_ids(self) -> list[int]: ...
    def working_orders(self) -> list[OrderView]: ...
    def open_positions(self) -> list[PositionView]: ...
    def submit(self, spec: OrderSpec, session: Session | None = None) -> int: ...
    def cancel(self, order_id: int, reason: str, session: Session | None = None) -> bool: ...


class ReplayEngine(Protocol):
    """The part of `Engine` the runner drives. `Engine` satisfies it once P5-T4 adds `on_candles`."""

    @property
    def broker(self) -> ReplayBroker: ...
    async def run_event(self, event_key: str, session_date: date) -> "EventResult": ...
    async def on_candles(self, candles: Mapping[int, Candle], now: datetime) -> list[FillEvent]: ...
    async def tick(self, now: datetime) -> None: ...
    async def end_of_session(self, session_date: date) -> list[PositionView]: ...


class ReplayMarket(MarketDataView, Protocol):
    """What strategies read in a replay (`MarketDataView`, never past the replay clock) plus the runner-only
    loading helpers. Implemented by `trader.replay.data.ReplayData` (P5-T5)."""

    async def prepare_day(self, session_date: date) -> None: ...
    async def load_minute_bars(self, symbol_ids: Sequence[int], session_date: date) -> None: ...
    def bar_ending_at(self, symbol_id: int, at: datetime) -> Candle | None: ...
    def last_close(self, symbol_id: int, at: datetime) -> Decimal | None: ...
    def progress_counts(self) -> Mapping[str, int]: ...

    @property
    def biased_days(self) -> frozenset[date]: ...
