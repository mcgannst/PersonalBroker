"""SIZECAP breaker (checker lane, e12c363): the per-stock cap `max_position_pct` and `trader live-run new`.

Sizing: qty never exceeds any of the three limits (risk, cap, cash), one of them binds, the capped cost holds
through the fill model's real fills (limit, stop-limit, market at >= $2) including commission and ECN, the cap
uses total equity (positions and unsettled cash) while cash still limits, and ten 10% entries can't spend more
than the settled cash. `live-run new`: DST and early-close boundaries of the 09:15-16:30 ET block, a second
confirm, the worker's LiveRunWatch, --set validation of the new key, and atomicity (strict xfail: see the
check log). Golden: the regenerated orb_week changed only in size.

Known gaps pinned as strict xfails (they flip to XPASS, and fail, when fixed):
- a `stop` entry that gaps above its stop fills above the cap (the cap is on the estimated cost);
- a market entry under $2 fills above the cap (slippage_min 0.01 > the 0.5% buffer);
- `live-run new` writes --set/--strategy-param before the run starts, in separate transactions.
"""

import asyncio
import json
import random
import re
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, date, datetime, time
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

import trader.bootstrap
import trader.engine.runs as runs_mod
from tests.factories import add_symbol
from tests.fakes_api import test_core
from trader.bootstrap import Core
from trader.broker.fill_model import FillParams, QuoteFillModel
from trader.broker.types import AccountState, NoFill
from trader.cli import app
from trader.db import models as m
from trader.engine.risk import Rejection, RiskContext, RiskManager, SizedOrder
from trader.engine.runs import get_live_run, reset_block_reason, sim_account, start_new_live_run
from trader.market.calendar import SessionCalendar
from trader.market.clock import ET, FixedClock
from trader.runtime import LiveRunWatch
from trader.settings_store import RuntimeSettings, SettingsStore
from trader.strategies.base import EnterLong
from trader.strategies.registry import StrategyRegistry

CAL = SessionCalendar()
RISK = RiskManager(CAL)
DAY = date(2026, 10, 6)
T = datetime(2026, 10, 6, 13, 40, tzinfo=UTC)  # 09:40 ET
runner = CliRunner()


def _acct(
    equity: Decimal, bp: Decimal, total: Decimal | None = None, value: Decimal = Decimal(0)
) -> AccountState:
    return AccountState(total if total is not None else bp, bp, bp, value, equity)


def _ctx(settings: RuntimeSettings, account: AccountState, **over: Any) -> RiskContext:
    base = RiskContext(
        now=T,
        session_date=DAY,
        settings=settings,
        account=account,
        positions={},
        orders={},
        strategy_config_id=1,
        symbol_market="US",
        symbol="XYZ",
    )
    return replace(base, **over)


def _stop(price: Decimal, stop_loss: Decimal) -> EnterLong:
    return EnterLong(7, "stop", price, None, stop_loss, "orb_breakout", {})


def _floor(x: Decimal) -> int:
    return int(x.to_integral_value(rounding=ROUND_FLOOR))


def _cap_unit(s: RuntimeSettings, entry: Decimal) -> Decimal:
    return entry * (1 + s.slippage_buffer) + (s.fees_ecn_per_share if s.fees_direct_route else 0)


