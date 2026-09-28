"""Runtime settings stored one key per row in trader.settings (SPEC §13), with an audit trail.

load() fails closed: any invalid stored row raises ValidationError (the invalid keys are logged,
never their values). set() ignores invalid rows of OTHER keys, so a corrupt row never locks out
writes, and setting the corrupt key itself to a valid value repairs the store.
"""

from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

import structlog
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError, field_validator
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from trader.db.models import AuditLog, Setting
from trader.db.session import session_scope

log = structlog.get_logger("settings_store")

DEFAULT_UNIVERSE_FILTERS = "ind_stocksonly,sh_price_5to50,sh_avgvol_o1000,ta_averagetruerange_o0.5,geo_usa"
# A comma-separated list of FinViz filter tokens, for example "sh_price_5to50,geo_usa".
FINVIZ_FILTERS_PATTERN = r"^[a-z0-9_.]+(,[a-z0-9_.]+)*$"
# One or more such lists separated by "|": the pre-market job runs one screen per list and unions the
# results, because FinViz can't OR two values of one filter in a single screen (it silently ignores
# "earningsdate_yesterdayafter|todaybefore", verified live 2026-09-27).
FINVIZ_FILTER_SETS_PATTERN = r"^[a-z0-9_.]+(,[a-z0-9_.]+)*(\|[a-z0-9_.]+(,[a-z0-9_.]+)*)*$"
# Stephen's decision (2026-09-27): earnings reported after the previous TRADING session's close OR before
# today's open. The pre-market job computes that window from the exchange calendar when the setting holds
# the keyword EARNINGS_SESSION_WINDOW (SPEC §4.2). The first default, LEGACY_EARNINGS_FILTER, used FinViz's
# "yesterday", which is the previous CALENDAR day (verified live on Sunday 2026-09-27: 0 matches while
# Friday had reporters), so it missed Friday's after-close reporters on a Monday; a stored copy of it is
# run as the session window too. Any other value is "|"-separated filter lists run as plain screens.
EARNINGS_SESSION_WINDOW = "session_window"
LEGACY_EARNINGS_FILTER = "earningsdate_yesterdayafter|earningsdate_todaybefore"
DEFAULT_EARNINGS_FILTER = EARNINGS_SESSION_WINDOW
TICKER_PATTERN = r"^[A-Z][A-Z0-9.\-]{0,9}$"
OVERLAY_SYMBOL = "SPY"  # the market overlay reads SPY bars, so it must always be in the universe
# A session event key (Phase 3). At most 39 characters, so the job name `event:<key>` fits job_runs.job
# and the failure-event source `job.event:<key>` fits event_log.source (both varchar(50)).
EVENT_KEY_PATTERN = r"^[a-z][a-z0-9_]{0,38}$"
DEFAULT_ALWAYS_FIRE_LATE = ("flatten", "entry_cancel", "overlay_decision")

Market = Literal["US", "TSX"]
Ticker = Annotated[str, StringConstraints(pattern=TICKER_PATTERN)]
EventKey = Annotated[str, StringConstraints(pattern=EVENT_KEY_PATTERN)]
Currency = Literal["USD", "CAD"]
ClaudeModel = Literal["claude-sonnet-5", "claude-haiku-4-5"]


def _default_markets() -> list[Market]:
    return ["US"]


