"""Mirror `error` / `critical` log lines into `event_log` for the System page (SPEC §2; P5-T14).

Asynchronous and lossy: `emit` only masks the record and queues it (a full queue drops it and counts it); a
daemon thread writes batches. It never blocks, never raises, never recurses, is rate-limited, and its rows
(source `log.<process>`) are never relayed to Telegram (the relay skips `log.*` sources, P5-T10).

The mirror never logs anything itself: its own failures (a full queue, a failed write) are only counted in
`dropped` and reported by the "log mirror dropped N lines" row, so a database outage cannot feed back into
the log it is mirroring.
"""

import json
import logging
import math
import queue
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from sqlalchemy.orm import Session, sessionmaker

from trader.db.models import EventLog
from trader.logging_setup import REDACTED, is_secret_key, redact_text
from trader.market.clock import Clock
from trader.settings_store import RuntimeSettings

MIRROR_SOURCE_PREFIX = "log."

SOURCE_MAX = 50  # event_log.source is varchar(50)
MESSAGE_MAX = 500
DATA_MAX_BYTES = 4096
EXC_MESSAGE_MAX = 300
VALUE_MAX = 1000  # one string value in data, before the 4 KB cut
DEPTH_MAX = 4
REPEAT_WINDOW = timedelta(seconds=60)
RATE_WINDOW = timedelta(seconds=60)
REPEAT_KEYS_MAX = 10_000  # (logger, event) keys remembered for the repeat window

# Loggers never mirrored: the mirror's own, and the database layers its writes go through.
SKIPPED_LOGGERS = ("trader.logging_mirror", "sqlalchemy", "alembic")

# Event-dict keys that are part of every structlog line (or of the exception) rather than its data.
_META_KEYS = frozenset(
    {"event", "logger", "level", "timestamp", "process", "exception", "exc_info", "stack", "stack_info"}
)
_EVENT_LOGGED = "event_logged"
# Attributes every stdlib LogRecord has: anything else came from `extra={...}`.
_RECORD_ATTRS = frozenset(logging.makeLogRecord({}).__dict__) | {"message", "asctime", "taskName"}

_LEVELS = {"error": logging.ERROR, "critical": logging.CRITICAL}


@dataclass(frozen=True, slots=True)
class _Row:
    ts: datetime
    level: str
    logger: str
    event: str
    message: str
    data: dict[str, Any] | None


def _skipped_logger(name: str) -> bool:
    """The mirror's own logger (and its children) and every `sqlalchemy*` / `alembic*` logger."""
    own = SKIPPED_LOGGERS[0]
    return name == own or name.startswith(own + ".") or name.startswith(SKIPPED_LOGGERS[1:])


def _one_line(text: str, limit: int) -> str:
    return " ".join(redact_text(text).split())[:limit]


def _masked(value: Any, depth: int = 0) -> Any:
    """A JSON-safe, masked copy: strings by pattern, secret-named keys entirely, other objects via str()."""
    if isinstance(value, str):
        return redact_text(value)[:VALUE_MAX]
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)  # JSONB refuses NaN and Infinity
    if depth >= DEPTH_MAX:
        return redact_text(str(value))[:VALUE_MAX]
    if isinstance(value, Mapping):
        return {
            str(k): REDACTED if is_secret_key(k) and v is not None else _masked(v, depth + 1)
            for k, v in value.items()
        }
    if isinstance(value, list | tuple | set | frozenset):
        return [_masked(v, depth + 1) for v in value]
    return redact_text(str(value))[:VALUE_MAX]


def _cut(data: dict[str, Any]) -> dict[str, Any]:
    """At most DATA_MAX_BYTES of JSON: fields are kept in order while they fit, then `truncated: true`."""
    if len(json.dumps(data)) <= DATA_MAX_BYTES:
        return data
    kept: dict[str, Any] = {}
    size = len(json.dumps({"truncated": True}))
    for key, value in data.items():
        item = len(json.dumps({key: value})) - 1  # the braces are counted once, in `size`
        if size + item <= DATA_MAX_BYTES:
            kept[key] = value
            size += item
    kept["truncated"] = True
    return kept


def _exception_fields(record: logging.LogRecord, event: Mapping[str, Any] | None) -> dict[str, str]:
    """`exc_type` and a masked one-line `exc_message`, never the traceback."""
    exc: BaseException | None = None
    if record.exc_info and record.exc_info[1] is not None:
        exc = record.exc_info[1]
    elif event is not None:
        info = event.get("exc_info")
        if isinstance(info, BaseException):
            exc = info
        elif isinstance(info, tuple) and len(info) == 3 and isinstance(info[1], BaseException):
            exc = info[1]
        elif isinstance(event.get("exception"), str):
            # structlog's format_exc_info already rendered the traceback: its last line is "Type: message"
            lines = [line for line in event["exception"].splitlines() if line.strip()]
            if lines:
                exc_type, _, message = lines[-1].partition(":")
                return {
                    "exc_type": exc_type.strip()[:100],
                    "exc_message": _one_line(message, EXC_MESSAGE_MAX),
                }
    if exc is None:
        return {}
    return {"exc_type": type(exc).__name__, "exc_message": _one_line(str(exc), EXC_MESSAGE_MAX)}


