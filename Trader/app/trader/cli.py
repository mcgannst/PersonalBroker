"""`trader <command>` entry points (the crontab's commands: docker/crontab)."""

import re
from collections.abc import Callable, Coroutine, Mapping
from datetime import date
from typing import TYPE_CHECKING, Any, NoReturn

import typer

from trader import __version__

if TYPE_CHECKING:
    from trader.bootstrap import Core
    from trader.jobs.runner import JobOutcome
    from trader.settings_store import RuntimeSettings

app = typer.Typer(no_args_is_help=True, add_completion=False)


def _setup_logging() -> None:
    """Every command calls this first (P3-T2/T12): structlog JSON lines, stdlib routed through it,
    token-bearing loggers quiet. Called through the module attribute, so tests can swap it."""
    from trader import logging_setup

    logging_setup.configure_logging("cron")


@app.callback()
def main() -> None:
    """Trader simulation platform."""


@app.command()
def version() -> None:
    """Print the Trader version."""
    _setup_logging()
    typer.echo(__version__)


@app.command("questrade-seed")
def questrade_seed(
    force: bool = typer.Option(False, "--force", help="Replace an existing healthy chain."),
) -> None:
    """Store QUESTRADE_REFRESH_TOKEN (from the environment) as the start of the token chain."""
    _setup_logging()
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
    _setup_logging()
    from trader import runtime
    from trader.adapters.questrade.auth import QuestradeAuth, QuestradeAuthError

    core = _core("token-refresh")
    auth = QuestradeAuth(core.factory, core.crypto, core.clock)
    try:
        auth.keep_alive()
    except QuestradeAuthError as exc:
        # The relay alerts; the 09:20 pre-open check alerts again. record_token_failure never raises.
        runtime.record_token_failure(core, exc)
        _fail(f"token refresh failed: {_masked_line(str(exc))}")
    except Exception as exc:  # an undecryptable chain (key changed), a database error, ...: same alert
        runtime.record_token_failure(core, exc)
        _fail(f"token refresh failed: {_one_line(exc)}")
    # keep_alive's access token may already be expired; the chain extension time is what matters.
    try:
        extended = auth.health().last_refresh_at
    except Exception as exc:
        _fail(f"token refresh: refreshed, but the chain state could not be read: {_one_line(exc)}")
    typer.echo(f"ok; chain last extended {extended.isoformat() if extended else 'never'}")


@app.command("questrade-check")
def questrade_check(symbol: str = "SPY") -> None:
    """Show Questrade server time, one quote's freshness, and remaining rate limits (spike S2)."""
    _setup_logging()
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
    _setup_logging()
    import asyncio
    from datetime import date as date_cls
    from typing import Any

    from trader import runtime
    from trader.adapters.finviz.scraper import FinvizScraper
    from trader.adapters.questrade.auth import QuestradeAuth
    from trader.adapters.questrade.client import QuestradeClient
    from trader.bootstrap import build_core
    from trader.jobs.nightly import NightlyDeps, earliest_run_time, run_nightly, target_session
    from trader.jobs.runner import run_job

    core = build_core()
    settings = _strict_settings(core, "nightly")
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
    cache_dir = runtime.finviz_cache_dir()  # one private cache for every process

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
    typer.echo(f"nightly {session_date}: {out.status} {_masked_line(str(out.detail or out.error or ''))}")
    if out.status == "failed":
        raise typer.Exit(1)


