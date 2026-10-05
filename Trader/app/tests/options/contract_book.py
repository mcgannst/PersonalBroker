"""`BookContract`: the behaviour every `OptionBook` must have (OPTSIM task plan §3.10). T1 runs it against
`FakeBook`, T5 against the database book.

Use: subclass it in a test module and give it a `book_env` fixture (the class is not collected by itself):

    class TestFakeBook(BookContract):
        @pytest.fixture
        def book_env(self) -> BookEnv:
            ...

The fixture yields a `BookEnv`: an EMPTY book holding `BookContract.START_CASH` (5,000) of cash, nothing
reserved and no structure, plus one put and one call contract (multiplier 100) on `underlying` that the book
knows. The numbers below divide exactly at 4 decimals, so a book that stores numeric(14,4) passes unchanged.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from trader.options.protocols import OptionBook
from trader.options.types import OptionContract, StructureKind

T0 = datetime(2026, 10, 6, 14, 0, tzinfo=UTC)
T1 = T0 + timedelta(minutes=5)
DAY = date(2026, 10, 6)


def D(text: str) -> Decimal:
    return Decimal(text)


@dataclass
class BookEnv:
    book: OptionBook
    put: OptionContract
    call: OptionContract
    underlying: str = "F"


class BookContract:
    START_CASH = Decimal("5000")

    @pytest.fixture
    def book_env(self) -> BookEnv:
        raise NotImplementedError("give the subclass a `book_env` fixture")

    @staticmethod
    def _add(env: BookEnv, kind: StructureKind = "csp", **over: Any) -> int:
        values: dict[str, Any] = {
            "kind": kind,
            "source": "manual",
            "strategy_config_id": None,
            "underlying": env.underlying,
            "qty": 1,
            "entry_net": D("0.45"),
            "reserved_cash": D("0"),
            "cover_structure_id": None,
            "parent_structure_id": None,
            "take_profit_net": None,
            "meta": {},
            "ts": T0,
        }
        return env.book.add_structure(**{**values, **over})

    def test_a_new_book_has_its_cash_and_nothing_else(self, book_env: BookEnv) -> None:
        book = book_env.book
        assert book.cash() == self.START_CASH
        assert book.reserved() == 0
        assert book.structures() == [] and book.structures(open_only=False) == []

    def test_add_structure_is_open_and_listed_by_its_filters(self, book_env: BookEnv) -> None:
        book = book_env.book
        first = self._add(book_env, reserved_cash=D("1450"), take_profit_net=D("-0.225"), meta={"note": "x"})
        second = self._add(book_env, "long_call", source="toy_call", entry_net=D("-1.20"))
        view = book.structure(first)
        assert (view.id, view.kind, view.source, view.underlying) == (first, "csp", "manual", "F")
        assert (view.state, view.close_reason, view.frozen, view.closed_at) == ("open", None, False, None)
        assert (view.qty, view.entry_net, view.reserved_cash) == (1, D("0.45"), D("1450"))
        assert (view.take_profit_net, view.realized_pnl, view.fees_total) == (D("-0.225"), 0, 0)
        assert view.positions == () and view.opened_at == T0 and view.meta == {"note": "x"}
        assert [s.id for s in book.structures()] == [first, second]
        assert [s.id for s in book.structures(source="toy_call")] == [second]
        assert [s.id for s in book.structures(underlying="F")] == [first, second]
        assert book.structures(underlying="ZZZ") == []

    def test_apply_opens_signed_positions_and_never_moves_cash(self, book_env: BookEnv) -> None:
        book = book_env.book
        sid = self._add(book_env, "custom")
        short = book.apply(sid, "option", book_env.put.id, -2, D("0.45"), T0)
        long = book.apply(sid, "option", book_env.call.id, 1, D("1.20"), T0)
        assert (short.qty, short.avg_price, short.realized_pnl) == (-2, D("0.45"), 0)
        assert (short.instrument, short.contract, short.structure_id) == ("option", book_env.put, sid)
        assert (long.qty, long.avg_price, long.underlying) == (1, D("1.20"), "F")
        assert [p.id for p in book.structure(sid).positions] == [short.id, long.id]
        assert book.cash() == self.START_CASH

    def test_adding_to_a_position_re_averages_its_price(self, book_env: BookEnv) -> None:
        book = book_env.book
        sid = self._add(book_env)
        book.apply(sid, "option", book_env.put.id, -1, D("0.40"), T0)
        more = book.apply(sid, "option", book_env.put.id, -3, D("0.60"), T1)
        assert (more.qty, more.avg_price, more.realized_pnl) == (-4, D("0.55"), 0)
        assert len(book.structure(sid).positions) == 1

    def test_reducing_a_long_realizes_price_minus_average(self, book_env: BookEnv) -> None:
        book = book_env.book
        sid = self._add(book_env, "long_call", entry_net=D("-1.20"))
        book.apply(sid, "option", book_env.call.id, 2, D("1.20"), T0)
        part = book.apply(sid, "option", book_env.call.id, -1, D("1.50"), T1)
        assert (part.qty, part.avg_price, part.realized_pnl) == (1, D("1.20"), D("30"))  # 0.30 x 1 x 100
        done = book.apply(sid, "option", book_env.call.id, -1, D("0.70"), T1)
        assert (done.qty, done.realized_pnl) == (0, D("-20"))  # 30 - 0.50 x 100
        view = book.structure(sid)
        assert view.realized_pnl == D("-20")
        assert [(p.id, p.qty) for p in view.positions] == [(done.id, 0)]  # a closed position stays listed

    def test_reducing_a_short_realizes_average_minus_price(self, book_env: BookEnv) -> None:
        book = book_env.book
        sid = self._add(book_env)
        book.apply(sid, "option", book_env.put.id, -2, D("0.45"), T0)
        closed = book.apply(sid, "option", book_env.put.id, 2, D("0.20"), T1)
        assert (closed.qty, closed.realized_pnl) == (0, D("50"))  # 0.25 x 2 x 100
        assert book.structure(sid).realized_pnl == D("50")

    def test_crossing_through_zero_is_refused_and_changes_nothing(self, book_env: BookEnv) -> None:
        book = book_env.book
        sid = self._add(book_env)
        book.apply(sid, "option", book_env.put.id, -1, D("0.45"), T0)
        with pytest.raises(ValueError):
            book.apply(sid, "option", book_env.put.id, 2, D("0.20"), T1)
        (position,) = book.structure(sid).positions
        assert (position.qty, position.avg_price, position.realized_pnl) == (-1, D("0.45"), 0)

    def test_shares_have_no_contract_and_a_multiplier_of_one(self, book_env: BookEnv) -> None:
        book = book_env.book
        sid = self._add(book_env, "shares", entry_net=D("-14.50"))
        held = book.apply(sid, "shares", None, 100, D("14.50"), T0)
        assert (held.instrument, held.contract, held.qty, held.avg_price) == ("shares", None, 100, D("14.50"))
        sold = book.apply(sid, "shares", None, -100, D("15.00"), T1)
        assert (sold.qty, sold.realized_pnl) == (0, D("50"))  # 0.50 x 100 x 1

    def test_move_cash_follows_the_ledger_signs_and_fees_reach_the_structure(self, book_env: BookEnv) -> None:
        book = book_env.book
        sid = self._add(book_env)
        book.move_cash(D("45"), "sell", "opt_fill:1", T0, structure_id=sid)
        book.move_cash(D("-120"), "buy", "opt_fill:2", T0)
        book.move_cash(D("-0.99"), "fee", "opt_fill:1", T0, structure_id=sid)
        assert book.cash() == self.START_CASH + D("45") - D("120") - D("0.99")
        assert book.structure(sid).fees_total == D("0.99")
        for amount, kind in ((D("-45"), "sell"), (D("120"), "buy"), (D("0.99"), "fee"), (D("0"), "sell")):
            with pytest.raises(ValueError):
                book.move_cash(amount, kind, "opt_fill:3", T0)  # type: ignore[arg-type]
        assert book.cash() == self.START_CASH + D("45") - D("120") - D("0.99")

    def test_reserved_is_the_open_structures_and_closing_releases_it(self, book_env: BookEnv) -> None:
        book = book_env.book
        first = self._add(book_env, reserved_cash=D("1450"))
        second = self._add(book_env, reserved_cash=D("500"))
        assert book.reserved() == D("1950")
        book.set_reserved(second, D("700"))
        assert (book.structure(second).reserved_cash, book.reserved()) == (D("700"), D("2150"))
        book.close_structure(first, "expired", T1)
        closed = book.structure(first)
        assert (closed.state, closed.close_reason, closed.closed_at) == ("closed", "expired", T1)
        assert closed.reserved_cash == 0 and book.reserved() == D("700")
        assert [s.id for s in book.structures()] == [second]
        assert [s.id for s in book.structures(open_only=False)] == [first, second]
        assert book.cash() == self.START_CASH  # reserving never moves cash

    def test_freeze_marks_the_structure_and_keeps_it_open(self, book_env: BookEnv) -> None:
        book = book_env.book
        sid = self._add(book_env)
        book.freeze(sid, "contract adjusted")
        view = book.structure(sid)
        assert (view.frozen, view.state) == (True, "open")

    def test_a_lifecycle_event_is_recorded_once_per_position_kind_and_session(
        self, book_env: BookEnv
    ) -> None:
        book = book_env.book
        sid = self._add(book_env)
        position = book.apply(sid, "option", book_env.put.id, -1, D("0.45"), T0)
        event: dict[str, Any] = {
            "structure_id": sid,
            "position_id": position.id,
            "contract_id": book_env.put.id,
            "session_date": DAY,
            "ts": T1,
            "underlying_close": D("14.10"),
            "strike": D("14.50"),
            "qty": 1,
            "shares_delta": 100,
            "cash_delta": D("-1450"),
            "detail": {"settled": "shares"},
        }
        first = book.record_lifecycle(kind="assigned", **event)
        assert isinstance(first, int)
        assert book.record_lifecycle(kind="assigned", **event) is None
        other_kind = book.record_lifecycle(kind="frozen", **event)
        other_day = book.record_lifecycle(kind="assigned", **{**event, "session_date": DAY + timedelta(1)})
        assert len({first, other_kind, other_day}) == 3 and None not in (other_kind, other_day)
        shares = self._add(book_env, "shares", parent_structure_id=sid, entry_net=D("-14.50"))
        book.link_lifecycle(first, shares)
        assert book.structure(shares).parent_structure_id == sid
