"""`trader <command>` entry points."""

import typer

from trader import __version__

app = typer.Typer(no_args_is_help=True, add_completion=False)


@app.callback()
def main() -> None:
    """Trader simulation platform."""


@app.command()
def version() -> None:
    """Print the Trader version."""
    typer.echo(__version__)


@app.command("questrade-seed")
def questrade_seed() -> None:
    """Store QUESTRADE_REFRESH_TOKEN (from the environment) as the start of the token chain."""
    from trader.adapters.questrade.auth import QuestradeAuth
    from trader.bootstrap import build_core

    core = build_core()
    if core.env.questrade_refresh_token is None:
        typer.echo("QUESTRADE_REFRESH_TOKEN is not set", err=True)
        raise typer.Exit(1)
    QuestradeAuth(core.factory, core.crypto, core.clock).seed(
        core.env.questrade_refresh_token.get_secret_value()
    )
    typer.echo("seeded")


@app.command("token-refresh")
def token_refresh() -> None:
    """Keep the Questrade refresh-token chain alive (daily job, SPEC §9)."""
    from trader.adapters.questrade.auth import QuestradeAuth, QuestradeAuthError
    from trader.bootstrap import build_core

    core = build_core()
    try:
        token = QuestradeAuth(core.factory, core.crypto, core.clock).keep_alive()
    except QuestradeAuthError as exc:
        typer.echo(f"token refresh failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"ok; access token valid until {token.expires_at.isoformat()}")


@app.command("questrade-check")
def questrade_check(symbol: str = "SPY") -> None:
    """Show Questrade server time, one quote's freshness, and remaining rate limits (spike S2)."""
    import asyncio

    from trader.adapters.questrade.auth import QuestradeAuth
    from trader.adapters.questrade.client import QuestradeClient
    from trader.bootstrap import build_core

    core = build_core()
    auth = QuestradeAuth(core.factory, core.crypto, core.clock)

    async def run() -> None:
        async with QuestradeClient(auth, core.clock) as qt:
            server = await qt.server_time()
            sym = (await qt.symbols_by_names([symbol]))[symbol]
            (quote,) = await qt.quotes([sym.symbol_id])
            now = core.clock.now()
            skew = (now - server).total_seconds()
            typer.echo(f"server time {server.isoformat()} (local clock differs by {skew:.1f}s)")
            age = (now - quote.last_trade_time).total_seconds() if quote.last_trade_time else None
            typer.echo(
                f"{symbol}: last={quote.last} bid={quote.bid} ask={quote.ask} delay={quote.delay} "
                f"lastTradeTime={quote.last_trade_time} age_s={age}"
            )
            typer.echo(f"rate limit remaining: {qt.rate_limit_remaining}")

    asyncio.run(run())
