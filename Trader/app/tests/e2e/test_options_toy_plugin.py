"""OPTSIM T16, feature acceptance items 6 and 2 (the strategy half) end to end.

- Item 6: the test-only plug-in `tests.options.toy_plugin:ToyCallBuyer` is installed by an entry point, next
  to the wheel. The real registry, host, worker, broker and API run it: it buys its call, the Strategies tab
  lists it with its settings form, serves its panel and takes its action. Nothing under `trader/` or
  `web/src` names it.
- Item 2: a plug-in that asks for an uncovered short call gets a rejected order, exactly as the ticket does.
"""

from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from tests.e2e import options_world as w
from tests.options.toy_plugin import TOY_EVENT, TOY_KEY, ToyCallBuyer
from trader.db import models as m
from trader.option_strategies.base import OpenStructure, OptionEvent, OptionIntent, OptionStrategyContext
from trader.options.types import ContractKey, LegSpec

pytestmark = pytest.mark.db
D = Decimal
REPO = Path(__file__).resolve().parents[4]
NAKED_KEY = "naked_call"
NAKED_ENTRY = "tests.e2e.test_options_toy_plugin:NakedCallSeller"


class NakedCallSeller(ToyCallBuyer):
    """A plug-in that tries to sell a call it has no shares for."""

    key = NAKED_KEY

    async def on_event(self, ctx: OptionStrategyContext, event: OptionEvent) -> list[OptionIntent]:
        if event.key != TOY_EVENT or ctx.orders:
            return []
        contract = ContractKey("F", w.PUT_EXPIRY, D(52), "call")
        return [
            OpenStructure(
                legs=(LegSpec("option", "sell", 1, "F", contract),),
                qty=1,
                order_type="market",
                net_limit=None,
                tif="day",
                reason="naked call",
            )
        ]


async def test_toy_plugin_trades_and_shows_its_panel_with_no_core_change(
    db_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    plugins = (("wheel", w.WHEEL_ENTRY), (TOY_KEY, w.TOY_ENTRY))
    async with w.open_world(db_factory, monkeypatch, tmp_path, plugins=plugins) as world:
        world.quote("F", w.PUT_EXPIRY, "52", "call", "4.50")  # the call nearest the money, 45 days out
        world.quote("F", w.PUT_EXPIRY, "50", "call", "6.00")
        await world.refresh(w.DAY0)

        world.go(w.DAY0, 9, 35)  # open+5m: its own scheduled event
        reports = await world.run_until(w.DAY0, 9, 36)
        assert (TOY_KEY, TOY_EVENT, "succeeded") in reports[0].events
        assert sum(r.fills for r in reports) == 1
        (held,) = await world.structures()
        assert (held.source, held.kind, held.reserved_cash, held.entry_net) == (
            TOY_KEY,
            "long_call",
            D(0),
            D("-4.55"),
        )
        (order,) = await world.orders("filled")
        assert (order.source, order.submitted_by, order.reason) == (TOY_KEY, f"strategy:{TOY_KEY}", TOY_EVENT)
        account = await world.assert_reconciled()
        assert account.cash == D(20_000) - 455 - D("0.99")
        assert any(t.startswith(f"<b>OPTION FILL</b> F long call ({TOY_KEY})") for t in world.messages())

        # The generic Strategies tab: listed with its own settings form, its panel and its action.
        listed = {item["key"]: item for item in world.get("/strategies")["items"]}
        assert set(listed) == {"wheel", TOY_KEY}
        toy = listed[TOY_KEY]
        assert (toy["enabled"], toy["open_structures"], toy["manual_events"]) == (True, 1, [TOY_EVENT])
        assert {f["name"] for f in toy["fields"]} == {"underlying", "min_dte", "at"}
        panel = world.get(f"/strategies/{TOY_KEY}/panel")
        assert panel["strategy_key"] == TOY_KEY
        (table,) = panel["tables"]
        (row,) = table["rows"]
        assert (row["id"], row["cells"]["contract"], row["cells"]["qty"]) == (
            str(held.id),
            "F 2026-11-20 C 52.00",
            1,
        )
        done = world.post(
            f"/strategies/{TOY_KEY}/actions", {"action": "note", "row_id": row["id"], "value": "hold it"}
        )
        assert done == {"ok": True, "message": "Note saved"}
        summary = {kv["label"]: kv["value"] for kv in world.get(f"/strategies/{TOY_KEY}/panel")["summary"]}
        assert summary == {"Calls held": "1", "Note": "hold it"}
        changed = world.send("PUT", f"/strategies/{TOY_KEY}", {"params": {"min_dte": 40}, "enabled": None})
        assert (changed["revision"], changed["params"]["min_dte"]) == (2, 40)
        bad = world.send(
            "PUT", f"/strategies/{TOY_KEY}", {"params": {"min_dte": 0}, "enabled": None}, status=422
        )
        assert bad["error"]["fields"][0]["msg"]  # the page shows this under the field

        # It did not disturb the wheel, and its event does not run twice in a session.
        assert world.count(m.WheelPosition) == 0
        await world.run_until(w.DAY0, 9, 38)
        assert len(await world.orders()) == 1
        assert world.errors() == []


def test_nothing_outside_its_package_names_the_toy_plugin() -> None:
    """Acceptance item 6: "no change outside its own package". The application and the web know nothing
    of it: its key appears nowhere under `trader/` or in the web's production code (the web's own tests
    use the same toy key as their example of an unknown plug-in)."""
    hits = []
    for root, pattern in (
        (REPO / "Trader" / "app" / "trader", "*.py"),
        (REPO / "Trader" / "web" / "src", "*.ts*"),
    ):
        assert root.is_dir(), root
        scanned = 0
        for path in root.rglob(pattern):
            if (
                ".test." in path.name
                or "test" in path.relative_to(root).parts[:-1]
                or path.name.startswith("test")
            ):
                continue
            scanned += 1
            text = path.read_text(encoding="utf-8")
            if TOY_KEY in text or "ToyCallBuyer" in text or "toy_plugin" in text:
                hits.append(str(path.relative_to(REPO)))
        assert scanned > 20, root
    assert hits == []


async def test_a_strategy_intent_for_an_uncovered_short_is_rejected(
    db_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    plugins = (("wheel", w.WHEEL_ENTRY), (NAKED_KEY, NAKED_ENTRY))
    async with w.open_world(db_factory, monkeypatch, tmp_path, plugins=plugins) as world:
        world.quote("F", w.PUT_EXPIRY, "52", "call", "4.50")
        await world.refresh(w.DAY0)
        world.go(w.DAY0, 9, 35)
        reports = await world.run_until(w.DAY0, 9, 37)
        assert (NAKED_KEY, TOY_EVENT, "succeeded") in reports[0].events
        assert sum(r.fills for r in reports) == 0
        (order,) = await world.orders()
        assert (order.source, order.status, order.reject_reason) == (NAKED_KEY, "rejected", "naked_short")
        assert order.reserved_cash == 0
        assert await world.structures() == []
        account = await world.assert_reconciled()
        assert (account.cash, account.reserved) == (D(20_000), D(0))
        # The host recorded why, on the strategy's own activity line.
        activity = world.get("/activity")["items"]
        assert any(
            a["source"] == NAKED_KEY and "rejected: naked_short" in a["title"] + a["detail"] for a in activity
        )
        assert world.errors() == []
