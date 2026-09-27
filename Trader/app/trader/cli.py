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
def questrade_seed(
    force: bool = typer.Option(False, "--force", help="Replace an existing healthy chain."),
) -> None:
    """Store QUESTRADE_REFRESH_TOKEN (from the environment) as the start of the token chain."""
    from trader.adapters.questrade.auth import QuestradeAuth, QuestradeAuthError
    from trader.bootstrap import build_core

    core = build_core()
    if core.env.questrade_refresh_token is None:
        typer.echo("QUESTRADE_REFRESH_TOKEN is not set", err=True)
        raise typer.Exit(1)
    auth = QuestradeAuth(core.factory, core.crypto, core.clock)
    health = auth.health()
    if health.seeded and not health.last_error and not force:
        typer.echo(
            "a healthy token chain is already stored; seeding would replace it. Use --force to overwrite.",
            err=True,
        )
        raise typer.Exit(1)
    try:
        auth.seed(core.env.questrade_refresh_token.get_secret_value())
    except QuestradeAuthError as exc:
        typer.echo(f"seed failed: {exc}", err=True)
        raise typer.Exit(1) from None
    typer.echo("seeded")


@app.command("token-refresh")
def token_refresh() -> None:
    """Keep the Questrade refresh-token chain alive (daily job, SPEC §9)."""
    from trader.adapters.questrade.auth import QuestradeAuth, QuestradeAuthError
    from trader.bootstrap import build_core

    core = build_core()
    auth = QuestradeAuth(core.factory, core.crypto, core.clock)
    try:
        auth.keep_alive()
    except QuestradeAuthError as exc:
        typer.echo(f"token refresh failed: {exc}", err=True)
        raise typer.Exit(1) from None
    # keep_alive's access token may already be expired; the chain extension time is what matters.
    extended = auth.health().last_refresh_at
    typer.echo(f"ok; chain last extended {extended.isoformat() if extended else 'never'}")


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


@app.command()
def nightly(
    date_: str | None = typer.Option(None, "--date", help="Target session YYYY-MM-DD"),
    force: bool = typer.Option(False, "--force"),
) -> None:
    """Build the universe and caches for the next session (SPEC §9, 20:00 ET)."""
    import asyncio
    from datetime import date as date_cls
    from pathlib import Path
    from typing import Any

    from trader.adapters.finviz.scraper import FinvizScraper
    from trader.adapters.questrade.auth import QuestradeAuth
    from trader.adapters.questrade.client import QuestradeClient
    from trader.bootstrap import build_core
    from trader.jobs.nightly import NightlyDeps, run_nightly, target_session
    from trader.jobs.runner import run_job

    core = build_core()
    settings = core.settings.load()
    session_date = date_cls.fromisoformat(date_) if date_ else target_session(core.calendar, core.clock)
    auth = QuestradeAuth(core.factory, core.crypto, core.clock)
    # A private per-user cache (the scraper creates it 0o700 and refuses one it doesn't own), never a
    # shared /tmp path.
    cache_dir = Path.home() / ".cache" / "trader" / "finviz"

    with FinvizScraper(
        min_interval_s=settings.finviz_min_interval_seconds,
        cache_dir=cache_dir,
        cache_ttl_s=settings.finviz_cache_hours * 3600,
    ) as finviz:

        def job() -> dict[str, Any]:
            async def go() -> dict[str, Any]:
                async with QuestradeClient(auth, core.clock) as qt:
                    return await run_nightly(
                        NightlyDeps(core.factory, core.clock, core.calendar, finviz, qt, settings),
                        session_date,
                    )

            return asyncio.run(go())

        out = run_job(core.factory, core.clock, "nightly", session_date, job, force=force)
    typer.echo(f"nightly {session_date}: {out.status} {out.detail or out.error or ''}")
    if out.status == "failed":
        raise typer.Exit(1)


@app.command()
def notify(text: str) -> None:
    """Send a Telegram message to Stephen through the configured bot."""
    import httpx

    from trader.config import get_env

    env = get_env()
    if env.telegram_bot_token is None or env.telegram_chat_id is None:
        typer.echo("Telegram isn't configured", err=True)
        raise typer.Exit(1)
    try:
        r = httpx.post(
            f"https://api.telegram.org/bot{env.telegram_bot_token.get_secret_value()}/sendMessage",
            data={"chat_id": env.telegram_chat_id, "text": text},
            timeout=15,
        )
    except httpx.HTTPError as exc:
        # Only the exception type: its message or traceback could carry the URL, which holds the token.
        typer.echo(f"failed: {type(exc).__name__}", err=True)
        raise typer.Exit(1) from None
    if r.status_code != 200:
        typer.echo(f"failed: HTTP {r.status_code}", err=True)
        raise typer.Exit(1)
    typer.echo("sent")
