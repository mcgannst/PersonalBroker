"""The web API (Phase 4, SPEC §11): a FastAPI app over the Phase 1-3 services.

`trader.api.main.create_app` builds the app; `trader.api.services.build_services` composes `ApiServices`
from Core; routers live in `trader.api.routers` (registered in order by `ROUTERS`).
"""
