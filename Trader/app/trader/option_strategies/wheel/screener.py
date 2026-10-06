"""The market-wide candidate screen (D8): FinViz tickers that match the wheel's filter codes and are not on
the wheel's list yet. The strategy screens them with its ten tests, ranks them and offers the best few to
the owner.

`source` is how FinViz is reached: a function from the comma-joined filter codes to tickers. The default
uses the existing polite FinViz scraper (network, about 2 s per page); a test replaces `source`."""

from collections.abc import Callable

from trader.adapters.finviz.parser import to_questrade_ticker
from trader.adapters.finviz.scraper import FinvizScraper
from trader.option_strategies.wheel.config import WheelParams

Screen = Callable[[str], list[str]]


def finviz_tickers(filters: str) -> list[str]:
    """Every ticker FinViz lists for these filter codes, in FinViz's order. Raises FinvizError when the page
    is blocked, changed, or a filter code was ignored."""
    with FinvizScraper(cache_screens=False) as scraper:
        return [to_questrade_ticker(row["Ticker"]) for row in scraper.screen(filters).rows]


source: Screen = finviz_tickers


def market_candidates(screen: Screen, params: WheelParams, known: set[str]) -> list[str]:
    """The screen's tickers that are new to the wheel, in the screen's order, each once. Nothing is asked
    of FinViz when the cap is 0 or there are no filters (an unfiltered screen is the whole market)."""
    if params.market_screen_max_candidates <= 0 or not params.market_screen_filters:
        return []
    found: list[str] = []
    for ticker in screen(",".join(params.market_screen_filters)):
        symbol = ticker.strip().upper()
        if symbol and symbol not in known and symbol not in found:
            found.append(symbol)
    return found
