"""The weekly report (BR-61; SPEC §4.3, §9 Sat 09:00; P5-T9): the Monday-Friday week just ended, its facts
from `trader.reports.metrics`, the Claude commentary's number check and budget, and the stored
`weekly_reports` row.

The number check is the guard against a commentary that invents or computes a figure (Review Focus 4): every
run of digits in the text must be a value found in the facts (see `check_numbers`).
"""

import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from trader.api.schemas import CommentaryStatus
from trader.db import models as m
from trader.db.session import session_scope
from trader.market.calendar import SessionCalendar
from trader.market.clock import ET, Clock
from trader.reports.metrics import Metrics, compute_metrics
from trader.settings_store import RuntimeSettings, SettingsStore

Q4 = Decimal("0.0001")
MIN_WORDS = 50
MAX_WORDS = 400
MAX_CHECKED_DECIMALS = 4
# The metrics quoted for the week and for the run to date, in this order.
METRIC_KEYS = (
    "trades",
    "wins",
    "losses",
    "win_rate",
    "expectancy_r",
    "avg_win_r",
    "avg_loss_r",
    "profit_factor",
    "total_pnl",
    "total_fees",
    "avg_slippage",
    "max_drawdown_pct",
    "adherence_pct",
)
# A number: digits with optional thousands commas, then one optional decimal part. A leading sign or `$` and a
# trailing `%`, `R` or `x` are simply not part of the match.
_NUMBER = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?")
_ISO_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_DECIMAL = re.compile(r"^-?\d+(?:\.\d+)?$")


@dataclass(frozen=True, slots=True)
class WeekWindow:
    start: date  # the Monday
    end: date  # the Friday
    sessions: tuple[date, ...]
    week_ending: date | None  # the week's last session; None when the week had none


@dataclass(frozen=True, slots=True)
class CommentaryOutcome:
    status: CommentaryStatus
    text: str | None
    error: str | None
    model: str | None
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal


def week_window(cal: SessionCalendar, any_day: date) -> WeekWindow:
    """The Monday-Friday week containing `any_day`; a Saturday or Sunday belongs to the week just ended."""
    monday = any_day - timedelta(days=any_day.weekday())
    days = [monday + timedelta(days=i) for i in range(5)]
    sessions = tuple(d for d in days if cal.is_session(d))
    return WeekWindow(monday, days[-1], sessions, sessions[-1] if sessions else None)


def last_completed_week(cal: SessionCalendar, today: date) -> WeekWindow:
    """Saturday or Sunday -> this week; Monday-Friday -> the previous week."""
    return week_window(cal, today if today.weekday() >= 5 else today - timedelta(days=7))


def _s(d: Decimal) -> str:
    """A Decimal as a plain string (never exponent notation)."""
    return format(d, "f")


def _opt(d: Decimal | None) -> str | None:
    return None if d is None else _s(d)


def _metric_facts(mt: Metrics) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in METRIC_KEYS:
        value = getattr(mt, key)
        out[key] = value if isinstance(value, int) or value is None else _s(value)
    return out


def _read_only_now() -> datetime:
    raise RuntimeError("the weekly report never writes settings")


def build_facts(
    factory: sessionmaker[Session],
    cal: SessionCalendar,
    run_id: int,
    week: WeekWindow,
    *,
    settings: RuntimeSettings | None = None,
) -> dict[str, Any]:
    """The JSON-safe facts (Decimals as strings) the commentary may quote. `settings` (for the expectancy
    switch's minimum) is read from the database when not given."""
    s = settings if settings is not None else SettingsStore(factory, _read_only_now).load()
    week_metrics = compute_metrics(factory, run_id, week.start, week.end)
    run_to_date = compute_metrics(factory, run_id, None, week.end)
    with factory() as db:
        days = _days(db, run_id, week)
        best, worst = _best_and_worst(db, run_id, week)
        trips = _trips(db, run_id, week)
        closed = _expectancy_trades(db, run_id, week.end)
    return {
        "week": {
            "start": week.start.isoformat(),
            "end": week.end.isoformat(),
            "sessions": len(week.sessions),
        },
        "week_metrics": _metric_facts(week_metrics),
        "run_to_date": _metric_facts(run_to_date),
        "days": days,
        "best_trade": best,
        "worst_trade": worst,
        "kill_switch_trips": trips,
        "expectancy_switch": {"closed_trades": closed, "min_trades": s.killswitch_expectancy_min_trades},
    }


def _days(db: Session, run_id: int, week: WeekWindow) -> list[dict[str, Any]]:
    per_day = {
        d: (int(n), Decimal(pnl))
        for d, n, pnl in db.execute(
            select(m.Trade.session_date, func.count(), func.coalesce(func.sum(m.Trade.pnl), 0))
            .where(m.Trade.run_id == run_id, m.Trade.session_date.between(week.start, week.end))
            .group_by(m.Trade.session_date)
        ).all()
    }
    answers: dict[date, bool | None] = {
        d: answer
        for d, answer in db.execute(
            select(m.Journal.session_date, m.Journal.rules_followed).where(
                m.Journal.run_id == run_id, m.Journal.session_date.between(week.start, week.end)
            )
        ).all()
    }
    out = []
    for d in week.sessions:
        n, pnl = per_day.get(d, (0, Decimal(0)))
        out.append(
            {
                "date": d.isoformat(),
                "trades": n,
                "pnl": _s(pnl.quantize(Q4)),
                "rules_followed": answers.get(d),
            }
        )
    return out


