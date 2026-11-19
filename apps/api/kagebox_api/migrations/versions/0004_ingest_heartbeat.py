"""ingest heartbeat

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-19 20:00:00

- chapters.ingest_heartbeat_at (nullable, timestamptz): the owning ingest run's last sign of
  life, written at start and after every page. Staleness (takeover by a new run or a new
  source, and the API reporting a dead run as interrupted) is judged on it, falling back to
  ingest_started_at while it is null. Existing rows keep null, so they fall back exactly as
  before. A plain nullable ADD COLUMN: no table rewrite or backfill on Postgres.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "chapters", sa.Column("ingest_heartbeat_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    # batch mode: SQLite before 3.35 cannot DROP COLUMN in place
    with op.batch_alter_table("chapters") as batch:
        batch.drop_column("ingest_heartbeat_at")
