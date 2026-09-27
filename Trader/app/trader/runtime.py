"""Composition root for Phase 3: builds the notifier, renderer, callback signer, decider, session engine,
event firing and the worker from Core (SPEC §1, §9, §13).

P3-T1 stub: the contracts are final, P3-T12 implements them.
"""

from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from datetime import date
from typing import Any

from trader.adapters.telegram.callbacks import CallbackSigner
from trader.bootstrap import Core
from trader.engine.orchestrator import Engine
from trader.engine.proposals import Decision, DecisionResult, Via
from trader.engine.scheduler import EventRunner, FireDeps
from trader.jobs.runner import JobOutcome
from trader.notify.messages import MessageRenderer
from trader.notify.types import Notifier


def build_notifier(core: Core) -> Notifier:
    """TelegramNotifier when TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set, else NullNotifier."""
    raise NotImplementedError("P3-T12")


def build_renderer(core: Core) -> MessageRenderer:
    raise NotImplementedError("P3-T12")


def build_signer(core: Core) -> CallbackSigner:
    raise NotImplementedError("P3-T12")


def build_decider(core: Core, run_id: int) -> Callable[[int, Decision, Via, str], DecisionResult]:
    raise NotImplementedError("P3-T12")


async def open_engine(core: Core, stack: AsyncExitStack) -> Engine:
    raise NotImplementedError("P3-T12")


def fire_deps(core: Core, runner_factory: Callable[[], Awaitable[EventRunner]]) -> FireDeps:
    raise NotImplementedError("P3-T12")


async def run_worker(once: bool = False) -> int:
    raise NotImplementedError("P3-T12")


async def run_cli_job(
    core: Core,
    job: str,
    session_date: date,
    body: Callable[[], Awaitable[dict[str, Any]]],
    *,
    force: bool,
) -> JobOutcome:
    raise NotImplementedError("P3-T12")
