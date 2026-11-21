"""scenes, stage runs, job stage

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-21 10:00:00

- scenes: one SceneJSON per panel (unique panel_id), cascading with its panel and chapter.
- stage_runs: per-(chapter, stage) run lock with owner, heartbeat and outcome.
- jobs.stage: which pipeline stage a job belongs to. NOT NULL with a constant server default
  'ingest', so existing rows are filled without a rewrite on Postgres 11+ and every earlier job
  (all of them ingest) reads correctly.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "scenes",
        sa.Column("id", sa.String(length=80), nullable=False),
        sa.Column("panel_id", sa.String(length=80), nullable=False),
        sa.Column("chapter_id", sa.String(length=80), nullable=False),
        sa.Column("params_hash", sa.String(length=64), nullable=False),
        sa.Column("artifact", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["panel_id"], ["panels.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["chapter_id"], ["chapters.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("panel_id"),
    )
    op.create_index("ix_scenes_chapter_id", "scenes", ["chapter_id"])
    op.create_table(
        "stage_runs",
        sa.Column("chapter_id", sa.String(length=80), nullable=False),
        sa.Column("stage", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("run_id", sa.String(length=80), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("counts", sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(["chapter_id"], ["chapters.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("chapter_id", "stage"),
    )
    op.add_column(
        "jobs",
        sa.Column("stage", sa.String(length=16), nullable=False, server_default="ingest"),
    )


def downgrade() -> None:
    # batch mode: SQLite before 3.35 cannot DROP COLUMN in place
    with op.batch_alter_table("jobs") as batch:
        batch.drop_column("stage")
    op.drop_table("stage_runs")
    op.drop_index("ix_scenes_chapter_id", table_name="scenes")
    op.drop_table("scenes")