def _best_and_worst(db: Session, run_id: int, week: WeekWindow) -> tuple[dict[str, Any] | None, ...]:
    """The week's trades with the highest and the lowest R (ties: larger/smaller P&L, then the earlier id)."""
    q = (
        select(m.Symbol.ticker, m.Trade.session_date, m.Trade.pnl, m.Trade.pnl_r)
        .join(m.Symbol, m.Symbol.id == m.Trade.symbol_id)
        .where(
            m.Trade.run_id == run_id,
            m.Trade.session_date.between(week.start, week.end),
            m.Trade.pnl_r.is_not(None),
        )
        .limit(1)
    )
    best = db.execute(q.order_by(m.Trade.pnl_r.desc(), m.Trade.pnl.desc(), m.Trade.id)).first()
    worst = db.execute(q.order_by(m.Trade.pnl_r.asc(), m.Trade.pnl.asc(), m.Trade.id)).first()

    def as_fact(row: Any) -> dict[str, Any] | None:
        if row is None:
            return None
        ticker, d, pnl, pnl_r = row
        return {"ticker": ticker, "date": d.isoformat(), "pnl": _s(pnl), "pnl_r": _s(pnl_r)}

    return as_fact(best), as_fact(worst)


def _trips(db: Session, run_id: int, week: WeekWindow) -> list[dict[str, Any]]:
    rows = db.execute(
        select(
            m.KillSwitchEvent.switch,
            m.KillSwitchEvent.session_date,
            m.KillSwitchEvent.value,
            m.KillSwitchEvent.threshold,
        )
        .where(
            m.KillSwitchEvent.run_id == run_id, m.KillSwitchEvent.session_date.between(week.start, week.end)
        )
        .order_by(m.KillSwitchEvent.tripped_at, m.KillSwitchEvent.id)
    ).all()
    return [
        {"switch": sw, "date": d.isoformat(), "value": _opt(value), "threshold": _opt(threshold)}
        for sw, d, value, threshold in rows
    ]


def _et_day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time(0), tzinfo=ET)
    return start, datetime.combine(day + timedelta(days=1), time(0), tzinfo=ET)


def last_expectancy_reset(db: Session, run_id: int, end: date) -> datetime | None:
    """The expectancy switch's last reset before the end of the ET day `end`, or None. A reset made after
    that day (a report or summary built later) does not change what the switch counted on it. Shared by the
    weekly facts and the daily summary's run-to-date line (`trader.jobs.postclose`)."""
    _, end_ts = _et_day_bounds(end)
    reset_at: datetime | None = db.execute(
        select(func.max(m.KillSwitchEvent.reset_at)).where(
            m.KillSwitchEvent.run_id == run_id,
            m.KillSwitchEvent.switch == "expectancy",
            m.KillSwitchEvent.reset_at < end_ts,
        )
    ).scalar_one()
    return reset_at


def r_trades_since(db: Session, run_id: int, end: date, reset_at: datetime | None) -> int:
    """Closed trades with an R multiple up to session `end`, only those closed after `reset_at` if given (as
    `KillSwitches` counts them for the expectancy switch)."""
    q = select(func.count(m.Trade.pnl_r)).where(m.Trade.run_id == run_id, m.Trade.session_date <= end)
    if reset_at is not None:
        q = q.where(m.Trade.closed_at > reset_at)
    return int(db.execute(q).scalar_one())


def _expectancy_trades(db: Session, run_id: int, end: date) -> int:
    """Closed trades with an R multiple the expectancy switch counts at the week's end."""
    return r_trades_since(db, run_id, end, last_expectancy_reset(db, run_id, end))


# --- the number check ---------------------------------------------------------------------------------------
def _is_ratio(key: str | None) -> bool:
    return key is not None and (key == "win_rate" or key.endswith("_pct"))


# The fields of a kill-switch trip that hold the tripping value and its limit. For a switch named `*_pct`
# (daily loss, max drawdown) both are ratios (0.0612 = 6.12%); the expectancy switch's are R multiples.
_TRIP_RATIO_FIELDS = ("value", "threshold")


def _ratio_fields(mapping: Mapping[Any, Any]) -> tuple[str, ...]:
    """The keys of `mapping` that hold ratios although their names do not end in `_pct`."""
    switch = mapping.get("switch")
    if isinstance(switch, str) and _is_ratio(switch):
        return _TRIP_RATIO_FIELDS
    return ()


def _leaves(value: Any, key: str | None = None, ratio: bool = False) -> Iterator[tuple[bool, Any]]:
    """(is this leaf a ratio, the leaf) for every leaf of `value`."""
    if isinstance(value, Mapping):
        extra = _ratio_fields(value)
        for k, v in value.items():
            name = str(k)
            yield from _leaves(v, name, _is_ratio(name) or name in extra)
    elif isinstance(value, list | tuple):
        for v in value:
            yield from _leaves(v, key, ratio)
    else:
        yield ratio, value


