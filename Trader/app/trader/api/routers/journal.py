"""GET /api/journal, PUT /api/journal/{date} (BR-60 on the web; SPEC §10 `journal`, §11, §12 Journal).

The live run's journal. `GET` lists, newest first, every session day in the range that has a journal row or
trades (`v_daily_pnl`); the default range is the last 30 sessions up to today (ET). `PUT` upserts one day's
row: the day must be a session on or before today (ET), else 422. Only the fields sent change, so a notes
edit never overwrites the "Rules followed?" answer given on Telegram; `rules_followed: null` clears the
answer. Sending `rules_followed` sets `answered_via = "web"`. Every PUT writes an `audit_log` row
`journal.update` with the row before and after. The first PUT of a day inserts with `ON CONFLICT DO NOTHING`
and otherwise locks the existing row before reading it, so two PUTs racing for a new day both audit the
true "before" (the first `null`, the second the first's result).

Dates outside the market calendar's range (2020-2030) are never a 500: a PUT refuses them (422) and the
list's default range falls back to calendar days.
"""

from datetime import date, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Query
from sqlalchemy import Date, bindparam, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from trader.api.deps import CsrfUser, CurrentUser, Services, actor, live_run_id
from trader.api.errors import ApiError
from trader.api.routers.performance import check_range
from trader.api.schemas import Items, JournalDayOut, JournalIn
from trader.db import models as m
from trader.db.session import session_scope
from trader.market.calendar import SessionCalendar
from trader.market.clock import et_date

router = APIRouter(tags=["journal"])

DEFAULT_SESSIONS = 30

_DAYS_SQL = """
SELECT coalesce(j.session_date, d.session_date) AS session_date,
       j.rules_followed, j.notes, j.answered_via, j.updated_at,
       coalesce(d.trades, 0) AS trades, d.realized_pnl
FROM (SELECT * FROM trader.journal WHERE run_id = :run) j
FULL OUTER JOIN (SELECT * FROM trader.v_daily_pnl WHERE run_id = :run) d
    ON d.session_date = j.session_date
WHERE coalesce(j.session_date, d.session_date) BETWEEN :d_from AND :d_to
ORDER BY 1 DESC
"""

_DAILY_PNL_SQL = """
SELECT trades, realized_pnl FROM trader.v_daily_pnl WHERE run_id = :run AND session_date = :day
"""


def _days(s: Session, run_id: int, date_from: date, date_to: date) -> list[JournalDayOut]:
    stmt = text(_DAYS_SQL).bindparams(bindparam("d_from", type_=Date()), bindparam("d_to", type_=Date()))
    rows = s.execute(stmt, {"run": run_id, "d_from": date_from, "d_to": date_to}).mappings()
    return [JournalDayOut(**r) for r in rows]


def _default_from(cal: SessionCalendar, today: date) -> date:
    """The first of the last `DEFAULT_SESSIONS` sessions up to and including `today`."""
    try:
        if cal.is_session(today):
            return cal.sessions_before(today, DEFAULT_SESSIONS - 1)[0]
        return cal.sessions_before(today, DEFAULT_SESSIONS)[0]
    except (ValueError, OverflowError):  # outside the calendar's range: fall back to calendar days
        pass
    try:
        return today - timedelta(days=DEFAULT_SESSIONS * 7 // 5)
    except OverflowError:  # before year 1
        return date.min


def _snapshot(row: m.Journal | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "run_id": row.run_id,
        "session_date": row.session_date.isoformat(),
        "rules_followed": row.rules_followed,
        "notes": row.notes,
        "answered_via": row.answered_via,
        "updated_at": row.updated_at.isoformat() if row.updated_at is not None else None,
    }


def _check_day(cal: SessionCalendar, day: date, today: date) -> None:
    def refuse(msg: str) -> ApiError:
        return ApiError(422, "validation", msg, [{"loc": ["path", "session_date"], "msg": msg}])

    if day > today:
        raise refuse("The date is in the future")
    try:
        is_session = cal.is_session(day)
    except (ValueError, OverflowError):  # outside the calendar's range (far dates overflow pandas)
        is_session = False
    if not is_session:
        raise refuse("The date is not a trading session")


@router.get("/journal")
def list_journal(
    _user: CurrentUser,
    services: Services,
    date_from: Annotated[date | None, Query(alias="from")] = None,
    date_to: Annotated[date | None, Query(alias="to")] = None,
) -> Items[JournalDayOut]:
    check_range(date_from, date_to)
    core = services.core
    run_id = live_run_id(services)
    end = date_to or et_date(core.clock.now())
    if date_from is None:
        date_from = _default_from(core.calendar, end)
    with core.factory() as s:
        return Items[JournalDayOut](items=_days(s, run_id, date_from, end))


@router.put("/journal/{session_date}")
def put_journal(session_date: date, body: JournalIn, user: CsrfUser, services: Services) -> JournalDayOut:
    core = services.core
    now: datetime = core.clock.now()
    _check_day(core.calendar, session_date, et_date(now))
    sent = body.model_fields_set & {"rules_followed", "notes"}
    if not sent:
        raise ApiError(
            422, "validation", "Send rules_followed or notes", [{"loc": ["body"], "msg": "nothing to change"}]
        )
    values: dict[str, Any] = {"updated_at": now}
    if "rules_followed" in sent:
        values["rules_followed"] = body.rules_followed
        values["answered_via"] = "web"
    if "notes" in sent:
        values["notes"] = body.notes
    run_id = live_run_id(services)
    key = (m.Journal.run_id == run_id, m.Journal.session_date == session_date)
    with session_scope(core.factory) as s:
        created = s.execute(
            insert(m.Journal)
            .values(run_id=run_id, session_date=session_date, **values)
            .on_conflict_do_nothing(index_elements=["run_id", "session_date"])
            .returning(m.Journal.run_id)
        ).first()
        before: dict[str, Any] | None = None
        if created is None:  # the row exists (perhaps just committed by a racing PUT): lock, read, update
            before = _snapshot(s.scalars(select(m.Journal).where(*key).with_for_update()).one())
            s.execute(update(m.Journal).where(*key).values(**values))
        s.expire_all()
        row = s.scalars(select(m.Journal).where(*key)).one()
        after = _snapshot(row)
        s.add(m.AuditLog(ts=now, actor=actor(user), action="journal.update", before=before, after=after))
        pnl = s.execute(text(_DAILY_PNL_SQL), {"run": run_id, "day": session_date}).one_or_none()
        return JournalDayOut(
            session_date=row.session_date,
            rules_followed=row.rules_followed,
            notes=row.notes,
            answered_via=row.answered_via,
            updated_at=row.updated_at,
            trades=int(pnl.trades) if pnl is not None else 0,
            realized_pnl=pnl.realized_pnl if pnl is not None else None,
        )
