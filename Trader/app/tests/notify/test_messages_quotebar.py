"""QUOTEBAR: the daily summary's "Opening bars from quotes" line (the 09:47 shadow check's result)."""

import pytest

from tests.notify.test_messages import r, summary  # noqa: F401 (fixture)
from trader.notify.messages import MessageRenderer
from trader.notify.types import QuoteBarsLineView


def text(renderer: MessageRenderer, line: QuoteBarsLineView | None) -> str:
    return renderer.daily_summary(summary(quote_bars=line), ()).text


def test_no_line_without_quote_built_bars(r: MessageRenderer) -> None:  # noqa: F811
    assert "from quotes" not in text(r, None)


def test_the_line_gives_counts_and_percentages(r: MessageRenderer) -> None:  # noqa: F811
    line = QuoteBarsLineView(
        quote_bars=540, compared=100, prices_exact=97, volume_within=81, decision_differs=2
    )
    got = text(r, line)
    assert (
        "Opening bars from quotes: 100 compared, prices exact 97%, volume within ±10% 81%, "
        "2 decisions would differ" in got
    )
    lines = got.splitlines()
    assert lines.index(next(x for x in lines if x.startswith("Opening bars"))) < lines.index(
        next(x for x in lines if x.startswith("Candles archived"))
    )


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (
            QuoteBarsLineView(quote_bars=3, compared=3, prices_exact=2, volume_within=3, decision_differs=0),
            "Opening bars from quotes: 3 compared, prices exact 67%, volume within ±10% 100%",
        ),
        (
            QuoteBarsLineView(
                quote_bars=540, compared=0, prices_exact=0, volume_within=0, decision_differs=0
            ),
            "Opening bars from quotes: 540 built, none compared (shadow check missing)",
        ),
        (
            QuoteBarsLineView(quote_bars=5, compared=1, prices_exact=1, volume_within=1, decision_differs=1),
            "Opening bars from quotes: 1 compared, prices exact 100%, volume within ±10% 100%, "
            "1 decision would differ",
        ),
    ],
)
def test_line_variants(r: MessageRenderer, line: QuoteBarsLineView, expected: str) -> None:  # noqa: F811
    got = text(r, line)
    assert expected in got
    if line.decision_differs == 0:
        assert "would differ" not in got
