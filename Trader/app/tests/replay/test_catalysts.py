"""P5-T5: the replay's read-only catalyst source (SPEC §8 `replay_catalyst_mode`)."""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from tests.fakes_replay import seed_replay_world
from trader.adapters.claude.catalyst import StoredCatalyst
from trader.db import models as m
from trader.replay.catalysts import UNKNOWN_REASON, ReplayCatalysts
from trader.strategies.base import CatalystSource

pytestmark = pytest.mark.db
DAY = date(2026, 11, 17)


def _unknown(sid: int) -> StoredCatalyst:
    return StoredCatalyst(sid, "unknown", "neutral", None, None, UNKNOWN_REASON, None, Decimal(0), False)


def _catalyst_rows(factory: sessionmaker[Session]) -> int:
    with factory() as s:
        return s.execute(select(func.count()).select_from(m.Catalyst)).scalar_one()


async def test_stored_mode_returns_stored_rows_and_unknown_for_missing(
    db_factory: sessionmaker[Session],
) -> None:
    w = seed_replay_world(db_factory, catalysts={("AAA", DAY): "earnings"}, strategies=False)
    aaa, bbb = w.symbols["AAA"], w.symbols["BBB"]
    source: CatalystSource = ReplayCatalysts(db_factory, "stored")

    got = await source.get([bbb, aaa], DAY)

    stored = got[aaa]
    assert isinstance(stored, StoredCatalyst)
    assert (stored.catalyst_type, stored.direction, stored.quality, stored.classified) == (
        "earnings",
        "positive",
        4,
        True,
    )
    assert stored.reason == "seeded"
    assert got[bbb] == _unknown(bbb)
    assert list(got) == sorted(got)  # ids sorted (determinism)
    # another day: the stored row belongs to DAY only
    other = await source.get([aaa], date(2026, 11, 18))
    assert other == {aaa: _unknown(aaa)}
    assert _catalyst_rows(db_factory) == 1  # nothing written


async def test_unknown_mode_reports_every_name_unknown(db_factory: sessionmaker[Session]) -> None:
    w = seed_replay_world(db_factory, catalysts={("AAA", DAY): "earnings"}, strategies=False)
    aaa, bbb = w.symbols["AAA"], w.symbols["BBB"]

    got = await ReplayCatalysts(db_factory, "unknown").get([aaa, bbb], DAY)

    assert got == {aaa: _unknown(aaa), bbb: _unknown(bbb)}
    assert not any(c.classified for c in got.values())
    assert _catalyst_rows(db_factory) == 1


async def test_empty_request(db_factory: sessionmaker[Session]) -> None:
    assert await ReplayCatalysts(db_factory, "stored").get([], DAY) == {}
