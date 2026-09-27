"""GET /api/watchlist, POST /api/watchlist (multipart), DELETE /api/watchlist/{date} (SPEC §4.2 manual
fallback, §11; P4-T10).

An uploaded list replaces FinViz for its session (resolved decision 5); deleting it goes back to FinViz.

- `GET ?date=` returns the session's list (404 when none). `date` defaults to `target_session` (the session
  the next nightly prepares).
- `POST` takes `multipart/form-data` only (else 415): `file` (the CSV, required), `date` (optional, the same
  default; it must be a session day not before today's, else 422) and `run_nightly` (optional boolean). A
  request larger than the file limit plus a small form allowance is refused before it is parsed. The CSV
  rules are `trader.market.watchlist.parse_watchlist_csv`; a file it refuses gives 422 with nothing stored.
  A second upload for the date replaces the first. `run_nightly=true` launches `nightly --date <date>
  --force` through `services.jobs`; when a nightly is already running the list is still stored and
  `launched.launched` is false.
- `DELETE /{date}` removes the list (404 when none).
Writes are audited (`watchlist.upload`, `watchlist.delete`) with the actor `web:<username>`.
"""

from datetime import date
from typing import Annotated

import anyio
from fastapi import APIRouter, Query, Request
from starlette.datastructures import UploadFile

from trader.api.deps import ApiServices, CsrfUser, CurrentUser, Services, actor
from trader.api.errors import ApiError
from trader.api.schemas import JobLaunchOut, OkOut, RejectedRowOut, WatchlistOut, WatchlistUploadOut
from trader.db.models import ManualWatchlist
from trader.jobs.nightly import target_session
from trader.market.clock import et_date
from trader.market.watchlist import (
    MAX_BYTES,
    WatchlistError,
    delete_watchlist,
    get_watchlist,
    parse_watchlist_csv,
    store_watchlist,
)

router = APIRouter(tags=["watchlist"])

FORM_ALLOWANCE = 16_384  # multipart boundaries, part headers and the two small fields
_TRUE = frozenset({"true", "1", "on", "yes"})
_FALSE = frozenset({"false", "0", "off", "no", ""})


def _invalid(field: str, msg: str) -> ApiError:
    return ApiError(422, "validation", msg, [{"loc": ["body", field], "msg": msg}])


def _watchlist_out(row: ManualWatchlist) -> WatchlistOut:
    return WatchlistOut(
        session_date=row.session_date,
        tickers=list(row.tickers),
        filename=row.filename,
        uploaded_at=row.uploaded_at,
        uploaded_by=row.uploaded_by,
    )


def _default_date(services: ApiServices) -> date:
    core = services.core
    return target_session(core.calendar, core.clock)


def _check_date(services: ApiServices, day: date) -> None:
    core = services.core
    if day < et_date(core.clock.now()):
        raise _invalid("date", "The date is before today's session")
    try:
        is_session = core.calendar.is_session(day)
    except ValueError:  # outside the calendar's range
        is_session = False
    if not is_session:
        raise _invalid("date", "The date is not a trading session")


def _form_date(value: object) -> date | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise _invalid("date", "The date must be YYYY-MM-DD")
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        raise _invalid("date", "The date must be YYYY-MM-DD") from None


def _form_bool(value: object) -> bool:
    if value is None:
        return False
    text = value.strip().lower() if isinstance(value, str) else None
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise _invalid("run_nightly", "run_nightly must be true or false")


def _too_large() -> ApiError:
    return _invalid("file", f"The file is larger than {MAX_BYTES // 1024} KB")


@router.get("/watchlist")
def read_watchlist(
    _user: CurrentUser,
    services: Services,
    session_date: Annotated[date | None, Query(alias="date")] = None,
) -> WatchlistOut:
    day = session_date or _default_date(services)
    row = get_watchlist(services.core.factory, day)
    if row is None:
        raise ApiError(404, "not_found", f"No uploaded watchlist for {day.isoformat()}")
    return _watchlist_out(row)


@router.post("/watchlist")
async def upload_watchlist(request: Request, user: CsrfUser, services: Services) -> WatchlistUploadOut:
    content_type = request.headers.get("content-type", "")
    if content_type.split(";", 1)[0].strip().lower() != "multipart/form-data":
        raise ApiError(415, "unsupported_media_type", "Send the file as multipart/form-data")
    length = request.headers.get("content-length", "")
    if length.isdigit() and int(length) > MAX_BYTES + FORM_ALLOWANCE:
        raise _too_large()

    async with request.form(max_files=1, max_fields=4, max_part_size=1024) as form:
        upload = form.get("file")
        if not isinstance(upload, UploadFile):
            raise _invalid("file", "Choose a CSV file to upload")
        data = await upload.read(MAX_BYTES + 1)
        filename = upload.filename
        day_field, run_field = form.get("date"), form.get("run_nightly")

    requested = _form_date(day_field)
    run_nightly = _form_bool(run_field)
    if len(data) > MAX_BYTES:
        raise _too_large()
    try:
        parsed = parse_watchlist_csv(data)
    except WatchlistError as exc:
        raise _invalid("file", str(exc)) from None

    core = services.core
    day = requested or _default_date(services)
    _check_date(services, day)
    who = actor(user)

    def store() -> ManualWatchlist | None:
        store_watchlist(core.factory, core.clock, day, parsed.tickers, filename, who)
        return get_watchlist(core.factory, day)

    row = await anyio.to_thread.run_sync(store)
    if row is None:  # deleted by a concurrent request between the two statements
        raise ApiError(409, "conflict", "The watchlist was removed while it was being stored")

    launched: JobLaunchOut | None = None
    if run_nightly:
        if services.jobs.running("nightly"):
            launched = JobLaunchOut(
                job="nightly",
                session_date=day,
                launched=False,
                message="nightly is already running; run it again when it has finished",
            )
        else:
            launched = await services.jobs.launch("nightly", day, True, who)

    return WatchlistUploadOut(
        watchlist=_watchlist_out(row),
        rejected=[RejectedRowOut(row=n, value=v, reason=r) for n, v, r in parsed.rejected],
        launched=launched,
    )


@router.delete("/watchlist/{session_date}")
def remove_watchlist(session_date: date, user: CsrfUser, services: Services) -> OkOut:
    core = services.core
    if not delete_watchlist(core.factory, core.clock, session_date, actor(user)):
        raise ApiError(404, "not_found", f"No uploaded watchlist for {session_date.isoformat()}")
    return OkOut(message=f"Watchlist for {session_date.isoformat()} deleted; the nightly job uses FinViz")
