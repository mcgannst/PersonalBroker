"""OPTSIM T5: the database book passes the contract every `OptionBook` must (`contract_book.BookContract`),
the same one T1 runs against `FakeBook`."""

from collections.abc import Iterator

import pytest
from sqlalchemy.orm import Session, sessionmaker

from tests.options import factories as f
from tests.options.contract_book import BookContract, BookEnv
from trader.broker.ledger import Ledger
from trader.db import models as m
from trader.market.calendar import SessionCalendar
from trader.market.clock import FixedClock
from trader.options.account import lock_book
from trader.options.book import DbBook, contract_view

pytestmark = pytest.mark.db


class TestDbBookPassesTheBookContract(BookContract):
    @pytest.fixture
    def book_env(self, db_factory: sessionmaker[Session]) -> Iterator[BookEnv]:
        with db_factory() as s:
            run_id = f.add_options_run(s, self.START_CASH)
            symbol_id = f.add_underlying(s, "F")
            put = s.get_one(m.OptionContract, f.add_contract(s, symbol_id, strike="14.50", right="put"))
            call = s.get_one(m.OptionContract, f.add_contract(s, symbol_id, strike="15.00", right="call"))
            s.commit()
            lock_book(s, run_id)  # as every writer does; released when the transaction ends
            book = DbBook(s, run_id, Ledger(SessionCalendar()), FixedClock(f.T0))
            yield BookEnv(book, contract_view(put), contract_view(call))
            s.rollback()
