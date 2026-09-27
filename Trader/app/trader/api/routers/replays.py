"""Replay routes (SPEC §11 `/replays`, §12 Replay; P5-T7): `GET /api/replays/options`, `GET /api/replays`,
`GET /api/replays/{id}`, `POST /api/replays` (202) and `POST /api/replays/{id}/cancel`.

P5-T1 registers this router with no routes; P5-T7 adds them.
"""

from fastapi import APIRouter

router = APIRouter(tags=["replays"])
