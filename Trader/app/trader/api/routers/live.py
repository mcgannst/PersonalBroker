"""GET /api/live -> LiveOut: the read-only live monitor (live dashboard design §5.1; plan S14, S15).

`range` selects the equity series (`today` or `run`); `expand` lists up to three open position ids whose
1-minute bars are included. The route never calls Questrade (D8), answers 200 with failed parts as null plus
`part_errors`, and sends `Server-Timing: app;dur=<ms>, <part>;dur=<ms>...`. DB-T1 stub with the final
signature; DB-T5 implements it.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response

from trader.api.deps import Services, current_user
from trader.api.schemas import LiveOut, LiveRange

# Every route needs a signed-in session (SPEC §14).
router = APIRouter(tags=["live"], dependencies=[Depends(current_user)])

EXPAND_PATTERN = r"^[1-9][0-9]{0,18}(,[1-9][0-9]{0,18}){0,2}$"


@router.get("/live", response_model=LiveOut)
async def get_live(
    services: Services,
    response: Response,
    range_: Annotated[LiveRange, Query(alias="range")] = "today",
    expand: Annotated[str | None, Query(max_length=64, pattern=EXPAND_PATTERN)] = None,
) -> LiveOut:
    raise NotImplementedError("DB-T5")
