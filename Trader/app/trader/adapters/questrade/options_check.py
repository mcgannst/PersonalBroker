"""The live check behind `trader options-check --symbol X` (OPTSIM task plan T2, run at T17): reads one
underlying's chain, one put quote near the money and the symbol details, and reports what came back.

The report holds plain JSON values only (text, numbers, booleans, lists) and never a token or an API host:
an error is Questrade's short masked summary, or the client's masked transport message."""

from decimal import Decimal
from typing import Any

from trader.adapters.questrade.client import QuestradeApiError
from trader.adapters.questrade.option_types import (
    OptionQuoteClient,
    QtChainExpiry,
    QtChainStrike,
    QtSymbolDetails,
)
from trader.logging_setup import redact_text

DETAIL_FIELDS: tuple[str, ...] = (
    "eps",
    "pe",
    "market_cap",
    "dividend",
    "ex_date",
    "yield_pct",
    "industry_sector",
    "industry_group",
)


def _text(value: Any) -> str | None:
    return None if value is None else str(value)


def _error_text(exc: QuestradeApiError) -> str:
    return exc.summary if exc.status else redact_text(str(exc))[:300]


def _near_the_money(expiry: QtChainExpiry, last: Decimal | None) -> QtChainStrike | None:
    """The strike closest to the underlying's last price (the middle strike when there is no price)."""
    strikes = sorted((s for root in expiry.roots for s in root.strikes), key=lambda s: s.strike)
    if not strikes:
        return None
    if last is None:
        return strikes[len(strikes) // 2]
    return min(strikes, key=lambda s: abs(s.strike - last))


def _details_report(details: QtSymbolDetails | None) -> dict[str, Any]:
    if details is None:
        return {"found": False, "has_options": None, "present": [], "missing": list(DETAIL_FIELDS)}
    present = [name for name in DETAIL_FIELDS if getattr(details, name) is not None]
    return {
        "found": True,
        "has_options": details.has_options,
        "security_type": details.security_type,
        "present": present,
        "missing": [name for name in DETAIL_FIELDS if name not in present],
    }


async def live_check(client: OptionQuoteClient, symbol: str) -> dict[str, Any]:
    """What Questrade returns for `symbol` right now. `ok` is True when the chain has expiries, a put quote
    came back and the details were found; `real_time` is True only when that quote's `delay` is 0 (None,
    i.e. omitted, counts as not real-time). A failed call ends the check with `error` set; nothing raises
    for a Questrade failure."""
    ticker = symbol.strip().upper()
    report: dict[str, Any] = {
        "symbol": ticker,
        "ok": False,
        "error": None,
        "symbol_id": None,
        "underlying_last": None,
        "expiries": 0,
        "first_expiry": None,
        "last_expiry": None,
        "put": None,
        "real_time": False,
        "details": None,
    }
    try:
        found = (await client.symbols_by_names([ticker])).get(ticker)
        if found is None:
            report["error"] = f"unknown symbol {ticker}"
            return report
        symbol_id = found.symbol_id
        report["symbol_id"] = symbol_id

        share = next(iter(await client.quotes([symbol_id])), None)
        last = None if share is None else (share.last if share.last is not None else share.last_regular)
        report["underlying_last"] = _text(last)

        chain = await client.option_chain(symbol_id)
        report["expiries"] = len(chain)
        if chain:
            report["first_expiry"] = chain[0].expiry.isoformat()
            report["last_expiry"] = chain[-1].expiry.isoformat()
            strike = _near_the_money(chain[0], last)
            quote = None if strike is None else next(iter(await client.option_quotes([strike.put_id])), None)
            if strike is not None and quote is not None:
                report["put"] = {
                    "symbol": quote.symbol,
                    "symbol_id": quote.symbol_id,
                    "expiry": chain[0].expiry.isoformat(),
                    "strike": _text(strike.strike),
                    "bid": _text(quote.bid),
                    "ask": _text(quote.ask),
                    "delay": quote.delay,
                    "is_halted": quote.is_halted,
                    "iv_pct": _text(quote.iv_pct),
                    "delta": _text(quote.delta),
                    "open_interest": quote.open_interest,
                    "last_trade_time": _text(quote.last_trade_time),
                }
                report["real_time"] = quote.delay == 0

        details = (await client.symbol_details([symbol_id])).get(symbol_id)
        report["details"] = _details_report(details)
        report["ok"] = bool(chain) and report["put"] is not None and details is not None
    except QuestradeApiError as exc:
        report["error"] = _error_text(exc)
    return report
