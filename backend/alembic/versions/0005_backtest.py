"""backtest: backtest_runs e calibrazione dei piani

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-16
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "backtest_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("strategy_version_id", sa.Integer(), sa.ForeignKey("strategy_versions.id", name="fk_backtest_runs_sv")),
        sa.Column("job_id", sa.Integer(), sa.ForeignKey("jobs.id", ondelete="SET NULL", name="fk_backtest_runs_job")),
        sa.Column("engine_version", sa.String(32), nullable=False),
        sa.Column("params", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("universe", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("period_start", sa.Date()),
        sa.Column("period_end", sa.Date()),
        sa.Column("summary", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("results", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("error", sa.Text()),
        sa.Column("is_demo", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("requested_by", sa.String(255), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("finished_at", TS),
    )
    op.create_index("ix_backtest_runs_status", "backtest_runs", ["status"])
    op.add_column("trade_plans", sa.Column("calibration", sa.JSON(), nullable=False, server_default="{}"))


def downgrade() -> None:
    op.drop_column("trade_plans", "calibration")
    op.drop_table("backtest_runs")
