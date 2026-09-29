"""GET /api/control -> ControlOut: the Control page's read model (live dashboard design §4, §5.3; plan S15).

Read-only: every action keeps using the existing mutation routes. Never calls Questrade (D8); each part is
isolated (`part_errors`). DB-T1 stub with the final signature; DB-T6 implements it.
"""

from fastapi import APIRouter, Depends

from trader.api.deps import Services, current_user
from trader.api.schemas import ControlOut

# Every route needs a signed-in session (SPEC §14).
router = APIRouter(tags=["control"], dependencies=[Depends(current_user)])


@router.get("/control", response_model=ControlOut)
async def get_control(services: Services) -> ControlOut:
    raise NotImplementedError("DB-T6")
