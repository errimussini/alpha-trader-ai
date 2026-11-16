"""ai committee: ai_runs e jobs

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-16
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "ai_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("agent", sa.String(48)),
        sa.Column("trade_plan_id", sa.Integer(), sa.ForeignKey("trade_plans.id", ondelete="SET NULL", name="fk_ai_runs_plan")),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL", name="fk_ai_runs_user")),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("model", sa.String(64)),
        sa.Column("prompt_version", sa.String(48), nullable=False),
        sa.Column("prompt_hash", sa.String(64)),
        sa.Column("input_hash", sa.String(64)),
        sa.Column("inputs", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("output", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("validation", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("usage", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("request_id", sa.String(128)),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("error", sa.Text()),
        sa.Column("latency_ms", sa.Integer()),
        sa.Column("created_at", TS, nullable=False),
    )
    op.create_index("ix_ai_runs_kind", "ai_runs", ["kind"])
    op.create_index("ix_ai_runs_trade_plan_id", "ai_runs", ["trade_plan_id"])
    op.create_index("ix_ai_runs_created_at", "ai_runs", ["created_at"])
    op.create_table(
        "jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("idempotency_key", sa.String(96), unique=True),
        sa.Column("payload", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("result", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("error", sa.Text()),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("requested_by", sa.String(255), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("started_at", TS),
        sa.Column("heartbeat_at", TS),
        sa.Column("finished_at", TS),
    )
    op.create_index("ix_jobs_kind", "jobs", ["kind"])
    op.create_index("ix_jobs_status", "jobs", ["status"])


def downgrade() -> None:
    op.drop_table("jobs")
    op.drop_table("ai_runs")
