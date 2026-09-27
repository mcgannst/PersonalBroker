"""P4-T19 acceptance test 2: the smoke seed is idempotent (one pending proposal after two runs), plus its
refusal to seed anything but the smoke database."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from tests.e2e import seed_smoke
from trader.db import models as m
from trader.market.clock import FixedClock

pytestmark = pytest.mark.db

NOW = datetime(2026, 9, 27, 22, 0, tzinfo=UTC)  # a Sunday evening: the seed must not need a session


def _count(s: Session, stmt: object) -> int:
    return int(s.execute(stmt).scalar_one())  # type: ignore[call-overload]


def test_seed_twice_leaves_one_pending_proposal_one_trade_and_the_same_events(
    db_factory: sessionmaker[Session],
) -> None:
    clock = FixedClock(NOW)
    first = seed_smoke.seed(db_factory, clock)
    clock.advance(timedelta(minutes=4))
    second = seed_smoke.seed(db_factory, clock)

    assert second == first
    with db_factory() as s:
        pending = s.execute(select(m.Proposal).where(m.Proposal.status == "pending")).scalars().all()
        assert [p.id for p in pending] == [first.proposal_id]
        assert pending[0].kind == "entry"
        # the re-run moved the expiry to 10 minutes after ITS time
        assert pending[0].expires_at == clock.now() + timedelta(minutes=seed_smoke.PENDING_MINUTES)
        ticker = s.execute(
            select(m.Symbol.ticker)
            .join(m.Signal, m.Signal.symbol_id == m.Symbol.id)
            .where(m.Signal.id == pending[0].signal_id)
        ).scalar_one()
        assert ticker == "AAA"
        assert _count(s, select(func.count()).select_from(m.Trade)) == 1
        trade = s.get(m.Trade, first.trade_id)
        assert trade is not None and trade.position_id == first.position_id
        assert _count(s, select(func.count()).select_from(m.Fill)) == 2
        assert (
            _count(s, select(func.count()).select_from(m.EventLog).where(m.EventLog.source == "smoke")) == 3
        )
        assert _count(s, select(func.count()).select_from(m.Run).where(m.Run.mode == "live")) == 1
        assert s.execute(select(m.StrategyConfig)).first() is not None


def test_a_duplicate_pending_proposal_is_expired_by_the_next_seed(db_factory: sessionmaker[Session]) -> None:
    clock = FixedClock(NOW)
    first = seed_smoke.seed(db_factory, clock)
    with db_factory() as s:
        original = s.get(m.Proposal, first.proposal_id)
        assert original is not None
        dup = m.Proposal(
            run_id=original.run_id, signal_id=original.signal_id, kind="entry",
            order_spec=original.order_spec, qty=original.qty, status="pending", created_at=NOW,
            expires_at=NOW + timedelta(minutes=5), escalations=0,
        )  # fmt: skip
        s.add(dup)
        s.commit()
    seed_smoke.seed(db_factory, clock)
    with db_factory() as s:
        pending = s.execute(select(m.Proposal.id).where(m.Proposal.status == "pending")).scalars().all()
    assert pending == [first.proposal_id]


def test_main_refuses_a_database_other_than_the_smoke_one(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        seed_smoke.main(["--url", "postgresql+psycopg://u:secret-pw@127.0.0.1:5432/trader_dev"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "refusing" in err
    assert "secret-pw" not in err
