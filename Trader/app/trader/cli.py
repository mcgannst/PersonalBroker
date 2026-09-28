"""`trader <command>` entry points (the crontab's commands: docker/crontab).

Phase 5 (P5-T17): every command that builds a `Core` installs the log mirror (process `cron`, rows
`log.cron`) through `_core`, and closes it when the command's context closes; `trader replay` installs its
own (process `replay`, stamped with the replay's run id) instead. The day-level jobs (nightly, premarket,
preopen, postclose, weekly) run with the settings' `RetryPolicy`; `trader weekly` and `trader replay` are new.
"""

import re
from collections.abc import Callable, Coroutine, Mapping
from datetime import date
from typing import TYPE_CHECKING, Any, NoReturn

import typer

from trader import __version__

if TYPE_CHECKING:
    from trader.bootstrap import Core
    from trader.jobs.runner import JobOutcome
    from trader.logging_mirror import EventLogMirror
    from trader.settings_store import RuntimeSettings

app = typer.Typer(no_args_is_help=True, add_completion=False)


CRON_PROCESS = "cron"  # the process name of the cron commands' log lines and mirror rows (`log.cron`)
REPLAY_PROCESS = "replay"
EXIT_BUSY = 2  # `trader replay`: another replay holds the replay lock


def _setup_logging(process: str = CRON_PROCESS) -> None:
    """Every command calls this first (P3-T2/T12): structlog JSON lines, stdlib routed through it,
    token-bearing loggers quiet. Called through the module attribute, so tests can swap it."""
    from trader import logging_setup

    logging_setup.configure_logging(process)


_OPEN_MIRRORS: list["EventLogMirror"] = []  # the log mirrors installed by this command (P5-T17)


@app.callback()
def main(ctx: typer.Context) -> None:
    """Trader simulation platform."""
    # The command's log mirror is flushed and removed when the command line's context closes (any exit).
    ctx.call_on_close(close_mirrors)


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

    core = _core("questrade-seed")
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

    class CheckFailed(Exception):
        pass

    core = _core("questrade-check")
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
    from trader.jobs.nightly import NightlyDeps, earliest_run_time, run_nightly, target_session
    from trader.jobs.runner import run_job

    core = _core("nightly")
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

        out = run_job(
            core.factory,
            core.clock,
            "nightly",
            session_date,
            job,
            force=force,
            retry=runtime.day_job_retry(settings),
        )
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
    from trader.jobs.premarket import PremarketDeps, run_premarket
    from trader.jobs.runner import run_job
    from trader.market.clock import ET, et_date
    from trader.market.data_service import MarketDataService

    core = _core("premarket")
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

        out = run_job(
            core.factory,
            core.clock,
            "premarket",
            session_date,
            job,
            force=force,
            retry=runtime.premarket_retry(settings, session_date),  # never past 09:18 ET
        )
    if out.status == "succeeded":
        typer.echo(out.detail["brief"])
        # Once per session (dedupe premarket:<date>); a Telegram failure never fails the job.
        asyncio.run(runtime.send_premarket_brief(core, session_date, out.detail["brief"]))
        # P6-T11: then a decision log pass (after the brief, so it is never delayed). It never raises.
        _record_decisions(core, session_date)
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


def _core(name: str, *, mirror: bool = True) -> "Core":
    """The command's Core (a failure is one line and exit 1). With `mirror`, the `cron` log mirror is
    installed for the rest of the command (`trader replay` installs its own, with its run id)."""
    from trader.bootstrap import build_core

    try:
        core = build_core()
    except Exception as exc:
        _fail(f"{name}: failed: {_one_line(exc)}")
    if mirror:
        _install_mirror(core, CRON_PROCESS)
    return core