class RuntimeSettings(BaseModel):
    # The DB key of a setting is its alias. Every field added after Phase 1 declares alias= (even when the
    # key equals the field name), because _DB_KEYS and model_dump(by_alias=True) are built from alias.
    # No model_validator: keys are validated one at a time.
    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    approval_mode: Literal["manual", "auto"] = "manual"
    markets_enabled: list[Market] = Field(default_factory=_default_markets, min_length=1)
    universe_finviz_filters: str = Field(
        DEFAULT_UNIVERSE_FILTERS, pattern=FINVIZ_FILTERS_PATTERN, alias="universe.finviz_filters"
    )
    universe_extra_symbols: list[Ticker] = Field(
        default_factory=lambda: [OVERLAY_SYMBOL], alias="universe.extra_symbols"
    )
    finviz_min_interval_seconds: float = Field(
        2.0, ge=2.0, le=60, allow_inf_nan=False, alias="finviz.min_interval_seconds"
    )
    finviz_cache_hours: float = Field(12.0, ge=0, le=168, allow_inf_nan=False, alias="finviz.cache_hours")
    open_bar_lookback_sessions: int = Field(14, ge=5, le=30, alias="open_bar.lookback_sessions")
    # When FinViz fails, the nightly job reuses the last stored universe. If that universe's original
    # FinViz date is more than this many sessions before the target session, the fallback is still
    # used but flagged stale (an error event and `fallback_stale` in the job detail).
    universe_fallback_stale_after_sessions: int = Field(
        3, ge=1, le=10, alias="universe.fallback_stale_after_sessions"
    )
    # --- Phase 2: account and FX (SPEC §7.3, BR-22)
    starting_cash: Decimal = Field(
        Decimal("720"), gt=0, le=Decimal("10000000"), allow_inf_nan=False, alias="starting_cash"
    )
    starting_cash_currency: Currency = Field("USD", alias="starting_cash_currency")
    account_currency: Currency = Field("USD", alias="account_currency")
    fx_cad_usd_rate: Decimal = Field(
        Decimal("0.72"), gt=0, le=Decimal("2"), allow_inf_nan=False, alias="fx.cad_usd_rate"
    )
    fx_fee_pct: Decimal = Field(
        Decimal("0.015"), ge=0, le=Decimal("0.10"), allow_inf_nan=False, alias="fx.fee_pct"
    )
    cash_account_mode: bool = Field(True, alias="cash_account_mode")
    # --- risk (SPEC §6.1, BR-40)
    risk_pct: Decimal = Field(
        Decimal("0.02"), gt=0, le=Decimal("0.10"), allow_inf_nan=False, alias="risk_pct"
    )
    slippage_buffer: Decimal = Field(
        Decimal("0.005"), ge=0, le=Decimal("0.05"), allow_inf_nan=False, alias="slippage_buffer"
    )
    no_entry_before_close_minutes: int = Field(30, ge=0, le=390, alias="no_entry_before_close_minutes")
    # --- fill model (SPEC §7.2)
    quote_poll_seconds: float = Field(2.0, ge=1.0, le=60, allow_inf_nan=False, alias="quote_poll_seconds")
    stale_quote_seconds: float = Field(10.0, ge=1.0, le=300, allow_inf_nan=False, alias="stale_quote_seconds")
    slippage_min: Decimal = Field(
        Decimal("0.01"), ge=0, le=Decimal("1"), allow_inf_nan=False, alias="slippage_min"
    )
    slippage_bps: Decimal = Field(
        Decimal("5"), ge=0, le=Decimal("100"), allow_inf_nan=False, alias="slippage_bps"
    )
    fees_commission: Decimal = Field(
        Decimal("0"), ge=0, le=Decimal("100"), allow_inf_nan=False, alias="fees.commission"
    )
    fees_direct_route: bool = Field(False, alias="fees.direct_route")
    fees_ecn_per_share: Decimal = Field(
        Decimal("0.0035"), ge=0, le=Decimal("1"), allow_inf_nan=False, alias="fees.ecn_per_share"
    )
    fees_sec_rate: Decimal = Field(
        Decimal("0.0000206"), ge=0, le=Decimal("0.001"), allow_inf_nan=False, alias="fees.sec_rate"
    )
    # --- proposals (SPEC §6.2)
    proposal_ttl_entry_seconds: int = Field(300, ge=30, le=3600, alias="proposal_ttl_entry_seconds")
    proposal_ttl_stop_seconds: int = Field(180, ge=30, le=3600, alias="proposal_ttl_stop_seconds")
    proposal_ttl_exit_seconds: int = Field(300, ge=30, le=3600, alias="proposal_ttl_exit_seconds")
    stop_escalation_seconds: int = Field(60, ge=10, le=3600, alias="stop_escalation_seconds")
    auto_flatten_on_expiry: bool = Field(True, alias="auto_flatten_on_expiry")
    # --- kill switches (SPEC §6.3, BR-41)
    killswitch_daily_loss_pct: Decimal = Field(
        Decimal("0.05"), gt=0, le=Decimal("1"), allow_inf_nan=False, alias="killswitch.daily_loss_pct"
    )
    killswitch_max_drawdown_pct: Decimal = Field(
        Decimal("0.15"), gt=0, le=Decimal("1"), allow_inf_nan=False, alias="killswitch.max_drawdown_pct"
    )
    killswitch_expectancy_min_trades: int = Field(
        50, ge=1, le=10000, alias="killswitch.expectancy_min_trades"
    )
    killswitch_expectancy_threshold_r: Decimal = Field(
        Decimal("0"),
        ge=Decimal("-10"),
        le=Decimal("10"),
        allow_inf_nan=False,
        alias="killswitch.expectancy_threshold_r",
    )
    # --- Claude (SPEC §4.3)
    claude_model: ClaudeModel = Field("claude-sonnet-5", alias="claude.model")
    claude_daily_budget_usd: Decimal = Field(
        Decimal("1.00"), ge=0, le=Decimal("100"), allow_inf_nan=False, alias="claude.daily_budget_usd"
    )
    claude_premarket_max_candidates: int = Field(50, ge=0, le=500, alias="claude.premarket_max_candidates")
    # --- pre-market scan (SPEC §4.2)
    premarket_gap_min_pct: Decimal = Field(
        Decimal("0.03"), gt=0, le=Decimal("1"), allow_inf_nan=False, alias="premarket.gap_min_pct"
    )
    # Each: one or more "|"-separated FinViz filter lists added to the universe filters; one screen per list.
    premarket_news_filter: str = Field(
        "news_date_today", pattern=FINVIZ_FILTER_SETS_PATTERN, alias="premarket.news_filter"
    )
    premarket_earnings_filter: str = Field(
        DEFAULT_EARNINGS_FILTER, pattern=FINVIZ_FILTER_SETS_PATTERN, alias="premarket.earnings_filter"
    )
    # --- Phase 3: worker, scheduler, Telegram, day-level jobs (SPEC §1, §4.4, §9)
    worker_heartbeat_seconds: int = Field(15, ge=5, le=300, alias="worker.heartbeat_seconds")
    worker_heartbeat_stale_seconds: int = Field(120, ge=30, le=3600, alias="worker.heartbeat_stale_seconds")
    worker_idle_poll_seconds: float = Field(
        30.0, ge=5, le=300, allow_inf_nan=False, alias="worker.idle_poll_seconds"
    )
    scheduler_late_grace_seconds: int = Field(120, ge=0, le=3600, alias="scheduler.late_grace_seconds")
    # Events fired however late (until the close): exits and cancels must never be skipped.
    scheduler_always_fire_late: list[EventKey] = Field(
        default_factory=lambda: list(DEFAULT_ALWAYS_FIRE_LATE), alias="scheduler.always_fire_late"
    )
    telegram_poll_timeout_seconds: int = Field(30, ge=1, le=50, alias="telegram.poll_timeout_seconds")
    telegram_confirm_ttl_seconds: int = Field(60, ge=10, le=600, alias="telegram.confirm_ttl_seconds")
    telegram_relay_catchup_max: int = Field(20, ge=0, le=200, alias="telegram.relay_catchup_max")
    preopen_notify_when_ok: bool = Field(True, alias="preopen.notify_when_ok")
    postclose_archive_top_n: int = Field(20, ge=0, le=100, alias="postclose.archive_top_n")
    # --- Phase 4: web app (SPEC §11, §12, §14): sessions, login limits, live updates, quote cache
    web_session_idle_hours: int = Field(168, ge=1, le=2160, alias="web.session_idle_hours")
    web_session_max_days: int = Field(30, ge=1, le=365, alias="web.session_max_days")
    web_login_max_failures: int = Field(5, ge=3, le=20, alias="web.login_max_failures")
    web_lockout_minutes: int = Field(15, ge=1, le=1440, alias="web.lockout_minutes")
    web_login_rate_per_minute: int = Field(10, ge=1, le=60, alias="web.login_rate_per_minute")
    web_sse_poll_seconds: float = Field(1.0, ge=0.5, le=10, allow_inf_nan=False, alias="web.sse_poll_seconds")
    web_quote_cache_seconds: float = Field(
        5.0, ge=1, le=60, allow_inf_nan=False, alias="web.quote_cache_seconds"
    )
    # --- Phase 5: replay (SPEC §7.4, §8), weekly report (SPEC §4.3), job retries (SPEC §9), log mirror (§2)
    replay_half_spread_bps: Decimal = Field(
        Decimal("5"), ge=0, le=Decimal("100"), allow_inf_nan=False, alias="replay.half_spread_bps"
    )
    replay_catalyst_mode: Literal["stored", "unknown"] = Field("stored", alias="replay.catalyst_mode")
    replay_questrade_rps: float = Field(4.0, ge=1, le=10, allow_inf_nan=False, alias="replay.questrade_rps")
    replay_questrade_window_days: int = Field(85, ge=1, le=120, alias="replay.questrade_window_days")
    replay_max_sessions: int = Field(130, ge=1, le=500, alias="replay.max_sessions")
    reports_weekly_commentary: bool = Field(True, alias="reports.weekly_commentary")
    reports_weekly_max_cost_usd: Decimal = Field(
        Decimal("0.05"), ge=0, le=Decimal("1"), allow_inf_nan=False, alias="reports.weekly_max_cost_usd"
    )
    # P5-GO fix round 1: at most 3 attempts and a 600 s first wait (600 + 1200 s), so a day-level job's
    # retries end well inside its window; jobs with a hard stop also pass a RetryPolicy deadline.
    jobs_retry_attempts: int = Field(3, ge=1, le=3, alias="jobs.retry_attempts")
    jobs_retry_delay_seconds: int = Field(120, ge=10, le=600, alias="jobs.retry_delay_seconds")
    logging_mirror_level: Literal["error", "critical", "off"] = Field("error", alias="logging.mirror_level")
    logging_mirror_max_per_minute: int = Field(30, ge=1, le=600, alias="logging.mirror_max_per_minute")
    # --- Phase 6 amendment: the decision log (P6-T9). Reporting only (D2: the `reports.*` group)
    reports_decisions_enabled: bool = Field(True, alias="reports.decisions_enabled")
    reports_decisions_scan_detail: Literal["all", "ranked"] = Field(
        "all", alias="reports.decisions_scan_detail"
    )
    reports_decisions_refresh_seconds: int = Field(
        60, ge=15, le=600, alias="reports.decisions_refresh_seconds"
    )
    reports_decisions_retention_days: int = Field(
        400, ge=30, le=3650, alias="reports.decisions_retention_days"
    )
    reports_decisions_replay_retention_days: int = Field(
        30, ge=1, le=3650, alias="reports.decisions_replay_retention_days"
    )
    reports_decisions_in_summary: bool = Field(True, alias="reports.decisions_in_summary")

    @field_validator("scheduler_always_fire_late")
    @classmethod
    def _unique_event_keys(cls, v: list[str]) -> list[str]:
        if len(set(v)) != len(v):
            raise ValueError("scheduler.always_fire_late must not contain duplicates")
        return v

    @field_validator("markets_enabled")
    @classmethod
    def _unique_markets(cls, v: list[Market]) -> list[Market]:
        if len(set(v)) != len(v):
            raise ValueError("markets_enabled must not contain duplicates")
        return v

    @field_validator("universe_extra_symbols")
    @classmethod
    def _extra_symbols(cls, v: list[str]) -> list[str]:
        if len(set(v)) != len(v):
            raise ValueError("universe.extra_symbols must not contain duplicates")
        if OVERLAY_SYMBOL not in v:
            raise ValueError(f"universe.extra_symbols must include {OVERLAY_SYMBOL}")
        return v