class _MirrorHandler(logging.Handler):
    def __init__(self, mirror: "EventLogMirror", level: int) -> None:
        super().__init__(level)
        self._mirror = mirror

    def emit(self, record: logging.LogRecord) -> None:
        self._mirror._emit(record)

    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802 (logging's name)
        """Never print or raise: the mirror's failures are only counted."""


class EventLogMirror:
    def __init__(
        self,
        factory: sessionmaker[Session],
        clock: Clock,
        *,
        process: str,
        level: Literal["error", "critical"],
        max_per_minute: int,
        run_id: int | None = None,
        queue_size: int = 1000,
        flush_seconds: float = 1.0,
    ) -> None:
        self._factory = factory
        self._clock = clock
        self.process = process
        self.level = level
        self.max_per_minute = max_per_minute
        self.run_id = run_id
        self.queue_size = queue_size
        self.flush_seconds = flush_seconds
        self.dropped = 0
        self.handler: logging.Handler | None = None
        self.source = f"{MIRROR_SOURCE_PREFIX}{process}"[:SOURCE_MAX]

        self._queue: queue.Queue[_Row] = queue.Queue(maxsize=max(1, queue_size))
        self._count_lock = threading.Lock()  # guards `dropped` (bumped by emitting threads and the writer)
        self._write_lock = threading.Lock()  # one writer at a time (the thread, or an inline flush)
        self._local = threading.local()  # `busy` while this thread writes: its own log lines are skipped
        self._thread: threading.Thread | None = None
        self._thread_ident: int | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._cond = threading.Condition()
        self._requested = 0
        self._served = 0
        self._written = 0
        self._closing = False
        # writer state (only touched under _write_lock)
        self._last_row: dict[tuple[str, str], datetime] = {}
        self._repeats: dict[tuple[str, str], int] = {}
        self._window_start: datetime | None = None
        self._window_rows = 0
        self._reported = 0

    # --- public ---------------------------------------------------------------------------------------------

    def install(self) -> None:
        """Add the mirror's handler to the root logger."""
        if self.handler is None:
            self.handler = _MirrorHandler(self, _LEVELS[self.level])
        root = logging.getLogger()
        if self.handler not in root.handlers:
            root.addHandler(self.handler)

    def start(self) -> None:
        """Start the daemon thread that writes queued rows every `flush_seconds`."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="trader-log-mirror", daemon=True)
        self._thread.start()

    def flush(self, timeout: float = 2.0) -> int:
        """Write what is queued now; returns the rows written (0 when the write did not finish in time)."""
        try:
            thread = self._thread
            if thread is not None and thread.is_alive() and threading.get_ident() != self._thread_ident:
                with self._cond:
                    before = self._written
                    self._requested += 1
                    ticket = self._requested
                    self._wake.set()
                    self._cond.wait_for(lambda: self._served >= ticket, timeout)
                    return self._written - before
            if not self._write_lock.acquire(timeout=max(0.0, timeout)):
                return 0
            try:
                return self._write_pending()
            finally:
                self._write_lock.release()
        except Exception:
            return 0

    def close(self) -> None:
        """Flush, stop the thread and remove the handler (a second call does nothing)."""
        if self._closing:
            return
        try:
            if self.handler is not None:
                logging.getLogger().removeHandler(self.handler)
            self._closing = True  # the last write also reports drops of the current minute
            self.flush()
            self._stop.set()
            self._wake.set()
            thread = self._thread
            if thread is not None and threading.get_ident() != self._thread_ident:
                thread.join(timeout=0.5)
        except Exception:
            return

    def queued(self) -> int:
        """Rows waiting for the writer."""
        return self._queue.qsize()

    # --- emit (any thread: never blocks, never raises) -----------------------------------------------------

    def _emit(self, record: logging.LogRecord) -> None:
        try:
            if getattr(self._local, "busy", False) or threading.get_ident() == self._thread_ident:
                return
            row = self._row(record)
            if row is None:
                return
            try:
                self._queue.put_nowait(row)
            except queue.Full:
                self._count_dropped(1)
        except Exception:
            return

    def _row(self, record: logging.LogRecord) -> _Row | None:
        if record.levelno < _LEVELS[self.level] or _skipped_logger(record.name):
            return None
        fields: dict[str, Any]
        event_dict: Mapping[str, Any] | None = None
        if isinstance(record.msg, Mapping):  # a structlog event (ProcessorFormatter.wrap_for_formatter)
            event_dict = dict(record.msg)
            if event_dict.get(_EVENT_LOGGED) is True:
                return None
            logger = str(event_dict.get("logger") or record.name)
            if _skipped_logger(logger):
                return None
            event = str(event_dict.get("event", ""))
            fields = {k: v for k, v in event_dict.items() if k not in _META_KEYS and k != _EVENT_LOGGED}
        else:
            if getattr(record, _EVENT_LOGGED, False) is True:
                return None
            logger = record.name
            try:
                event = record.getMessage()
            except Exception:
                event = str(record.msg)
            fields = {
                k: v
                for k, v in record.__dict__.items()
                if k not in _RECORD_ATTRS and not k.startswith("_") and k != _EVENT_LOGGED
            }
        data: dict[str, Any] = {**_exception_fields(record, event_dict), **_masked(fields)}
        return _Row(
            ts=self._clock.now(),
            level="critical" if record.levelno >= logging.CRITICAL else "error",
            logger=logger,
            event=event,
            message=redact_text(f"{logger}: {event}")[:MESSAGE_MAX],
            data=_cut(data) if data else None,
        )

    def _count_dropped(self, n: int) -> None:
        with self._count_lock:
            self.dropped += n

    # --- writer ---------------------------------------------------------------------------------------------

    def _run(self) -> None:
        self._thread_ident = threading.get_ident()
        while True:
            try:
                self._wake.wait(self.flush_seconds)
                self._wake.clear()
                with self._cond:
                    serving = self._requested
                stopping = self._stop.is_set()
                with self._write_lock:
                    self._write_pending()
                with self._cond:
                    self._served = serving
                    self._cond.notify_all()
                if stopping:
                    return
            except Exception:
                if self._stop.is_set():
                    return

    def _drain(self) -> list[_Row]:
        drained: list[_Row] = []
        while True:
            try:
                drained.append(self._queue.get_nowait())
            except queue.Empty:
                return drained

    def _roll(self, at: datetime, out: list[tuple[EventLog, int]]) -> None:
        """Start a new rate window when a minute has passed, writing the dropped summary first."""
        if self._window_start is None:
            self._window_start = at
            return
        if at - self._window_start >= RATE_WINDOW:
            self._summary(at, out)
            self._window_start = at
            self._window_rows = 0
            if len(self._last_row) > REPEAT_KEYS_MAX:
                self._prune(at)

    def _summary(self, at: datetime, out: list[tuple[EventLog, int]]) -> None:
        """One "log mirror dropped N lines" row for the drops not yet reported (taken back if it fails)."""
        with self._count_lock:
            pending = self.dropped - self._reported
        if pending > 0:
            self._reported += pending
            out.append(
                (
                    self._event(at, "error", f"log mirror dropped {pending} lines", {"dropped": pending}),
                    pending,
                )
            )

    def _prune(self, at: datetime) -> None:
        for key in [k for k, ts in self._last_row.items() if at - ts >= REPEAT_WINDOW]:
            del self._last_row[key]
            self._repeats.pop(key, None)

    def _event(self, ts: datetime, level: str, message: str, data: dict[str, Any] | None) -> EventLog:
        return EventLog(
            ts=ts, level=level, source=self.source, run_id=self.run_id, message=message, data=data
        )

    def _select(self, drained: list[_Row], now: datetime) -> list[tuple[EventLog, int]]:
        """Apply the repeat window and the per-minute limit to the drained rows.

        Each result is (row, the drops it reports): 0 for a mirrored line, N for a summary row."""
        out: list[tuple[EventLog, int]] = []
        for row in drained:
            self._roll(row.ts, out)
            key = (row.logger, row.event)
            last = self._last_row.get(key)
            if last is not None and row.ts - last < REPEAT_WINDOW:
                self._repeats[key] = self._repeats.get(key, 0) + 1
                continue
            if self._window_rows >= self.max_per_minute:
                self._count_dropped(1)
                continue
            data = row.data
            repeated = self._repeats.pop(key, 0)
            if repeated:
                data = {**(data or {}), "repeated": repeated}
            self._last_row[key] = row.ts
            self._window_rows += 1
            out.append((self._event(row.ts, row.level, row.message, data), 0))
        self._roll(now, out)
        if self._closing:
            self._summary(now, out)  # the last write: report this minute's drops now
        return out

    def _write_pending(self) -> int:
        """Drain the queue and write one batch (called with _write_lock held). Never raises or logs."""
        self._local.busy = True
        try:
            out = self._select(self._drain(), self._clock.now())
            if not out:
                return 0
            try:
                with self._factory() as session:
                    session.add_all([row for row, _ in out])
                    session.commit()
            except Exception:
                self._reported -= sum(reports for _, reports in out)
                self._count_dropped(sum(1 for _, reports in out if not reports))
                return 0
            with self._cond:
                self._written += len(out)
            return len(out)
        except Exception:
            return 0
        finally:
            self._local.busy = False


def install_event_mirror(
    factory: sessionmaker[Session],
    clock: Clock,
    process: str,
    settings: RuntimeSettings,
    *,
    run_id: int | None = None,
) -> EventLogMirror | None:
    """None when `logging.mirror_level` is `off`; else an installed and started mirror."""
    level = settings.logging_mirror_level
    if level == "off":
        return None
    mirror = EventLogMirror(
        factory,
        clock,
        process=process,
        level=level,
        max_per_minute=settings.logging_mirror_max_per_minute,
        run_id=run_id,
    )
    mirror.install()
    mirror.start()
    return mirror