def _install_mirror(core: "Core", process: str, *, run_id: int | None = None) -> None:
    """Install the log mirror (P5-T14). It is closed (flushed) by `close_mirrors` when the `trader` command
    line's context closes, whatever the exit (`main` registers it), or else at interpreter exit. Optional
    wiring: never raises."""
    import atexit

    from trader import runtime

    installed = runtime.install_log_mirror(core, process, run_id=run_id)
    if installed is None:
        return
    if not _OPEN_MIRRORS:
        atexit.register(close_mirrors)  # a safety net: close_mirrors is idempotent
    _OPEN_MIRRORS.append(installed)


def close_mirrors() -> None:
    """Close every log mirror this process's commands installed. Never raises."""
    from trader import runtime

    while _OPEN_MIRRORS:
        runtime.close_log_mirror(_OPEN_MIRRORS.pop())


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


def _record_decisions(core: "Core", day: date) -> None:
    """P6-T11: one best-effort decision log pass (`runtime.record_decisions_quietly`, which never raises
    itself). Nothing it does can change the command's output line or exit code."""
    import asyncio

    from trader import runtime

    try:
        asyncio.run(runtime.record_decisions_quietly(core, day))
    except Exception as exc:  # belt and braces: record_decisions_quietly already isolates its failures
        import structlog

        structlog.get_logger("cli").warning("cli.decisions_not_recorded", error_type=type(exc).__name__)


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
    if any(r.status == "fired" for r in results):
        # P6-T11: a cron-fired event (the worker was down) is recorded too. Best effort: it never raises and
        # the exit code never depends on it. A backup that found the event settled records nothing.
        _record_decisions(core, day)
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


# --- P4-T18: the web login's user ---------------------------------------------------------------------------

ADMIN_RESULTS = {
    "created": "created",
    "exists": "exists",
    "not_configured": "not configured",
    "rejected": "rejected",
}


@app.command("create-admin")
def create_admin() -> None:
    """Create the web user from ADMIN_USERNAME and ADMIN_PASSWORD_INITIAL when there is no user yet (the
    container entrypoint runs it on every start). Prints created, exists, not configured or rejected (exit 1,
    the reason is logged); never prints the password."""
    _setup_logging()
    from trader.api.auth import ensure_admin

    core = _core("create-admin")
    try:
        result = ensure_admin(
            core.factory, core.clock, core.env.admin_username, core.env.admin_password_initial
        )
    except Exception as exc:
        _fail(f"create-admin failed: {_one_line(exc)}")
    typer.echo(ADMIN_RESULTS[result])
    if result == "rejected":
        raise typer.Exit(1)


@app.command("user-password")
def user_password(
    username: str | None = typer.Option(None, "--username", help="Only needed when there are several users."),
) -> None:
    """Reset the web user's password from a hidden prompt (entered twice). Signs out every session."""
    _setup_logging()
    from trader.api.auth import MIN_PASSWORD_CHARS, ResetRefused, reset_password

    core = _core("user-password")
    password: str = typer.prompt(
        f"New password (at least {MIN_PASSWORD_CHARS} characters)", hide_input=True, confirmation_prompt=True
    )
    try:
        name, revoked = reset_password(core.factory, core.clock, password, username=username)
    except ResetRefused as exc:
        _fail(f"user-password: {exc}. Nothing was changed.")
    except Exception as exc:
        _fail(f"user-password failed: {_one_line(exc)}")
    typer.echo(f"password changed for {name}; {revoked} sessions signed out")


# --- P5-T17: the weekly report and replays ------------------------------------------------------------------


