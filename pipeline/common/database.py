"""Database engine factory shared by the API and the workers."""

from __future__ import annotations

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.pool import StaticPool

from pipeline.common.settings import Settings


def make_engine(settings: Settings) -> Engine:
    url = make_url(settings.database_url)
    if url.get_backend_name() == "sqlite":
        engine = create_engine(url, connect_args={"check_same_thread": False}, poolclass=StaticPool)
        # Enforce foreign keys like Postgres does, so tests catch ordering bugs.
        event.listen(engine, "connect", lambda conn, _: conn.execute("PRAGMA foreign_keys=ON"))
        return engine
    password = settings.secret("database_password")
    if password is not None:
        url = url.set(password=password.get_secret_value())
    return create_engine(url, pool_pre_ping=True, hide_parameters=True)
