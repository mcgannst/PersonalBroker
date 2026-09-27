"""The broker seam (master plan §7.1): the simulated broker now, a live adapter one day (BRD O6)."""

from collections.abc import Sequence
from datetime import date, datetime
from typing import Protocol

from sqlalchemy.orm import Session

from trader.adapters.questrade.models import QtQuote
from trader.broker.types import FillEvent, OrderSpec


class BrokerRejected(ValueError):
    """The broker refused an order: long-only rule, unknown or closed position, wrong symbol."""


class Broker(Protocol):
    def submit(self, spec: OrderSpec, session: Session | None = None) -> int: ...
    def cancel(self, order_id: int, reason: str, session: Session | None = None) -> bool: ...
    def on_quotes(self, quotes: Sequence[QtQuote], now: datetime) -> list[FillEvent]: ...
    def end_of_session(self, session_date: date) -> list[int]: ...