@app.command()
def weekly(
    date_: str | None = typer.Option(
        None, "--date", help="Any day of the week to report, YYYY-MM-DD (default: the last completed week)"
    ),
    force: bool = typer.Option(
        False, "--force", help="Re-run a week that already succeeded (it still sends one message per week)."
    ),
) -> None:
    """Weekly report of the Monday-Friday week just ended, with a Claude commentary (SPEC §9, Sat 09:00 ET).

    Keyed by the week's last session (`job_runs` `weekly`); a week without a session prints "no sessions" and
    exits 0; a week whose last session has not closed yet is refused (exit 1)."""
    _setup_logging()
    from trader import runtime
    from trader.market.clock import et_date
    from trader.reports.weekly import last_completed_week, week_window

    core = _core("weekly")
    now = core.clock.now()
    day = et_date(now)
    if date_:
        try:
            day = date.fromisoformat(date_)
        except ValueError:
            _fail(f"weekly: --date {date_} is not a valid date (use YYYY-MM-DD)")
    try:
        week = week_window(core.calendar, day) if date_ else last_completed_week(core.calendar, day)
    except ValueError:  # outside the calendar's range
        _fail(f"weekly: --date {day} is outside the trading calendar")
    week_ending = week.week_ending
    if week_ending is None:
        typer.echo(f"weekly {week.start}..{week.end}: no sessions, nothing to do")
        return
    if now < core.calendar.session_close(week_ending):
        _fail(f"weekly {week_ending}: the week of {week.start} has not ended yet")
    out = _run("weekly", week_ending, lambda: runtime.weekly_job(core, week, force=force))
    _report("weekly", week_ending, out)


# --- P6-T2: the soak report and marks -----------------------------------------------------------------------


def _soak_deps(core: "Core", notifier: Any = None, render: Any = None) -> Any:
    """The read-only SoakDeps: settings through `quiet_settings` (defaults on an unusable row, no event),
    plans through `soak.readonly_plan` (never `runtime.plan_builder`)."""
    from trader import runtime
    from trader.jobs import soak
    from trader.notify.notifier import NullNotifier

    settings = runtime.quiet_settings(core)
    return soak.SoakDeps(
        factory=core.factory,
        clock=core.clock,
        calendar=core.calendar,
        settings=settings,
        plan=soak.readonly_plan(core.factory, core.clock, core.calendar, settings),
        orb_enabled=soak.orb_enabled_reader(core.factory, core.clock),
        notifier=notifier if notifier is not None else NullNotifier(),
        render=render if render is not None else runtime.build_renderer(core),
        env=core.env.app_env,
    )


@app.command("soak-report")
def soak_report(
    through: str | None = typer.Option(
        None, "--through", help="Last session YYYY-MM-DD (default: the latest session up to today in ET)"
    ),
    sessions: int = typer.Option(20, "--sessions", min=1, max=250, help="Sessions in the window"),
    target: int = typer.Option(10, "--target", min=1, max=250, help="Consecutive clean days wanted"),
    as_json: bool = typer.Option(False, "--json", help="Print the report as JSON"),
    notify: bool = typer.Option(False, "--notify", help="Send the day's line through the configured bot"),
    final: bool = typer.Option(False, "--final", help="The Saturday line settling the week's last session"),
) -> None:
    """The soak report (P6-T2): the clean-day verdict of each recent session, the run of consecutive clean
    days and the earliest finish. Read-only (never a `job_runs` row). Exit 0 whether or not the days are
    clean; 1 only when the database can't be read."""
    _setup_logging()
    import asyncio
    import json
    from contextlib import AsyncExitStack

    from trader import runtime
    from trader.jobs import soak
    from trader.market.clock import et_date

    # No log mirror: the report writes nothing (a day plan's own error lines would otherwise become rows).
    core = _core("soak-report", mirror=False)
    last: date | None = None
    if through:
        try:
            last = date.fromisoformat(through)
            core.calendar.is_session(last)
        except ValueError:
            _fail(f"soak-report: --through {through} is not a date in the trading calendar (use YYYY-MM-DD)")
    elif notify and not final and not core.calendar.is_session(et_date(core.clock.now())):
        typer.echo("soak-report: not a trading session, nothing to send")
        return
    deps = _soak_deps(core)
    try:
        report = soak.load_report(deps, through=last, sessions=sessions, target=target)
    except Exception as exc:
        _fail(f"soak-report: failed: {_one_line(exc)}")
    # With --json the JSON is always the LAST stdout line (the send's status line comes before it), so a
    # consumer can parse the last line; the table prints first and the status line after it.
    body = [json.dumps(soak.report_json(report))] if as_json else soak.report_lines(report)
    if not as_json:
        for line in body:
            typer.echo(line)
    status: str | None = None
    send_error: str | None = None
    if notify:
        label = soak.dedupe_key(report.through, final=final)
        if not runtime.telegram_configured(core.env):
            status = "Telegram not configured"
        else:

            async def send() -> str:
                async with AsyncExitStack() as stack:
                    api = await runtime.open_telegram(core, stack)
                    sending = _soak_deps(
                        core, runtime.build_notifier(core, api), runtime.build_renderer(core)
                    )
                    return await soak.notify_report(sending, report, final=final)

            try:
                outcome = asyncio.run(send())
            except Exception as exc:
                send_error = f"soak-report: the line was not sent: {_one_line(exc)}"
            else:
                status = {
                    "sent": f"sent {label}",
                    "duplicate": f"already sent {label}, nothing to send",
                    "failed": f"send failed {label}",
                    "off": "Telegram not configured",
                }[outcome]
    if status is not None:
        typer.echo(status)
    if as_json:
        for line in body:
            typer.echo(line)
    if send_error is not None:
        _fail(send_error)


