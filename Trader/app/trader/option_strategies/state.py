"""A plug-in's own key-value state (OPTSIM task plan T7): table `option_strategy_state`, scoped to one
strategy key, so a plug-in can never read or change another plug-in's state. Each call is its own short
transaction; `put` replaces the value and counts the row's version up."""

from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from trader.db import models as m
from trader.db.session import session_scope
from trader.market.clock import Clock

State = m.OptionStrategyState


class DbStrategyState:
    """`StrategyState` over the database."""

    def __init__(self, factory: sessionmaker[Session], clock: Clock, strategy_key: str) -> None:
        self._factory = factory
        self._clock = clock
        self._key = strategy_key

    def get(self, scope_key: str) -> dict[str, Any] | None:
        with self._factory() as s:
            value = s.execute(
                select(State.value).where(State.strategy_key == self._key, State.scope_key == scope_key)
            ).scalar_one_or_none()
            return None if value is None else dict(value)

    def put(self, scope_key: str, value: dict[str, Any]) -> None:
        now = self._clock.now()
        stmt = insert(State).values(
            strategy_key=self._key, scope_key=scope_key, value=dict(value), version=1, updated_at=now
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[State.strategy_key, State.scope_key],
            set_={"value": stmt.excluded.value, "version": State.version + 1, "updated_at": now},
        )
        with session_scope(self._factory) as s:
            s.execute(stmt)

    def delete(self, scope_key: str) -> None:
        with session_scope(self._factory) as s:
            s.execute(delete(State).where(State.strategy_key == self._key, State.scope_key == scope_key))