# --- 1. property: qty within all three limits, one binds, Decimal only ----------------------------------
def test_property_qty_within_every_limit_and_one_binds() -> None:
    rng = random.Random(20260929)
    sized = rejected_cap = rejected_zero = 0
    for _ in range(3000):
        s = RuntimeSettings(
            max_position_pct=rng.choice(
                [Decimal("0.0001"), Decimal("0.01"), Decimal("0.10"), Decimal("0.25"), Decimal("1")]
            ),
            risk_pct=rng.choice([Decimal("0.005"), Decimal("0.02"), Decimal("0.10")]),
            slippage_buffer=rng.choice([Decimal("0"), Decimal("0.005"), Decimal("0.05")]),
            fees_commission=rng.choice([Decimal("0"), Decimal("4.95")]),
            fees_direct_route=rng.choice([True, False]),
            fees_ecn_per_share=Decimal("0.0035"),
        )
        equity = Decimal(rng.randint(1_00, 100_000_000)) / 100
        bp = (equity * Decimal(rng.randint(1, 150)) / 100).quantize(Decimal("0.0001"))
        entry = Decimal(rng.randint(5, 300_000)) / 100
        dist = max(Decimal("0.01"), (entry * Decimal(rng.randint(1, 49)) / 100).quantize(Decimal("0.01")))
        if dist >= entry:
            continue
        out = RISK.evaluate(_stop(entry, entry - dist), _ctx(s, _acct(equity, bp)))
        cap = equity * s.max_position_pct
        unit = _cap_unit(s, entry)
        risk_dollars = equity * s.risk_pct
        cash_unit = entry * (1 + s.slippage_buffer)
        if isinstance(out, SizedOrder):
            sized += 1
            q = out.qty
            assert isinstance(q, int) and q >= 1 and out.spec is not None and out.spec.qty == q
            assert q * unit + s.fees_commission <= cap  # rounding never goes over the cap
            assert q * cash_unit <= bp
            assert q * dist <= risk_dollars
            # at least one limit binds: one more share breaks one of them
            assert (
                (q + 1) * unit + s.fees_commission > cap
                or (q + 1) * cash_unit > bp
                or (q + 1) * dist > risk_dollars
            )
            z = out.sizing
            assert q == min(int(z["shares_risk"]), int(z["shares_cap"]), int(z["shares_cash"]))
            assert all(isinstance(v, str | bool) for v in z.values())
            assert "e" not in z["shares_cap"].lower() and "." not in z["shares_cap"]
            lim = {"risk": int(z["shares_risk"]), "cash": int(z["shares_cash"]), "cap": int(z["shares_cap"])}
            assert lim[z["limited_by"]] == q
        else:
            assert isinstance(out, Rejection)
            assert out.check in ("position_cap", "zero_shares"), out
            if out.check == "position_cap":
                rejected_cap += 1
                assert unit + s.fees_commission > cap
            else:
                rejected_zero += 1
                assert unit + s.fees_commission <= cap
    assert sized > 500 and rejected_cap > 20 and rejected_zero > 5


# --- 2. real fills through the fill model stay within the cap -------------------------------------------
COSTLY = RuntimeSettings(fees_commission=Decimal("4.95"), fees_direct_route=True)


def _fill_cost(s: RuntimeSettings, out: SizedOrder, ask: Decimal) -> Decimal | None:
    model = QuoteFillModel(FillParams.from_settings(s))
    assert out.spec is not None
    priced = model._buy(out.spec, ask, None)
    if isinstance(priced, NoFill):
        return None
    price = priced[0].quantize(Decimal("0.0001"))
    fees = model.fees("buy", out.qty, price)
    return out.qty * price + fees.commission + fees.ecn + fees.sec


@pytest.mark.parametrize("s", [RuntimeSettings(), COSTLY], ids=["default_fees", "commission_ecn"])
def test_limit_stop_limit_and_market_fills_cost_at_most_the_cap(s: RuntimeSettings) -> None:
    equity = Decimal("720")
    cap = equity * s.max_position_pct
    for cents in range(200, 7000, 37):
        px = Decimal(cents) / 100
        acct = _acct(equity, Decimal("100000"))
        # limit entry; the ask gapped down through the limit fills at the limit, never above
        lim = RISK.evaluate(EnterLong(7, "limit", None, px, px * Decimal("0.9"), "t", {}), _ctx(s, acct))
        if isinstance(lim, SizedOrder):
            for ask in (px, px * Decimal("0.97")):
                cost = _fill_cost(s, lim, ask.quantize(Decimal("0.01")))
                assert cost is not None and cost <= cap, (px, ask, cost)
        # stop-limit: any fill is at most the limit
        sl = EnterLong(7, "stop_limit", px * Decimal("0.99"), px, px * Decimal("0.9"), "t", {})
        out = RISK.evaluate(sl, _ctx(s, acct))
        if isinstance(out, SizedOrder):
            for ask in (px * Decimal("0.99"), px * Decimal("1.03")):
                cost = _fill_cost(s, out, ask.quantize(Decimal("0.01")))
                assert cost is None or cost <= cap, (px, ask, cost)
        # market at the reference ask the engine sized from (price >= $2: the slippage fits the buffer)
        mk = EnterLong(7, "market", None, None, px * Decimal("0.9"), "t", {})
        out = RISK.evaluate(mk, _ctx(s, acct, reference_price=px))
        if isinstance(out, SizedOrder):
            cost = _fill_cost(s, out, px)
            assert cost is not None and cost <= cap, (px, cost)


