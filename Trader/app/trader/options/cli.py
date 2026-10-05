"""The `trader options-*` commands (OPTSIM task plan §3.6). `register_options_cli(app)` adds them to the
`trader` command line; `trader.cli` calls it.

`options-run new` is complete here. The other four dispatch to `trader.options.runtime.run_cli_command`
(written by T16); until that module exists they print one line and exit 1. Imports are inside the commands,
as everywhere in `trader.cli`, so `trader --help` stays fast and `trader.cli` can import this module.
"""

import importlib
from collections.abc import Callable
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, NoReturn

import typer

NOT_WIRED = "options runtime is not wired yet"
RUNTIME_MODULE = "trader.options.runtime"
OPTIONS_RUN_ACTOR = "cli:options-run"
MAX_CASH = Decimal("10000000")

options_run_app = typer.Typer(no_args_is_help=True, add_completion=False, help="The options run (OPTSIM).")

DATE_OPTION = typer.Option(None, "--date", help="Session YYYY-MM-DD (default: today in ET)")
FORCE_OPTION = typer.Option(False, "--force", help="Re-run a session that already succeeded.")


def _base() -> Any:
    from trader import cli

    return cli


def _fail(message: str, code: int = 1) -> NoReturn:
    typer.echo(message, err=True)
    raise typer.Exit(code)


def _runtime_command(name: str) -> Callable[..., int]:
    """`trader.options.runtime.run_cli_command`, or exit 1 with one clear line while it does not exist."""
    try:
        module = importlib.import_module(RUNTIME_MODULE)
    except ModuleNotFoundError as exc:
        if exc.name != RUNTIME_MODULE:  # the runtime exists but one of its own imports is missing
            _fail(f"{name}: failed: {_base()._one_line(exc)}")
        _fail(f"{name}: {NOT_WIRED}")
    command: Callable[..., int] = module.run_cli_command
    return command


def _date(name: str, value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        _fail(f"{name}: --date {value} is not a valid date (use YYYY-MM-DD)")


def _dispatch(name: str, command: str, *, date_: str | None, force: bool, **kwargs: Any) -> None:
    _base()._setup_logging()
    session_date = _date(name, date_)
    run = _runtime_command(name)
    raise typer.Exit(run(command, session_date=session_date, force=force, **kwargs))


def _cash(value: str | None, default: Decimal) -> Decimal:
    if value is None:
        return default
    try:
        cash = Decimal(value)
    except InvalidOperation:
        _fail(f"options-run new: --cash {value} is not a number")
    if not cash.is_finite() or cash <= 0 or cash > MAX_CASH:
        _fail(f"options-run new: --cash must be above 0 and at most {MAX_CASH}")
    return cash


@options_run_app.command("new")
def options_run_new(
    cash: str | None = typer.Option(
        None, "--cash", help="Starting cash in USD (default: the setting options.starting_cash)"
    ),
    confirm: bool = typer.Option(False, "--confirm", help="Required: retire the active options run."),
) -> None:
    """Retire the active options run (kept, status completed) and start a new one with a fresh USD account.
    Refused while a structure is open or an order is working, and from 09:15 to 16:30 ET on a session day.
    Without --confirm it only prints what it would do. The stock live run is never touched."""
    base = _base()
    base._setup_logging()
    from trader.options.account import OptionsRunRefused, active_options_run_id, start_options_run
    from trader.options.settings import OptionSettingsStore

    name = "options-run new"
    core = base._core(name)
    try:
        settings = OptionSettingsStore(core.factory, core.clock.now).load()
    except Exception as exc:
        _fail(f"{name}: failed: the option settings can't be read: {type(exc).__name__}")
    amount = _cash(cash, settings.starting_cash)
    if not confirm:
        try:
            active = active_options_run_id(core.factory)
        except Exception as exc:
            _fail(f"{name}: failed: {base._one_line(exc)}")
        retire = f"retire options run {active} and " if active is not None else ""
        typer.echo(
            f"{name}: would {retire}start a new options run with {amount} USD; "
            "nothing was changed, add --confirm to do it"
        )
        return
    try:
        out = start_options_run(
            core.factory, core.clock, core.calendar, settings, cash=amount, actor=OPTIONS_RUN_ACTOR
        )
    except OptionsRunRefused as exc:
        _fail(f"{name}: refused: {base._masked_line(str(exc))}")
    except Exception as exc:
        _fail(f"{name}: failed: {base._one_line(exc)}")
    if out.retired_run_id is not None:
        typer.echo(f"retired options run {out.retired_run_id} (status completed, rows kept)")
    typer.echo(f"started options run {out.run_id}: cash {amount.quantize(Decimal('0.0001'))} USD")


def options_check(
    symbol: str = typer.Option("F", "--symbol", help="The underlying to check"),
) -> None:
    """Read-only live check of an option chain, its quotes and the symbol details."""
    _dispatch("options-check", "check", date_=None, force=False, symbol=symbol.upper())


def options_refresh(date_: str | None = DATE_OPTION, force: bool = FORCE_OPTION) -> None:
    """Morning refresh: facts, chains and daily bars of every watched underlying (08:15 ET)."""
    _dispatch("options-refresh", "refresh", date_=date_, force=force)


def options_postclose(date_: str | None = DATE_OPTION, force: bool = FORCE_OPTION) -> None:
    """Post-close: day-order expiry, option expiry and assignment, marks, snapshot, summary (16:20 ET)."""
    _dispatch("options-postclose", "postclose", date_=date_, force=force)


def options_event(
    strategy: str | None = typer.Argument(None, help="The strategy key, e.g. wheel"),
    key: str | None = typer.Argument(None, help="The event key, e.g. opt_daily"),
    due: bool = typer.Option(False, "--due", help="Fire every due strategy event that has not run yet."),
    date_: str | None = DATE_OPTION,
    force: bool = typer.Option(False, "--force", help="With STRATEGY KEY: run it even if it already ran."),
) -> None:
    """Fire one strategy event (STRATEGY KEY), or every due and unfired one (--due)."""
    name = "options-event"
    if due and (strategy is not None or key is not None):
        _fail(f"{name}: give STRATEGY KEY or --due (not both)", code=2)
    if not due and (strategy is None or key is None):
        _fail(f"{name}: give STRATEGY and KEY, or --due", code=2)
    if due and force:
        _fail(
            f"{name}: --due --force is refused: it would re-run every event that already ran today. "
            "Use `options-event STRATEGY KEY --force` for one event.",
            code=2,
        )
    _dispatch(name, "event", date_=date_, force=force, strategy=strategy, key=key, due=due)


def register_options_cli(app: typer.Typer) -> None:
    """Add the option commands to the `trader` command line."""
    app.add_typer(options_run_app, name="options-run")
    app.command("options-check")(options_check)
    app.command("options-refresh")(options_refresh)
    app.command("options-postclose")(options_postclose)
    app.command("options-event")(options_event)
