"""Mirror `error` / `critical` log lines into `event_log` for the System page (SPEC §2; P5-T14).

Asynchronous and lossy: `emit` only masks the record and queues it (a full queue drops it and counts it); a
daemon thread writes batches. It never blocks, never raises, never recurses, is rate-limited, and its rows
(source `log.<process>`) are never relayed to Telegram.
"""

import logging
from typing import Literal

from sqlalchemy.orm import Session, sessionmaker

from trader.market.clock import Clock
from trader.settings_store import RuntimeSettings

MIRROR_SOURCE_PREFIX = "log."


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

    def install(self) -> None:
        """Add the mirror's handler to the root logger."""
        raise NotImplementedError("P5-T14")

    def start(self) -> None:
        """Start the daemon thread that writes queued rows every `flush_seconds`."""
        raise NotImplementedError("P5-T14")

    def flush(self, timeout: float = 2.0) -> int:
        """Write what is queued now; returns the rows written."""
        raise NotImplementedError("P5-T14")

    def close(self) -> None:
        """Flush, stop the thread and remove the handler."""
        raise NotImplementedError("P5-T14")


def install_event_mirror(
    factory: sessionmaker[Session],
    clock: Clock,
    process: str,
    settings: RuntimeSettings,
    *,
    run_id: int | None = None,
) -> EventLogMirror | None:
    """None when `logging.mirror_level` is `off`; else an installed and started mirror."""
    raise NotImplementedError("P5-T14")