@pytest.mark.xfail(
    strict=True,
    reason="SIZECAP should-fix: the cap is on the estimated cost; a stop entry that gaps 3% over its stop "
    "(or a market entry under $2, slippage_min 0.01 > 0.5% buffer) fills above equity x max_position_pct",
)
def test_gap_through_stop_and_sub_two_dollar_market_fills_stay_within_the_cap() -> None:
    s = RuntimeSettings()
    equity = Decimal("720")
    cap = equity * s.max_position_pct
    over = []
    # equity 1005: the cap is 100.50 and exactly 10 shares at 10.00 x 1.005 fit; the ask gaps to 10.30
    tight = RISK.evaluate(
        _stop(Decimal("10.00"), Decimal("9.00")), _ctx(s, _acct(Decimal("1005"), Decimal("1005")))
    )
    assert isinstance(tight, SizedOrder) and tight.qty == 10
    gap = _fill_cost(s, tight, Decimal("10.30"))
    assert gap is not None
    over.append(gap - Decimal("1005") * s.max_position_pct + cap)  # the overshoot, against the 72 cap
    mk = EnterLong(7, "market", None, None, Decimal("0.40"), "t", {})
    cheap = RISK.evaluate(mk, _ctx(s, _acct(equity, Decimal("720")), reference_price=Decimal("0.50")))
    assert isinstance(cheap, SizedOrder) and cheap.qty == 143
    over.append(_fill_cost(s, cheap, Decimal("0.50")))
    assert all(c is not None and c <= cap for c in over), over


# --- 3. the cap uses total equity; cash still limits ----------------------------------------------------
def test_cap_uses_total_equity_with_positions_and_unsettled_cash_while_cash_limits() -> None:
    s = RuntimeSettings()
    # equity 1000 = 300 cash (only 50 settled, T+1) + 700 of open positions
    tight = _acct(Decimal("1000"), Decimal("50"), total=Decimal("300"), value=Decimal("700"))
    out = RISK.evaluate(_stop(Decimal("10"), Decimal("9")), _ctx(s, tight))
    assert isinstance(out, SizedOrder) and out.qty == 4 and out.sizing["limited_by"] == "cash"
    assert Decimal(out.sizing["cap_dollars"]) == Decimal("100") and out.sizing["shares_cap"] == "9"
    # cash mode off: buying power is all 300 of cash; the cap (10% of 1000, not of 300) binds at 9
    loose = _acct(Decimal("1000"), Decimal("300"), total=Decimal("300"), value=Decimal("700"))
    out = RISK.evaluate(_stop(Decimal("10"), Decimal("9")), _ctx(s, loose))
    assert isinstance(out, SizedOrder) and out.qty == 9 and out.sizing["limited_by"] == "cap"


def test_ten_ten_percent_entries_never_spend_more_than_settled_cash() -> None:
    s = RuntimeSettings()
    equity, settled = Decimal("720"), Decimal("500")  # 220 unsettled from yesterday's sale
    spent, outcomes = Decimal(0), []
    for _ in range(10):
        out = RISK.evaluate(
            _stop(Decimal("7.00"), Decimal("6.90")), _ctx(s, _acct(equity, settled - spent, total=equity))
        )
        if isinstance(out, SizedOrder):
            spent += out.qty * Decimal("7.00") * (1 + s.slippage_buffer)
            outcomes.append((out.qty, out.sizing["limited_by"]))
        else:
            outcomes.append((0, out.check))
    assert spent <= settled
    assert outcomes[0] == (10, "cap")  # 72 / 7.035 = 10.2
    assert (1, "cash") in outcomes or any(q < 10 and why == "cash" for q, why in outcomes)
    assert outcomes[-1][0] == 0 and outcomes[-1][1] in ("zero_shares", "settled_cash")


