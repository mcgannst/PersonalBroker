"""Composes `ApiServices` from Core for the API process (registry, kill switches, the Questrade token store,
the guarded decider, the notifier, cached quotes, candles, the job launcher, the change feed, the day plan).

Stub (P4-T1): T18 implements it. It opens nothing eagerly that needs the network.
"""

from contextlib import AsyncExitStack

from trader.api.deps import ApiServices
from trader.bootstrap import Core


async def build_services(core: Core, stack: AsyncExitStack) -> ApiServices:
    raise NotImplementedError("P4-T18")