@app.command("soak-mark")
def soak_mark(
    kind: str = typer.Argument(..., help="reset, outage or clear"),
    date_: str = typer.Option(..., "--date", help="The session YYYY-MM-DD"),
    reason: str = typer.Option(..., "--reason", help="Why (1-200 characters)"),
) -> None:
    """Mark a soak day (P6-T2): `reset` restarts the count on that session (a trading-change deploy), `outage`
    makes it not clean, `clear` removes the day's mark. One `info` event (source `soak`), never relayed."""
    _setup_logging()
    from trader import runtime
    from trader.jobs import soak

    if kind not in soak.MARK_KINDS:
        _fail(f"soak-mark: the mark must be one of {', '.join(soak.MARK_KINDS)}")
    if not 1 <= len(reason.strip()) <= soak.MAX_REASON or len(reason) > soak.MAX_REASON:
        _fail(f"soak-mark: reason must be 1-{soak.MAX_REASON} characters")
    try:
        day = date.fromisoformat(date_)
    except ValueError:
        _fail(f"soak-mark: --date {date_} is not a valid date (use YYYY-MM-DD)")
    core = _core("soak-mark")
    try:
        is_session = core.calendar.is_session(day)
    except ValueError:
        is_session = False
    if not is_session:
        _fail(f"soak-mark: {day} is not a trading session")
    try:
        soak.record_mark(
            core.factory, core.clock, runtime.active_live_run_id(core.factory), kind, day, reason
        )
    except Exception as exc:
        _fail(f"soak-mark: failed: {_one_line(exc)}")
    typer.echo(f"marked {kind} {day}")


def _lower_priority() -> None:
    """A replay shares the container's CPUs with the live worker: run it at nice 10 (best effort)."""
    import os

    try:
        os.nice(10)
    except (AttributeError, OSError) as exc:
        import structlog

        structlog.get_logger("cli").warning("cli.replay_nice_failed", error_type=type(exc).__name__)


def _parse_sets(values: list[str]) -> dict[str, Any]:
    """`--set KEY=VALUE` overrides: VALUE as JSON when it parses (numbers, true/false), else as text."""
    import json

    out: dict[str, Any] = {}
    for item in values:
        key, sep, raw = item.partition("=")
        key = key.strip()
        if not sep or not key:
            _fail(f"replay: --set {_masked_line(item)} is not KEY=VALUE")
        try:
            out[key] = json.loads(raw)
        except ValueError:
            out[key] = raw
    return out