# --- 4. the position_cap reason is fixed-form text -------------------------------------------------------
@pytest.mark.parametrize(
    ("pct", "equity", "symbol", "want"),
    [
        ("0.10", "720", "BRK.B", "1 share of BRK.B costs $100.50, over the 10% cap $72.00"),
        ("0.125", "720", "ABC", "1 share of ABC costs $100.50, over the 12.5% cap $90.00"),
        ("0.0001", "10000000", "XYZ", "1 share of XYZ costs $1005.00, over the 0.01% cap $1000.00"),
        ("1", "99.999", None, "1 share of symbol 7 costs $100.50, over the 100% cap $100.00"),
    ],
)
def test_position_cap_reason_is_plain_fixed_form(
    pct: str, equity: str, symbol: str | None, want: str
) -> None:
    s = RuntimeSettings(max_position_pct=Decimal(pct))
    price = Decimal("1000") if pct == "0.0001" else Decimal("100")
    out = RISK.evaluate(
        _stop(price, price - 1), _ctx(s, _acct(Decimal(equity), Decimal("1000000")), symbol=symbol)
    )
    assert isinstance(out, Rejection) and out.check == "position_cap"
    assert out.reason == want
    assert re.fullmatch(
        r"1 share of [\w. ]+ costs \$\d+\.\d\d, over the [\d.]+% cap \$-?\d+\.\d\d", out.reason
    )
    assert "E+" not in out.reason and "\n" not in out.reason


# --- 5. live-run new: the trading-day block across DST and early closes ----------------------------------
def _utc(y: int, mo: int, d: int, hh: int, mm: int) -> datetime:
    return datetime(y, mo, d, hh, mm, tzinfo=UTC)


@pytest.mark.parametrize(
    ("now", "blocked"),
    [
        (_utc(2026, 3, 9, 13, 14), False),  # Mon after DST starts: 09:14 EDT
        (_utc(2026, 3, 9, 13, 15), True),  # 09:15 EDT
        (_utc(2026, 3, 9, 20, 29), True),  # 16:29 EDT
        (_utc(2026, 3, 9, 20, 30), False),  # 16:30 EDT
        (_utc(2026, 11, 2, 14, 14), False),  # Mon after DST ends: 09:14 EST (13:15Z would be 08:15 EST)
        (_utc(2026, 11, 2, 13, 15), False),
        (_utc(2026, 11, 2, 14, 15), True),
        (_utc(2026, 11, 2, 21, 29), True),  # 16:29 EST
        (_utc(2026, 11, 2, 21, 30), False),
        (_utc(2026, 11, 26, 15, 0), False),  # Thanksgiving: not a session
        (_utc(2026, 11, 27, 19, 0), True),  # early close (13:00) day, 14:00 ET: still blocked (conservative)
        (_utc(2026, 11, 27, 21, 30), False),
        (_utc(2026, 12, 24, 21, 29), True),
    ],
)
def test_reset_block_honours_dst_and_early_close_days(now: datetime, blocked: bool) -> None:
    assert (reset_block_reason(CAL, now) is not None) is blocked
    assert (reset_block_reason(CAL, now.astimezone(ET)) is not None) is blocked  # any tz-aware input


# --- 6. live-run new CLI (database) ----------------------------------------------------------------------
NIGHT = datetime.combine(date(2026, 9, 28), time(23, 45), tzinfo=ET).astimezone(UTC)
CAD_ARGS = [
    "--set",
    "starting_cash=1000",
    "--set",
    "starting_cash_currency=CAD",
    "--set",
    "fx.cad_usd_rate=0.72",
    "--set",
    "fx.fee_pct=0.015",
]


@pytest.fixture
def core(db_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch) -> Iterator[Core]:
    c = test_core(db_factory, FixedClock(NIGHT))
    monkeypatch.setattr(trader.bootstrap, "build_core", lambda *a, **k: c)
    yield c


def _old_run(factory: sessionmaker[Session]) -> int:
    return get_live_run(factory, FixedClock(NIGHT), RuntimeSettings()).id


