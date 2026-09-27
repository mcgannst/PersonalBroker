"""The worker's Telegram side: long-poll updates from the configured chat only, verify signed single-use
callbacks, approvals through ProposalService.decide, commands, the pause confirmation, journal answers,
and proposal messages (BR-31, BR-34, BR-60; SPEC §4.4, §14).

P3-T1 stub: the contracts are final, P3-T6 implements them.
"""

import asyncio
from collections.abc import Awaitable, Callable

from sqlalchemy.orm import Session, sessionmaker

from trader.adapters.telegram.callbacks import CallbackSigner, DbCallbackIssuer
from trader.adapters.telegram.types import CommandHandler, TelegramApi, Update
from trader.engine.proposals import Decision, DecisionResult, Via
from trader.market.clock import Clock
from trader.notify.types import Renderer
from trader.settings_store import RuntimeSettings


class TelegramBot:
    """Implements trader.adapters.telegram.types.ProposalMessenger. `decide` has
    ProposalService.decide's signature: (proposal_id, decision, via, actor)."""

    def __init__(
        self,
        api: TelegramApi,
        chat_id: int,
        factory: sessionmaker[Session],
        clock: Clock,
        issuer: DbCallbackIssuer,
        signer: CallbackSigner,
        decide: Callable[[int, Decision, Via, str], DecisionResult],
        commands: CommandHandler,
        render: Renderer,
        run_id: int,
        *,
        settings: Callable[[], RuntimeSettings],
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.api = api
        self.chat_id = chat_id
        self.factory = factory
        self.clock = clock
        self.issuer = issuer
        self.signer = signer
        self.decide = decide
        self.commands = commands
        self.render = render
        self.run_id = run_id
        self.settings = settings
        self.sleep = sleep

    async def run(self, stop: asyncio.Event) -> None:
        raise NotImplementedError("P3-T6")

    async def handle_update(self, update: Update) -> None:
        raise NotImplementedError("P3-T6")

    async def send_proposal(self, proposal_id: int, *, resend: bool = False) -> bool:
        raise NotImplementedError("P3-T6")

    async def sync_closed(self) -> int:
        raise NotImplementedError("P3-T6")
