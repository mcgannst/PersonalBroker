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

    from trader.adapters.questrade.auth import QuestradeAuth, QuestradeAuthError
    from trader.adapters.questrade.client import QuestradeApiError, QuestradeClient
    from trader.bootstrap import build_core

    class CheckFailed(Exception):
        pass

    core = build_core()
    auth = QuestradeAuth(core.factory, core.crypto, core.clock)
    name = symbol.upper()

    async def run() -> None:
        async with QuestradeClient(auth, core.clock) as qt:
            server = await qt.server_time()
            sym = (await qt.symbols_by_names([name])).get(name)
            if sym is None:
                raise CheckFailed(f"unknown symbol {name}")
            quotes = await qt.quotes([sym.symbol_id])
            if not quotes:
                raise CheckFailed(f"Questrade returned no quote for {name}")
            quote = quotes[0]
            now = core.clock.now()
            skew = (now - server).total_seconds()
            typer.echo(f"server time {server.isoformat()} (local clock differs by {skew:.1f}s)")
            age = (now - quote.last_trade_time).total_seconds() if quote.last_trade_time else None
            delay = "unknown" if quote.delay is None else quote.delay
            typer.echo(
                f"{name}: last={quote.last} bid={quote.bid} ask={quote.ask} delay={delay} "
                f"lastTradeTime={quote.last_trade_time} age_s={age}"
            )
            typer.echo(f"rate limit remaining: {qt.rate_limit_remaining}")

    try:
        asyncio.run(run())
    except (CheckFailed, QuestradeAuthError, QuestradeApiError) as exc:
        typer.echo(f"questrade-check failed: {exc}", err=True)
        raise typer.Exit(1) from None


@app.command()
def nightly(
    date_: str | None = typer.Option(None, "--date", help="Target session YYYY-MM-DD"),
    force: bool = typer.Option(
        False, "--force", help="Re-run a succeeded session, and allow a run before the data has settled."
    ),
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
    from trader.jobs.nightly import NightlyDeps, earliest_run_time, run_nightly, target_session
    from trader.jobs.runner import run_job

    core = build_core()
    settings = core.settings.load()
    if date_:
        try:
            session_date = date_cls.fromisoformat(date_)
            is_session = core.calendar.is_session(session_date)
        except ValueError:  # not a date, or outside the calendar's range
            is_session = False
        if not is_session:
            typer.echo(f"--date {date_} is not a trading session", err=True)
            raise typer.Exit(1)
    else:
        session_date = target_session(core.calendar, core.clock)
    not_before = earliest_run_time(core.calendar, session_date, settings.open_bar_lookback_sessions)
    if core.clock.now() < not_before and not force:
        typer.echo(
            f"too early for {session_date}: the last lookback session's data settles at "
            f"{not_before.isoformat()} (use --force to run anyway)",
            err=True,
        )
        raise typer.Exit(1)
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
def premarket(
    date_: str | None = typer.Option(None, "--date", help="Session YYYY-MM-DD (default: today in ET)"),
    force: bool = typer.Option(False, "--force"),
) -> None:
    """Pre-market scan: gappers and news, headlines, Claude catalysts, brief (SPEC §9, 08:00 ET)."""
    import asyncio
    from datetime import date as date_cls
    from pathlib import Path
    from typing import Any

    import anthropic

    from trader.adapters.claude.catalyst import CatalystClassifier, CatalystService, CatalystStore
    from trader.adapters.finviz.scraper import FinvizScraper
    from trader.adapters.questrade.auth import QuestradeAuth
    from trader.adapters.questrade.client import QuestradeClient
    from trader.bootstrap import build_core
    from trader.jobs.premarket import PremarketDeps, run_premarket
    from trader.jobs.runner import run_job
    from trader.market.clock import et_date
    from trader.market.data_service import MarketDataService

    core = build_core()
    settings = core.settings.load()
    session_date = date_cls.fromisoformat(date_) if date_ else et_date(core.clock.now())
    if not core.calendar.is_session(session_date):
        typer.echo(f"premarket {session_date}: not a trading session, nothing to do")
        return
    auth = QuestradeAuth(core.factory, core.crypto, core.clock)
    api_key = core.env.anthropic_api_key
    cache_dir = Path.home() / ".cache" / "trader" / "finviz"

    with FinvizScraper(
        min_interval_s=settings.finviz_min_interval_seconds,
        cache_dir=cache_dir,
        cache_ttl_s=settings.finviz_cache_hours * 3600,
    ) as finviz:

        def job() -> dict[str, Any]:
            async def go() -> dict[str, Any]:
                claude = (
                    anthropic.AsyncAnthropic(api_key=api_key.get_secret_value(), timeout=30, max_retries=1)
                    if api_key
                    else None
                )
                try:
                    async with QuestradeClient(auth, core.clock) as qt:
                        classifier = CatalystClassifier(claude, core.settings.load) if claude else None
                        service = CatalystService(
                            core.factory,
                            core.clock,
                            CatalystStore(core.factory, core.clock),
                            classifier,
                            finviz,
                        )
                        data = MarketDataService(core.factory, core.clock, core.calendar, qt)
                        deps = PremarketDeps(core.factory, core.clock, finviz, data, service, settings)
                        return await run_premarket(deps, session_date)
                finally:
                    if claude is not None:
                        await claude.close()

            return asyncio.run(go())

        out = run_job(core.factory, core.clock, "premarket", session_date, job, force=force)
    if out.status == "succeeded":
        typer.echo(out.detail["brief"])
    else:
        typer.echo(f"premarket {session_date}: {out.status} {out.error or ''}")
    if out.status == "failed":
        raise typer.Exit(1)


@app.command()
def notify(text: str) -> None:
    """Send a Telegram message to Stephen through the configured bot."""
    import httpx
    from pydantic import ValidationError

    from trader.config import get_env

    try:
        env = get_env()
    except ValidationError as exc:
        # Field names only: the error text would echo the invalid values, which may be secrets.
        fields = sorted({".".join(str(p) for p in e["loc"]) for e in exc.errors()})
        typer.echo(f"invalid configuration: {', '.join(fields)}", err=True)
        raise typer.Exit(1) from None
    if env.telegram_bot_token is None or env.telegram_chat_id is None:
        typer.echo("Telegram isn't configured", err=True)
        raise typer.Exit(1)
    try:
        r = httpx.post(
            f"https://api.telegram.org/bot{env.telegram_bot_token.get_secret_value()}/sendMessage",
            data={"chat_id": env.telegram_chat_id, "text": text},
            timeout=15,
        )
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        # Only the exception type: its message or traceback could carry the URL, which holds the token.
        typer.echo(f"failed: {type(exc).__name__}", err=True)
        raise typer.Exit(1) from None
    if r.status_code != 200:
        typer.echo(f"failed: HTTP {r.status_code}", err=True)
        raise typer.Exit(1)
    typer.echo("sent")