# DB key -> field name.
_DB_KEYS: dict[str, str] = {(f.alias or name): name for name, f in RuntimeSettings.model_fields.items()}
_DEFAULTS: dict[str, Any] = RuntimeSettings().model_dump(mode="json")


def _key_is_valid(key: str, value: Any) -> bool:
    try:
        RuntimeSettings.model_validate({key: value})
    except ValidationError:
        return False
    return True


def _split_rows(rows: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Validate each known stored key on its own. Returns (valid rows, invalid keys)."""
    valid: dict[str, Any] = {}
    invalid: list[str] = []
    for key, value in rows.items():
        if key not in _DB_KEYS:
            continue
        if _key_is_valid(key, value):
            valid[key] = value
        else:
            invalid.append(key)
    return valid, invalid


class SettingsStore:
    def __init__(self, factory: sessionmaker[Session], now: Callable[[], datetime]) -> None:
        self._factory = factory
        self._now = now

    def _rows(self, session: Session) -> dict[str, Any]:
        return {row.key: row.value for row in session.execute(select(Setting)).scalars()}

    def load(self) -> RuntimeSettings:
        with self._factory() as session:
            rows = self._rows(session)
        try:
            return RuntimeSettings.model_validate(rows)
        except ValidationError:
            _, invalid = _split_rows(rows)
            log.error("settings.invalid_stored_rows", keys=sorted(invalid))
            raise

    def set(self, key: str, value: Any, actor: str) -> RuntimeSettings:
        if key not in _DB_KEYS:
            raise KeyError(key)
        field = _DB_KEYS[key]
        now = self._now()
        with session_scope(self._factory) as session:
            valid, _ = _split_rows(self._rows(session))
            updated = RuntimeSettings.model_validate({**valid, key: value})  # raises ValidationError
            stored = updated.model_dump(mode="json")[field]
            # Create the row if it is new. A concurrent creator makes this wait for its commit, then
            # do nothing, so two first writes of one key never collide on the primary key.
            inserted = session.execute(
                pg_insert(Setting)
                .values(key=key, value=stored, updated_at=now, updated_by=actor)
                .on_conflict_do_nothing(index_elements=[Setting.key])
                .returning(Setting.key)
            ).first()
            if inserted is not None:
                before: dict[str, Any] = {"value": _DEFAULTS[field]}
            else:
                # The row exists: lock it and read "before" under the lock.
                row = session.execute(
                    select(Setting)
                    .where(Setting.key == key)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                ).scalar_one()
                if _key_is_valid(key, row.value):
                    before = {
                        "value": RuntimeSettings.model_validate({key: row.value}).model_dump(mode="json")[
                            field
                        ]
                    }
                else:
                    before = {"value": row.value, "invalid": True}
                row.value, row.updated_at, row.updated_by = stored, now, actor
            session.add(
                AuditLog(
                    ts=now, actor=actor, action=f"settings.set:{key}", before=before, after={"value": stored}
                )
            )
        return updated
