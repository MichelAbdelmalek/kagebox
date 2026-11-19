"""bubble reading order, chapter ingest lock

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-19 16:00:00

- bubbles.reading_order (1-based within the panel) with a unique (panel_id, reading_order).
  Existing rows are backfilled in id order (ingest handed out ids in reading order before this
  revision) with a portable correlated subquery, then the column becomes NOT NULL.
- chapters.ingest_started_at: when the current ingest run took the chapter (stale-run takeover).
- chapters.ingest_run_id: the owning run's token (its Prefect flow-run id).

Artifacts written before this revision lack the new Page / Bubble fields (source_page,
reading_order); re-run ingest for such chapters.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

UQ = "uq_bubbles_panel_id_reading_order"


def upgrade() -> None:
    op.add_column("chapters", sa.Column("ingest_started_at", sa.DateTime(timezone=True)))
    op.add_column("chapters", sa.Column("ingest_run_id", sa.String(length=80)))
    op.add_column("bubbles", sa.Column("reading_order", sa.Integer(), nullable=True))
    op.execute(
        "UPDATE bubbles SET reading_order = ("
        "SELECT COUNT(*) FROM bubbles AS b2"
        " WHERE b2.panel_id = bubbles.panel_id AND b2.id <= bubbles.id)"
    )
    # batch mode: SQLite cannot ALTER a column or add a constraint in place
    with op.batch_alter_table("bubbles") as batch:
        batch.alter_column("reading_order", existing_type=sa.Integer(), nullable=False)
        batch.create_unique_constraint(UQ, ["panel_id", "reading_order"])


def downgrade() -> None:
    with op.batch_alter_table("bubbles") as batch:
        batch.drop_constraint(UQ, type_="unique")
        batch.drop_column("reading_order")
    with op.batch_alter_table("chapters") as batch:
        batch.drop_column("ingest_run_id")
        batch.drop_column("ingest_started_at")