def allowed_values(facts: Mapping[str, Any]) -> set[Decimal]:
    """Every number the commentary may quote: each numeric leaf, its absolute value, ratio fields x 100 (the
    `win_rate` and `*_pct` keys, and the `value`/`threshold` of a `*_pct` kill-switch trip), and the year,
    month and day of every date."""
    out: set[Decimal] = set()
    for ratio, value in _leaves(facts):
        number: Decimal | None = None
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, int | Decimal):
            number = Decimal(value)
        elif isinstance(value, float):
            number = Decimal(str(value))
        elif isinstance(value, str):
            if match := _ISO_DATE.match(value):
                out.update(Decimal(part) for part in match.groups())
                continue
            if _DECIMAL.match(value):
                number = Decimal(value)
        if number is None:
            continue
        out.update((number, abs(number)))
        if ratio:
            out.update((number * 100, abs(number) * 100))
    return out


def _quoted(token: str, allowed: set[Decimal]) -> bool:
    try:
        value = Decimal(token.replace(",", ""))
    except InvalidOperation:  # pragma: no cover - the regex only matches digits
        return False
    places = len(token.partition(".")[2])
    if places > MAX_CHECKED_DECIMALS:
        return value in allowed
    step = Decimal(1).scaleb(-places)
    return any(a.quantize(step, ROUND_HALF_UP) == value for a in allowed)


def check_numbers(text: str, facts: Mapping[str, Any]) -> list[str]:
    """The number tokens of `text` not found in `facts` (empty means OK), each once, in order of appearance.

    A token with k decimals (k <= 4) passes when some allowed value (see `allowed_values`) rounded half-up to
    k decimals equals it; a token with more decimals must equal an allowed value."""
    allowed = allowed_values(facts)
    bad: list[str] = []
    for match in _NUMBER.finditer(text):
        token = match.group(0)
        if token not in bad and not _quoted(token, allowed):
            bad.append(token)
    return bad


def check_length(text: str) -> str | None:
    """None when the commentary has MIN_WORDS..MAX_WORDS words, else the reason it is rejected."""
    words = len(text.split())
    if MIN_WORDS <= words <= MAX_WORDS:
        return None
    return f"length: {words} words (expected {MIN_WORDS}-{MAX_WORDS})"


# --- spend and storage --------------------------------------------------------------------------------------
def claude_spent(factory: sessionmaker[Session], day: date) -> Decimal:
    """The day's Claude spend: catalysts of that session date plus weekly reports updated that ET date.

    A forced re-run on a later day moves the row's whole accumulated `cost_usd` (earlier days' calls
    included) to the re-run's day. That is accepted on purpose: the error only ever over-counts the day the
    budget is checked for (the earlier day is past and never checked again, and `updated_at` only moves
    forward), so it can refuse a re-run's commentary but never let the daily budget be passed. Counting each
    call on its own day exactly would need a per-call spend record (a schema change)."""
    start, end = _et_day_bounds(day)
    with factory() as s:
        catalysts = s.execute(
            select(func.coalesce(func.sum(m.Catalyst.cost_usd), 0)).where(m.Catalyst.session_date == day)
        ).scalar_one()
        reports = s.execute(
            select(func.coalesce(func.sum(m.WeeklyReport.cost_usd), 0)).where(
                m.WeeklyReport.updated_at >= start, m.WeeklyReport.updated_at < end
            )
        ).scalar_one()
    return Decimal(catalysts) + Decimal(reports)


def upsert_report(
    factory: sessionmaker[Session],
    clock: Clock,
    week: WeekWindow,
    run_id: int,
    facts: Mapping[str, Any],
    outcome: CommentaryOutcome,
) -> None:
    """Insert or replace the week's row, keeping `created_at` and accumulating `cost_usd`."""
    if week.week_ending is None:
        raise ValueError("a week without sessions has no report")
    now = clock.now()
    stmt = pg_insert(m.WeeklyReport).values(
        week_ending=week.week_ending,
        week_start=week.start,
        run_id=run_id,
        facts=dict(facts),
        commentary=outcome.text,
        commentary_status=outcome.status,
        commentary_error=outcome.error,
        model=outcome.model,
        input_tokens=outcome.input_tokens,
        output_tokens=outcome.output_tokens,
        cost_usd=outcome.cost_usd,
        created_at=now,
        updated_at=now,
    )
    col = m.WeeklyReport.__table__.c
    replaced = (
        "week_start",
        "run_id",
        "facts",
        "commentary",
        "commentary_status",
        "commentary_error",
        "model",
        "input_tokens",
        "output_tokens",
        "updated_at",
    )
    set_: dict[str, Any] = {k: stmt.excluded[k] for k in replaced}
    set_["cost_usd"] = col.cost_usd + stmt.excluded["cost_usd"]
    with session_scope(factory) as s:
        s.execute(stmt.on_conflict_do_update(index_elements=[col.week_ending], set_=set_))
