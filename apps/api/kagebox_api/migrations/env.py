"""Alembic environment: uses the app's engine factory so secrets come from files."""

from __future__ import annotations

from alembic import context

from pipeline.common.database import make_engine
from pipeline.common.dbmodels import Base
from pipeline.common.settings import get_settings

target_metadata = Base.metadata


def run() -> None:
    engine = make_engine(get_settings())
    with engine.connect() as conn:
        context.configure(connection=conn, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


run()
