"""Wires the long-lived objects from the environment. Every process builds one Core."""

from dataclasses import dataclass

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from trader.config import EnvSettings, get_env
from trader.crypto import Crypto
from trader.db.session import make_engine, make_session_factory
from trader.logging_setup import quiet_http_loggers
from trader.market.calendar import SessionCalendar
from trader.market.clock import Clock, RealClock
from trader.settings_store import SettingsStore


@dataclass
class Core:
    env: EnvSettings
    engine: Engine
    factory: sessionmaker[Session]
    crypto: Crypto
    clock: Clock
    calendar: SessionCalendar
    settings: SettingsStore


def build_core(env: EnvSettings | None = None) -> Core:
    quiet_http_loggers()
    env = env or get_env()
    engine = make_engine(env.database_url.get_secret_value())
    factory = make_session_factory(engine)
    clock = RealClock()
    return Core(
        env=env,
        engine=engine,
        factory=factory,
        crypto=Crypto(env.app_encryption_key.get_secret_value()),
        clock=clock,
        calendar=SessionCalendar(),
        settings=SettingsStore(factory, now=clock.now),
    )