@app.command()
def premarket(
    date_: str | None = typer.Option(None, "--date", help="Session YYYY-MM-DD (default: today in ET)"),
    force: bool = typer.Option(
        False,
        "--force",
        help="Re-run a succeeded session, and allow a run outside the pre-market window "
        "(another day's session, or after the open).",
    ),
) -> None:
    """Pre-market scan: gappers and news, headlines, Claude catalysts, brief (SPEC §9, 08:00 ET).

    It runs only in the pre-market window: on the session's own ET date, before the open. Anything else
    needs --force, and a forced run's brief says which window it used."""
    _setup_logging()
    import asyncio
    from datetime import date as date_cls
    from typing import Any

    import anthropic

    from trader import runtime
    from trader.adapters.claude.catalyst import CatalystClassifier, CatalystService, CatalystStore
    from trader.adapters.finviz.scraper import FinvizScraper
    from trader.adapters.questrade.auth import QuestradeAuth
    from trader.adapters.questrade.client import QuestradeClient
    from trader.bootstrap import build_core
    from trader.jobs.premarket import PremarketDeps, run_premarket
    from trader.jobs.runner import run_job
    from trader.market.clock import ET, et_date
    from trader.market.data_service import MarketDataService

    core = build_core()
    settings = _strict_settings(core, "premarket")
    now = core.clock.now()
    today = et_date(now)
    if date_:
        try:
            session_date = date_cls.fromisoformat(date_)
        except ValueError:
            typer.echo(f"--date {date_} is not a valid date (use YYYY-MM-DD)", err=True)
            raise typer.Exit(1) from None
    else:
        session_date = today
    try:
        is_session = core.calendar.is_session(session_date)
    except ValueError:  # outside the calendar's range
        typer.echo(f"--date {session_date} is outside the trading calendar", err=True)
        raise typer.Exit(1) from None
    if not is_session:
        typer.echo(f"premarket {session_date}: not a trading session, nothing to do")
        return
    opens = core.calendar.session_open(session_date)
    window = (
        f"session {session_date} (opens {opens.astimezone(ET):%Y-%m-%d %H:%M} ET), "
        f"run at {now.astimezone(ET):%Y-%m-%d %H:%M} ET"
    )
    if session_date != today:
        problem = f"{session_date} is not today's ET date ({today})"
    elif now >= opens:
        problem = f"the {session_date} session has already opened"
    else:
        problem = None
    warnings: list[str] = []
    if problem is not None:
        if not force:
            typer.echo(f"premarket {session_date}: {problem}: {window}. Use --force to run anyway.", err=True)
            raise typer.Exit(1)
        warnings.append(f"WARNING: forced run outside the pre-market window ({problem}): {window}")
        typer.echo(warnings[-1], err=True)
    auth = QuestradeAuth(core.factory, core.crypto, core.clock)
    api_key = core.env.anthropic_api_key
    cache_dir = runtime.finviz_cache_dir()  # one private cache for every process

    # Screens are never read from the cache (a run must see this morning's news and earnings); the
    # quote pages behind the headlines are cached per ET day by the scraper itself.
    with FinvizScraper(
        min_interval_s=settings.finviz_min_interval_seconds,
        cache_dir=cache_dir,
        cache_ttl_s=settings.finviz_cache_hours * 3600,
        cache_screens=False,
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
                        deps = PremarketDeps(
                            core.factory, core.clock, finviz, data, service, settings, core.calendar
                        )
                        return await run_premarket(deps, session_date, warnings)
                finally:
                    if claude is not None:
                        await claude.close()

            return asyncio.run(go())

        out = run_job(core.factory, core.clock, "premarket", session_date, job, force=force)
    if out.status == "succeeded":
        typer.echo(out.detail["brief"])
        # Once per session (dedupe premarket:<date>); a Telegram failure never fails the job.
        asyncio.run(runtime.send_premarket_brief(core, session_date, out.detail["brief"]))
    elif out.status == "skipped":
        typer.echo(f"premarket {session_date}: skipped ({out.detail.get('reason', 'no reason given')})")
    else:
        typer.echo(
            f"premarket {session_date}: failed: {_masked_line(out.error or 'unknown error')}", err=True
        )
        raise typer.Exit(1)


@app.command()
def notify(text: str) -> None:
    """Send a Telegram message to Stephen through the configured bot."""
    _setup_logging()
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


# --- P3-T12: the cron commands of docker/crontab ------------------------------------------------------------
# Each prints one result line; a failure is one line on stderr and exit 1, never a traceback. On a day that
# is not a trading session they print "not a trading session" and exit 0 without building anything.

MAX_LINE = 1000
FIRE_FAILURES = ("failed", "missed")


def _fail(message: str, code: int = 1) -> NoReturn:
    typer.echo(message, err=True)
    raise typer.Exit(code)


def _masked_line(text: str) -> str:
    """Any text as one masked line: secrets redacted, whitespace collapsed, length capped."""
    from trader.logging_setup import redact_text

    text = " ".join(redact_text(text).split())
    return text if len(text) <= MAX_LINE else text[: MAX_LINE - 1] + "…"


def _one_line(exc: BaseException) -> str:
    """An exception as one masked line: its type and message, no traceback, no secrets."""
    message = str(exc).strip()
    return _masked_line(f"{type(exc).__name__}: {message}" if message else type(exc).__name__)


def _detail(detail: Mapping[str, Any]) -> str:
    import json

    from trader.logging_setup import redact_text

    text = redact_text(json.dumps(dict(detail), default=str))
    return text if len(text) <= MAX_LINE else text[: MAX_LINE - 1] + "…"


def _strict_settings(core: "Core", name: str) -> "RuntimeSettings":
    """The stored settings for a job that acts on them (nightly, premarket): an unusable row is one line
    and exit 1, never a traceback and never the defaults."""
    try:
        return core.settings.load()
    except Exception as exc:
        from trader import runtime

        _fail(f"{name}: failed: {runtime.settings_problem_text(exc)}; fix the row on the Settings page")


def _core(name: str) -> "Core":
    from trader.bootstrap import build_core

    try:
        return build_core()
    except Exception as exc:
        _fail(f"{name}: failed: {_one_line(exc)}")


def _session(core: "Core", date_: str | None, name: str) -> date | None:
    """The session to run for (--date, else today's ET date); None (after printing why) when it is not a
    trading session. A bad or out-of-calendar date exits 1."""
    from trader.market.clock import et_date

    if date_:
        try:
            day = date.fromisoformat(date_)
        except ValueError:
            _fail(f"{name}: --date {date_} is not a valid date (use YYYY-MM-DD)")
    else:
        day = et_date(core.clock.now())
    try:
        is_session = core.calendar.is_session(day)
    except ValueError:
        _fail(f"{name}: --date {day} is outside the trading calendar")
    if not is_session:
        typer.echo(f"{name} {day}: not a trading session, nothing to do")
        return None
    return day


def _run[T](name: str, day: date, work: Callable[[], Coroutine[Any, Any, T]]) -> T:
    import asyncio

    try:
        return asyncio.run(work())
    except Exception as exc:
        _fail(f"{name} {day}: failed: {_one_line(exc)}")


def _report(name: str, day: date, out: "JobOutcome") -> None:
    if out.status == "succeeded":
        typer.echo(f"{name} {day}: succeeded {_detail(out.detail)}")
    elif out.status == "skipped":
        typer.echo(
            f"{name} {day}: skipped ({_masked_line(str(out.detail.get('reason', 'no reason given')))})"
        )
    else:
        _fail(f"{name} {day}: failed: {_masked_line(out.error or 'unknown error')}")


DATE_OPTION = typer.Option(None, "--date", help="Session YYYY-MM-DD (default: today in ET)")


@app.command()
def preopen(
    date_: str | None = DATE_OPTION,
    force: bool = typer.Option(False, "--force", help="Re-run a session that already succeeded."),
) -> None:
    """Pre-open check: token, data, kill switches, worker heartbeat; sends the result (SPEC §9, 09:20 ET)."""
    _setup_logging()
    from trader import runtime

    core = _core("preopen")
    day = _session(core, date_, "preopen")
    if day is None:
        return
    _report("preopen", day, _run("preopen", day, lambda: runtime.preopen_job(core, day, force=force)))


_AT = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


@app.command()
def checkin(
    at: str = typer.Option(..., "--at", help="The check-in label, HH:MM (11:30 or 13:30)"),
    date_: str | None = DATE_OPTION,
    force: bool = typer.Option(
        False, "--force", help="Re-run a check-in that already succeeded (its event backup is never forced)."
    ),
) -> None:
    """Check-in: a status message plus a backup firing of every due session event (SPEC §9)."""
    _setup_logging()
    from trader import runtime

    if not _AT.match(at):
        _fail(f"checkin: --at {at!r} is not a time HH:MM", code=2)
    name = runtime.checkin_job_name(at)
    core = _core(name)
    day = _session(core, date_, name)
    if day is None:
        return
    _report(name, day, _run(name, day, lambda: runtime.checkin_job(core, day, at, force=force)))


@app.command()
def event(
    key: str | None = typer.Argument(None, help="The event key, e.g. orb_open or flatten"),
    due: bool = typer.Option(False, "--due", help="Fire every due event that has not run yet."),
    date_: str | None = DATE_OPTION,
    force: bool = typer.Option(
        False, "--force", help="With a KEY: run it even if it already ran or is early or late."
    ),
) -> None:
    """Cron backup of the worker's session events: fire KEY (or every due event) once (SPEC §9).

    Exit 0 for fired, skipped (the worker was first), too_early, not_scheduled and not_session; 1 for
    failed and missed."""
    _setup_logging()
    from trader import runtime

    if due and force:
        _fail(
            "event --due --force is refused: it would re-run every event that already ran today. "
            "Use `event KEY --force` for one event.",
            code=2,
        )
    if (key is None) == (not due):
        _fail("event: give an event KEY or --due (not both)", code=2)
    name = f"event {key}" if key is not None else "event --due"
    core = _core(name)
    day = _session(core, date_, name)
    if day is None:
        return
    results = _run(name, day, lambda: runtime.event_backup(core, key, day, due=due, force=force))
    if len(results) == 1 and results[0].status == "not_session":
        typer.echo(f"{name} {day}: not a trading session, nothing to do")
        return
    if not results:
        typer.echo(f"{name} {day}: nothing due")
    failed = False
    for r in results:
        line = f"event {r.key} {day}: {r.status} {_detail(r.detail)}"
        if r.status in FIRE_FAILURES:
            failed = True
            typer.echo(line, err=True)
        else:
            typer.echo(line)
    if failed:
        raise typer.Exit(1)


@app.command()
def postclose(
    date_: str | None = DATE_OPTION,
    force: bool = typer.Option(False, "--force", help="Re-run a session that already succeeded."),
) -> None:
    """Post-close: end-of-day cancels, journal row, candle archive, daily summary (SPEC §9, 16:15 ET)."""
    _setup_logging()
    from trader import runtime

    core = _core("postclose")
    day = _session(core, date_, "postclose")
    if day is None:
        return
    _report("postclose", day, _run("postclose", day, lambda: runtime.postclose_job(core, day, force=force)))


@app.command("telegram-test")
def telegram_test(
    buttons: bool = typer.Option(
        False, "--buttons", help="Add two test buttons (unsigned: a running bot answers Invalid button)."
    ),
) -> None:
    """Send one clearly labelled test message through the configured bot (it never polls for updates)."""
    _setup_logging()
    import asyncio
    from zoneinfo import ZoneInfo

    from pydantic import ValidationError

    from trader import runtime
    from trader.adapters.telegram.types import TelegramApiError
    from trader.config import get_env
    from trader.logging_setup import redact_text
    from trader.market.clock import RealClock
    from trader.notify.messages import fmt_time

    try:
        env = get_env()
    except ValidationError as exc:
        fields = sorted({".".join(str(p) for p in e["loc"]) for e in exc.errors()})
        _fail(f"invalid configuration: {', '.join(fields)}")
    if not runtime.telegram_configured(env):
        _fail("Telegram isn't configured (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)")
    now_label = fmt_time(RealClock().now(), ZoneInfo(env.tz_display))
    try:
        message_id = asyncio.run(runtime.telegram_test(env, now_label, buttons=buttons))
    except TelegramApiError as exc:  # its description is Telegram's text with the token masked
        _fail(f"telegram-test failed: {exc.status or 'network'} {redact_text(exc.description)}")
    except Exception as exc:  # the type only: a client error's text can carry the token in a URL
        _fail(f"telegram-test failed: {type(exc).__name__}")
    typer.echo(f"sent message {message_id}")