@pytest.mark.db
def test_confirm_twice_leaves_one_active_run_each_with_one_exact_deposit(core: Core) -> None:
    old = _old_run(core.factory)
    first = runner.invoke(app, ["live-run", "new", "--confirm", *CAD_ARGS])
    second = runner.invoke(app, ["live-run", "new", "--confirm", *CAD_ARGS])
    assert first.exit_code == 0 and second.exit_code == 0, (first.output, second.output)
    with core.factory() as s:
        runs = [(r.id, r.status) for r in s.execute(select(m.Run).order_by(m.Run.id)).scalars()]
        audits = (
            s.execute(select(m.AuditLog).where(m.AuditLog.action == "live_run.new").order_by(m.AuditLog.id))
            .scalars()
            .all()
        )
        deposits = {
            r: s.execute(
                select(m.CashLedger.amount).where(m.CashLedger.run_id == r, m.CashLedger.kind == "deposit")
            )
            .scalars()
            .all()
            for r, _ in runs[1:]
        }
    assert [st for _, st in runs] == ["completed", "completed", "active"] and runs[0][0] == old
    assert all(v == [Decimal("709.2000")] for v in deposits.values())  # 1000 x 0.72 x 0.985, exact
    assert len(audits) == 2 and audits[1].after["retired_run_id"] == runs[1][0]
    fx = SettingsStore(core.factory, core.clock.now).load().fx_cad_usd_rate
    assert fx == Decimal("0.72") and str(fx) == "0.72"  # the JSON float came through exactly
    acct = sim_account(core.factory, runs[2][0])
    assert acct is not None and acct.fx_rate == Decimal("0.720000") and acct.fx_fee == Decimal("0.015")


@pytest.mark.db
@pytest.mark.parametrize(
    "bad", ["max_position_pct=0", "max_position_pct=1.5", "max_position_pct=-0.1", "max_position_pct=NaN"]
)
def test_cli_rejects_a_bad_cap_before_writing_anything(core: Core, bad: str) -> None:
    old = _old_run(core.factory)
    result = runner.invoke(app, ["live-run", "new", "--confirm", "--set", "starting_cash=1000", "--set", bad])
    assert result.exit_code == 1
    assert "invalid" in result.output and "max_position_pct" in result.output
    store = SettingsStore(core.factory, core.clock.now).load()
    assert store.starting_cash == Decimal("720") and store.max_position_pct == Decimal("0.10")
    with core.factory() as s:
        assert s.execute(select(func.count()).select_from(m.Run)).scalar_one() == 1
    assert get_live_run(core.factory, core.clock, store).id == old
    ok = runner.invoke(app, ["live-run", "new", "--confirm", "--set", "max_position_pct=0.05"])
    assert ok.exit_code == 0, ok.output
    assert SettingsStore(core.factory, core.clock.now).load().max_position_pct == Decimal("0.05")


@pytest.mark.db
def test_the_workers_live_run_watch_follows_the_new_run(core: Core) -> None:
    old = _old_run(core.factory)
    stop = asyncio.Event()
    watch = LiveRunWatch(core, old, stop)
    assert watch.check(force=True) is True and not stop.is_set()
    new = start_new_live_run(core.factory, core.clock, CAL, RuntimeSettings(), actor="cli:test")
    assert watch.check(force=True) is False
    assert stop.is_set() and watch.changed and watch.current == new.run.id != old