def _date_option(name: str, value: str | None) -> date:
    if not value:
        _fail("replay: give --from and --to (YYYY-MM-DD), or --run ID")
    try:
        return date.fromisoformat(value)
    except ValueError:
        _fail(f"replay: {name} {value} is not a valid date (use YYYY-MM-DD)")


class _SessionLines:
    """The replay's engine, printing one line per finished session once the engine has ended it (the day's
    trades are all closed by then). Everything else is the engine's own (`__getattr__` covers the runner's
    targeted candle pass)."""

    def __init__(self, engine: Any, factory: Any, run_id: int) -> None:
        self._engine = engine
        self._factory = factory
        self._run_id = run_id

    @property
    def broker(self) -> Any:
        return self._engine.broker

    def __getattr__(self, name: str) -> Any:
        return getattr(self._engine, name)

    async def run_event(self, event_key: str, session_date: date) -> Any:
        return await self._engine.run_event(event_key, session_date)

    async def on_candles(self, candles: Any, now: Any) -> Any:
        return await self._engine.on_candles(candles, now)

    async def tick(self, now: Any) -> None:
        await self._engine.tick(now)

    async def end_of_session(self, session_date: date) -> Any:
        result = await self._engine.end_of_session(session_date)
        try:
            typer.echo(_session_line(self._factory, self._run_id, session_date))
        except Exception as exc:  # a line not printed never stops the replay
            import structlog

            structlog.get_logger("cli").warning("cli.replay_line_failed", error_type=type(exc).__name__)
        return result


def _money(value: Any) -> str:
    from decimal import ROUND_HALF_UP, Decimal

    return str(Decimal(value).quantize(Decimal("0.01"), ROUND_HALF_UP))


def _session_line(factory: Any, run_id: int, day: date) -> str:
    """ "2026-11-24 done: 1 trade, P&L -7.20" for the replay's trades of that session."""
    from sqlalchemy import func, select

    from trader.db import models as m

    with factory() as s:
        n, pnl = s.execute(
            select(func.count(m.Trade.id), func.coalesce(func.sum(m.Trade.pnl), 0)).where(
                m.Trade.run_id == run_id, m.Trade.session_date == day
            )
        ).one()
    return f"{day} done: {n} trade{'' if n == 1 else 's'}, P&L {_money(pnl)}"


async def _run_replay(core: "Core", run: Any) -> Any:
    """`run_replay` over the real composition (`open_replay_deps`), with the per-session lines."""
    import dataclasses

    from trader.replay import runner as replay_runner

    async with replay_runner.open_replay_deps(core, data_mode=run.data_mode) as deps:
        inner = deps.engine_factory

        def engine_factory(*args: Any) -> Any:
            return _SessionLines(inner(*args), core.factory, run.id)

        return await replay_runner.run_replay(
            dataclasses.replace(deps, engine_factory=engine_factory), run.id
        )


def _replay_summary(core: "Core", final: Any) -> str:
    from trader.reports.metrics import compute_metrics

    metrics = compute_metrics(core.factory, final.id)
    expectancy = "n/a" if metrics.expectancy_r is None else f"{metrics.expectancy_r}R"
    biased = ", ".join(d.isoformat() for d in final.progress.biased_days) or "none"
    line = (
        f"replay {final.id} {final.status} ({final.data_mode} data, {final.date_from}..{final.date_to}): "
        f"trades {metrics.trades}, expectancy {expectancy}, P&L {_money(metrics.total_pnl)}, "
        f"biased days {biased}"
    )
    if final.error:
        line += f"; error: {_masked_line(final.error)}"
    return line


