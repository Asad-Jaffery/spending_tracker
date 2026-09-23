from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


def build_engine(database_url: str):
    url = make_url(database_url)
    if url.drivername in {"postgres", "postgresql"}:
        url = url.set(drivername="postgresql+psycopg")
    options = {"pool_pre_ping": True}
    if url.get_backend_name() == "sqlite":
        if url.database and url.database != ":memory:":
            Path(url.database).parent.mkdir(parents=True, exist_ok=True)
        options["connect_args"] = {"check_same_thread": False, "timeout": 30}
        if not url.database or url.database == ":memory:":
            options["poolclass"] = StaticPool
    engine = create_engine(url, **options)
    if url.get_backend_name() == "sqlite":

        @event.listens_for(engine, "connect")
        def sqlite_pragmas(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")

    return engine


def session_factory(engine):
    return sessionmaker(bind=engine, expire_on_commit=False)