@pytest.mark.db
@pytest.mark.xfail(
    strict=True,
    reason="SIZECAP should-fix: cli.live_run_new writes --set and --strategy-param (own transactions) "
    "before start_new_live_run; a failure or late refusal there leaves the settings changed",
)
@pytest.mark.parametrize("failure", ["crash", "late_refusal"])
def test_a_failure_mid_way_leaves_the_old_run_and_the_settings(
    core: Core, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    old = _old_run(core.factory)
    StrategyRegistry(core.factory, core.clock).ensure_defaults()
    if failure == "crash":

        def boom(*a: Any, **k: Any) -> Any:
            raise RuntimeError("database went away")

        monkeypatch.setattr(runs_mod, "start_new_live_run", boom)
    else:  # a proposal/order appears between the pre-check and the start (the worker was running)
        with core.factory() as s:
            sym = add_symbol(s, "ZZZ")
            s.add(
                m.Order(
                    run_id=old,
                    symbol_id=sym,
                    side="buy",
                    order_type="stop",
                    purpose="entry",
                    qty=3,
                    stop_price=Decimal("20"),
                    tif="day",
                    status="working",
                    reason="orb_breakout",
                    session_date=date(2026, 9, 28),
                    submitted_at=NIGHT,
                    stale_alerted=False,
                )
            )
            s.commit()
        monkeypatch.setattr(runs_mod, "check_new_live_run", lambda *a, **k: None)
    result = runner.invoke(
        app, ["live-run", "new", "--confirm", *CAD_ARGS, "--strategy-param", "orb_sip.max_positions=10"]
    )
    assert result.exit_code == 1
    with core.factory() as s:
        assert s.execute(select(m.Run.status).where(m.Run.id == old)).scalar_one() == "active"
    assert SettingsStore(core.factory, core.clock.now).load().starting_cash == Decimal("720")
    assert StrategyRegistry(core.factory, core.clock).current("orb_sip").params["max_positions"] == 1


# --- 7. the golden replay changed only in size ----------------------------------------------------------
GOLDEN = Path(__file__).resolve().parents[1] / "replay" / "golden" / "orb_week_expected.json"
OLD_QTY = {"BBB": 34, "CCC": 46, "AAA": 66, "DDD": 23}  # e12c363^ (pre-cap, cash-limited)
NEW_QTY = {"BBB": 3, "CCC": 4, "AAA": 6, "DDD": 2}
# e12c363^ fills without qty: (ticker, purpose, order_type, side, price, slippage, reason, ts)
OLD_FILLS = [
    ("BBB", "entry", "stop", "buy", "20.5306", "0.0103", "orb_breakout", "2026-11-23T14:41:00+00:00"),
    ("BBB", "stop", "stop", "sell", "20.3896", "0.0102", "protective_stop", "2026-11-23T14:41:00+00:00"),
    ("CCC", "entry", "stop", "buy", "15.4277", "0.0100", "orb_breakout", "2026-11-24T14:36:00+00:00"),
    ("CCC", "stop", "stop", "sell", "15.0824", "0.0100", "protective_stop", "2026-11-24T15:31:00+00:00"),
    ("AAA", "entry", "stop", "buy", "10.5253", "0.0100", "orb_breakout", "2026-11-25T14:36:00+00:00"),
    ("AAA", "exit", "market", "sell", "10.6846", "0.0100", "overlay_negative", "2026-11-25T20:31:00+00:00"),
    ("DDD", "entry", "stop", "buy", "30.5406", "0.0153", "orb_breakout", "2026-11-27T14:36:00+00:00"),
    ("DDD", "exit", "market", "sell", "30.7692", "0.0154", "flatten_close", "2026-11-27T17:51:00+00:00"),
]
OLD_R = {"BBB": "-1.1726", "CCC": "-2.9364", "AAA": "1.3797", "DDD": "1.7455"}


def test_golden_orb_week_changed_only_in_size() -> None:
    new = json.loads(GOLDEN.read_text())
    fills = [
        (
            f["ticker"],
            f["purpose"],
            f["order_type"],
            f["side"],
            f["price"],
            f["slippage"],
            f["reason"],
            f["ts"],
        )
        for f in new["fills"]
    ]
    assert fills == OLD_FILLS  # prices, slippage, reasons and times identical, in order
    assert {f["ticker"]: f["qty"] for f in new["fills"]} == NEW_QTY
    for t in new["trades"]:
        old = next(f for f in OLD_FILLS if f[0] == t["ticker"] and f[1] == "entry")
        ex = next(f for f in OLD_FILLS if f[0] == t["ticker"] and f[1] != "entry")
        assert (t["entry_price"], t["opened_at"]) == (old[4], old[7])
        assert (t["exit_price"], t["exit_reason"], t["closed_at"]) == (ex[4], ex[6], ex[7])
        assert t["qty"] == NEW_QTY[t["ticker"]] < OLD_QTY[t["ticker"]]
        # R per trade moves only by per-order fee rounding (commission is 0; SEC fee is per share, 4 dp)
        assert abs(Decimal(t["pnl_r"]) - Decimal(OLD_R[t["ticker"]])) <= Decimal("0.0002")
    # each entry's filled cost is within 10% of the equity before it (720 less earlier realized P&L)
    equity = Decimal("720")
    for t in new["trades"]:
        assert NEW_QTY[t["ticker"]] * Decimal(t["entry_price"]) <= equity * Decimal("0.10")
        equity += Decimal(t["pnl"])
    mt = new["metrics"]
    assert (mt["trades"], mt["wins"], mt["losses"], mt["win_rate"]) == (4, 2, 2, "0.5000")
    assert (mt["avg_win_r"], mt["avg_loss_r"], mt["expectancy_r"]) == ("1.5626", "-2.0545", "-0.2460")
    assert new["progress"]["trades"] == 4 and new["progress"]["biased_days"] == ["2026-11-25"]