@app.command()
def replay(
    date_from: str | None = typer.Option(None, "--from", help="First session, YYYY-MM-DD"),
    date_to: str | None = typer.Option(None, "--to", help="Last session, YYYY-MM-DD"),
    label: str | None = typer.Option(None, "--label", help="A name for the run (at most 200 characters)"),
    offline: bool = typer.Option(False, "--offline", help="Stored data only: never call Questrade."),
    set_: list[str] = typer.Option(  # noqa: B008 (typer's option declaration)
        [],
        "--set",
        help="KEY=VALUE setting override, repeatable (VALUE is JSON when it parses, else text).",
    ),
    run_id: int | None = typer.Option(
        None, "--run", help="Run an existing queued replay (the web's launcher)."
    ),
) -> None:
    """Replay the strategies over past sessions (SPEC §8): create the run (actor `cli`) and run it here, or
    run a queued one with --run. One line per finished session and a summary. Exit 0 completed or cancelled,
    1 failed or invalid, 2 another replay is running."""
    _setup_logging(REPLAY_PROCESS)
    import asyncio

    from trader.replay import runner as replay_runner
    from trader.replay.types import (
        ReplayBusy,
        ReplayInvalid,
        ReplayNotFound,
        ReplayRequest,
        load_replay_run,
    )
    from trader.strategies.registry import StrategyRegistry

    if run_id is not None:
        if date_from or date_to or label is not None or offline or set_:
            _fail("replay: --run takes no other option")
    else:
        request = ReplayRequest(
            _date_option("--from", date_from),
            _date_option("--to", date_to),
            label=label,
            overrides=_parse_sets(set_),
            offline=offline,
        )
    _lower_priority()
    core = _core("replay", mirror=False)  # the replay's own mirror is installed once its id is known
    if run_id is None:
        registry = StrategyRegistry(core.factory, core.clock)
        try:
            run_id = replay_runner.create_replay(
                core.factory,
                core.clock,
                core.calendar,
                core.settings,
                registry,
                request,
                "cli",
                app_version=core.env.app_version,
            )
        except ReplayInvalid as exc:
            for loc, msg in exc.errors:
                typer.echo(f"replay: {loc}: {_masked_line(msg)}", err=True)
            raise typer.Exit(1) from None
        except ReplayBusy:
            _fail("replay: another replay is queued or running", code=EXIT_BUSY)
        except Exception as exc:
            _fail(f"replay: failed: {_one_line(exc)}")
    try:
        run = load_replay_run(core.factory, run_id)
    except ReplayNotFound:
        _fail(f"replay: {run_id} is not a replay run")
    except Exception as exc:
        _fail(f"replay {run_id}: failed: {_one_line(exc)}")
    _install_mirror(core, REPLAY_PROCESS, run_id=run.id)
    if run.status != "queued":
        _fail(f"replay {run.id}: it is {run.status}, not queued: nothing to run")
    typer.echo(f"replay {run.id}: {run.date_from}..{run.date_to}, {run.data_mode} data")
    try:
        final = asyncio.run(_run_replay(core, run))
    except ReplayBusy:
        _fail(f"replay {run.id}: another replay is running", code=EXIT_BUSY)
    except Exception as exc:
        _fail(f"replay {run.id}: failed: {_one_line(exc)}")
    typer.echo(_replay_summary(core, final))
    if final.status not in ("completed", "cancelled"):
        raise typer.Exit(1)


# --- P6-T11: the decision log -------------------------------------------------------------------------------
# `trader decisions record|show|export|prune`. None writes a `job_runs` row (they are not jobs and not in
# `ManualJob`). Exit 0, or 1 on a database error or an unknown run. A date that is not a trading session
# prints "not a trading session" and exits 0.

decisions_app = typer.Typer(no_args_is_help=True, add_completion=False, help="The decision log (P6-T11).")
app.add_typer(decisions_app, name="decisions")

RUN_OPTION = typer.Option(None, "--run", help="A run id (default: the live run)")
DAY_OPTION = typer.Option(..., "--date", help="The session YYYY-MM-DD")
EXPORT_SPOOL_BYTES = 8 * 1024 * 1024  # `decisions export` buffers in memory up to this, then on disk


