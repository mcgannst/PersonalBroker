"""OPTSIM T14: `/prompts` and `/prompts/{id}/answer` over the fake prompt store."""

from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from tests.api.conftest import make_client
from tests.fakes_api import make_services, test_core
from tests.options.factories import T0, add_options_run
from tests.options.fakes import make_option_services
from trader.api.routers.options import router
from trader.db import models as m
from trader.market.clock import FixedClock
from trader.options.types import OwnerPromptRequest, PromptChoice

pytestmark = pytest.mark.db

YES_NO = (PromptChoice("y", "Yes"), PromptChoice("n", "No"))
SECRET = "a private thought 4711"


@pytest.mark.parametrize(
    ("prompt_id", "body", "status", "code", "loc"),
    [
        (1, {"choice": "y"}, 200, None, None),
        (2, {"choice": "a", "text": "Because earnings"}, 200, None, None),
        (1, {"choice": "z", "text": SECRET}, 422, "validation", ["body", "choice"]),  # not offered
        (2, {"choice": "a", "text": "  "}, 422, "validation", ["body", "text"]),  # the text is required
        (3, {"choice": "y", "text": SECRET}, 409, "conflict", None),  # already cancelled
        (99, {"choice": "y", "text": SECRET}, 404, "not_found", None),
        (1, {"choice": "yes"}, 422, "validation", ["body", "choice"]),  # one lowercase letter
    ],
)
def test_prompts_list_and_answer_outcomes(
    db_factory: sessionmaker[Session],
    prompt_id: int,
    body: dict[str, Any],
    status: int,
    code: str | None,
    loc: list[str] | None,
) -> None:
    clock = FixedClock(T0)
    options, fakes = make_option_services(db_factory, clock)
    client = make_client(make_services(test_core(db_factory, clock), options=options), router)
    store = fakes.prompts
    store.ensure(1, "toy_call", OwnerPromptRequest("fresh_cash", "F", "k1", "Keep F?", "Body", YES_NO))
    store.ensure(
        1,
        "toy_call",
        OwnerPromptRequest(
            "review",
            "F",
            "k2",
            "Why?",
            "Body",
            (PromptChoice("a", "Answer"),),
            needs_text=True,
            data={"n": 1},
        ),
    )
    store.ensure(1, "toy_call", OwnerPromptRequest("old", "F", "k3", "Old", "Body", YES_NO))
    store.cancel("k3")

    pending = client.get("/api/options/prompts").json()["items"]
    assert [p["id"] for p in pending] == [2, 1]  # the cancelled one is not pending
    assert pending[0]["needs_text"] is True and pending[0]["data"] == {"n": 1}
    assert pending[1]["choices"] == [{"code": "y", "label": "Yes"}, {"code": "n", "label": "No"}]

    r = client.post(f"/api/options/prompts/{prompt_id}/answer", json=body)
    assert r.status_code == status, r.text
    if code is None:
        answered = r.json()
        assert (answered["id"], answered["status"], answered["answer"], answered["answered_via"]) == (
            prompt_id,
            "answered",
            body["choice"],
            "web",
        )
        assert (answered["answer_text"], answered["answered_at"]) == (
            body.get("text"),
            "2026-10-06T14:00:00Z",
        )
        assert store.answered_by == {prompt_id: "web:stephen"}
        assert prompt_id not in [p["id"] for p in client.get("/api/options/prompts").json()["items"]]
        # A second answer is a conflict.
        again = client.post(f"/api/options/prompts/{prompt_id}/answer", json=body)
        assert (again.status_code, again.json()["error"]["code"]) == (409, "conflict")
    else:
        error = r.json()["error"]
        assert error["code"] == code and SECRET not in r.text
        if loc is not None:
            assert error["fields"][0]["loc"] == loc
        assert store.answered_by == {}


def test_all_prompts_come_from_the_runs_rows(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        run_id = add_options_run(s)
        for n, status in enumerate(("answered", "pending")):
            s.add(
                m.OwnerPrompt(
                    run_id=run_id,
                    source="toy_call",
                    kind="fresh_cash",
                    scope_key="F",
                    dedupe_key=f"k{n}",
                    title=f"Question {n}",
                    body="Body",
                    choices=[{"code": "y", "label": "Yes"}],
                    data={},
                    status=status,
                    asked_at=T0,
                    answer="y" if status == "answered" else None,
                    answered_via="telegram" if status == "answered" else None,
                )
            )
        s.commit()
    clock = FixedClock(T0)
    options, _ = make_option_services(db_factory, clock, run_id=run_id)
    client = make_client(make_services(test_core(db_factory, clock), options=options), router)
    r = client.get("/api/options/prompts", params={"status": "all"})
    assert r.status_code == 200, r.text
    assert [(p["id"], p["status"], p["answer"], p["answered_via"]) for p in r.json()["items"]] == [
        (2, "pending", None, None),
        (1, "answered", "y", "telegram"),
    ]
    assert client.get("/api/options/prompts", params={"status": "answered"}).status_code == 422
