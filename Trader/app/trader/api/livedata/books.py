"""The books check (live dashboard plan S3): cash + positions at cost = starting cash + realised − fees, to
the cent. DB-T1 stub with the final signature; DB-T3 implements it."""

from sqlalchemy.orm import Session, sessionmaker

from trader.api.schemas import BooksCheckOut


def books_check(factory: sessionmaker[Session], run_id: int) -> BooksCheckOut:
    raise NotImplementedError("DB-T3")