def _decisions_day(core: "Core", name: str, date_: str) -> date | None:
    """--date as a session, or None (after printing "not a trading session") when it isn't one. A bad date
    exits 1."""
    try:
        day = date.fromisoformat(date_)
    except ValueError:
        _fail(f"{name}: --date {date_} is not a valid date (use YYYY-MM-DD)")
    try:
        is_session = core.calendar.is_session(day)
    except ValueError:
        is_session = False
    if not is_session:
        typer.echo(f"{name} {day}: not a trading session, nothing to do")
        return None
    return day


def _decisions_run(core: "Core", name: str, run_id: int | None) -> tuple[int, str]:
    """(id, mode) of --run, else of the live run (the active one, else the latest). Exit 1 when unknown."""
    from trader.decisions import read

    try:
        found = read.resolve_run(core.factory, run_id)
    except Exception as exc:
        _fail(f"{name}: failed: {_one_line(exc)}")
    if found is None:
        _fail(f"{name}: unknown run {run_id}" if run_id is not None else f"{name}: there is no live run")
    return found


@decisions_app.command("record")
def decisions_record(
    date_: str = DAY_OPTION,
    run_id: int | None = RUN_OPTION,
    final: bool = typer.Option(False, "--final", help="Freeze the day (the post-close does this)"),
    rebuild: bool = typer.Option(
        False, "--rebuild", help="Rebuild even if nothing changed (a final day stays final)"
    ),
) -> None:
    """Record a day's decisions now (idempotent: an unchanged or final day is skipped unless --rebuild)."""
    _setup_logging()
    import asyncio
    import dataclasses

    from trader import runtime
    from trader.decisions.recorder import record_day
    from trader.replay.types import load_replay_run

    name = "decisions record"
    core = _core(name)
    day = _decisions_day(core, name, date_)
    if day is None:
        return
    rid, mode = _decisions_run(core, name, run_id)
    deps = runtime.recorder_deps(core)
    try:
        if mode == "replay":  # a replay's rows use its own settings snapshot, never the live settings
            snapshot = load_replay_run(core.factory, rid).settings
            deps = dataclasses.replace(deps, settings=lambda: snapshot)
        result = asyncio.run(record_day(deps, rid, day, final=final, rebuild=rebuild))
    except Exception as exc:
        _fail(f"{name} {day} run {rid}: failed: {_one_line(exc)}")
    frozen = ", final" if result.final else ""
    if result.skipped is not None:
        typer.echo(f"{name} {day} run {rid}: skipped ({result.skipped}{frozen})")
    else:
        typer.echo(f"{name} {day} run {rid}: {sum(result.stages.values())} rows{frozen}")


@decisions_app.command("show")
def decisions_show(
    date_: str = DAY_OPTION,
    run_id: int | None = RUN_OPTION,
    stage: str | None = typer.Option(None, "--stage", help="Only this stage (scan, proposal, fill, ...)"),
    outcome: str | None = typer.Option(None, "--outcome", help="Only this outcome (passed, rejected, ...)"),
    limit: int = typer.Option(200, "--limit", min=1, max=5000, help="At most this many rows"),
) -> None:
    """The day's summary, then one line per row: time (MT), stage, ticker, outcome, rule."""
    _setup_logging()
    from zoneinfo import ZoneInfo

    from trader.decisions import read
    from trader.decisions.types import OUTCOMES, STAGE_ORDER

    name = "decisions show"
    if stage is not None and stage not in STAGE_ORDER:
        _fail(f"{name}: --stage must be one of {', '.join(STAGE_ORDER)}", code=2)
    if outcome is not None and outcome not in OUTCOMES:
        _fail(f"{name}: --outcome must be one of {', '.join(OUTCOMES)}", code=2)
    core = _core(name)
    day = _decisions_day(core, name, date_)
    if day is None:
        return
    if run_id is not None:
        _decisions_run(core, name, run_id)
    try:
        view = read.load_day(
            core.factory,
            run_id,
            day,
            stage=stage,  # checked against STAGE_ORDER above
            outcome=outcome,  # checked against OUTCOMES above
            limit=limit,
        )
    except Exception as exc:
        _fail(f"{name} {day}: failed: {_one_line(exc)}")
    if view is None:
        typer.echo(f"{name} {day}: no decisions recorded")
        return
    tz = ZoneInfo(core.env.tz_display)
    state = "final" if view.final else "not final"
    typer.echo(f"decisions {day} run {view.run_id} ({view.run_mode}, {state}), {view.total} rows")
    if view.summary_text:
        for line in view.summary_text.splitlines():
            typer.echo(_masked_line(line))
    for r in view.rows:
        at = r.ts.astimezone(tz).strftime("%H:%M:%S")
        ticker, rule = _masked_line(r.ticker or "-"), _masked_line(r.rule or "-")
        typer.echo(f"{at} MT  {r.stage:<11} {ticker:<8} {r.outcome:<13} {rule}")
    if view.total > len(view.rows):
        typer.echo(f"... {view.total - len(view.rows)} more (use --limit)")


@decisions_app.command("export")
def decisions_export(
    date_: str = DAY_OPTION,
    run_id: int | None = RUN_OPTION,
    out: str | None = typer.Option(None, "--out", help="Write the CSV here (default: stdout)"),
) -> None:
    """The day's decisions as CSV (the same file as the web's Download CSV)."""
    _setup_logging()
    import os
    import shutil
    import sys
    import tempfile
    from pathlib import Path

    from trader.decisions import read
    from trader.decisions.export import decisions_csv

    name = "decisions export"
    core = _core(name)
    day = _decisions_day(core, name, date_)
    if day is None:
        return
    if run_id is not None:
        _decisions_run(core, name, run_id)
    try:
        with core.factory() as s:
            picked = read.pick_run(s, run_id, day)
            has = picked is not None and read.has_rows(s, picked[0], day)
    except Exception as exc:
        _fail(f"{name} {day}: failed: {_one_line(exc)}")
    if picked is None or not has:
        typer.echo(f"{name} {day}: no decisions recorded", err=True)
        return
    # The whole CSV is read into a spooled buffer first: nothing is emitted (stdout) or put in place (--out,
    # written to a side file and renamed) unless the read completed, so a database error mid-export never
    # leaves a truncated CSV that looks whole (gauntlet fix). Exit 1 on any failure.
    target = None if out is None else Path(out)
    part = None if target is None else target.with_name(f".{target.name}.part")
    try:
        with tempfile.SpooledTemporaryFile(
            max_size=EXPORT_SPOOL_BYTES, mode="w+", encoding="utf-8", newline=""
        ) as buf:
            lines = decisions_csv(core.factory, picked[0], day)
            try:
                for line in lines:
                    buf.write(line)
            finally:
                lines.close()
            buf.seek(0)
            if target is None or part is None:
                shutil.copyfileobj(buf, sys.stdout)
                sys.stdout.flush()
            else:
                with part.open("w", encoding="utf-8", newline="") as f:
                    shutil.copyfileobj(buf, f)
                os.replace(part, target)
    except Exception as exc:
        if part is not None:
            part.unlink(missing_ok=True)
        _fail(f"{name} {day}: failed: {_one_line(exc)}")
    if out is not None:
        typer.echo(f"{name} {day} run {picked[0]}: written to {out}")


@decisions_app.command("prune")
def decisions_prune() -> None:
    """Delete decision rows past their retention: reports.decisions_retention_days for live runs,
    reports.decisions_replay_retention_days for replays (the post-close does this every session)."""
    _setup_logging()
    from trader import runtime
    from trader.decisions.prune import prune

    name = "decisions prune"
    core = _core(name)
    try:
        result = prune(core.factory, core.clock, runtime.quiet_settings(core)())
    except Exception as exc:
        _fail(f"{name}: failed: {_one_line(exc)}")
    typer.echo(f"{name}: deleted {result.live_deleted} live rows, {result.replay_deleted} replay rows")
