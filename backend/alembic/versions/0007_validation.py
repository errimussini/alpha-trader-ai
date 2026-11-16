"""validazione prospettica: plan_outcomes

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-16
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)
PRICE = sa.Numeric(20, 8)
R = sa.Numeric(10, 4)


def upgrade() -> None:
    op.create_table(
        "plan_outcomes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("trade_plan_id", sa.Integer(), sa.ForeignKey("trade_plans.id", ondelete="CASCADE",
                                                               name="fk_plan_outcomes_plan"), nullable=False),
        sa.Column("instrument_id", sa.Integer(), sa.ForeignKey("instruments.id", name="fk_plan_outcomes_instrument"),
                  nullable=False),
        sa.Column("model_version", sa.String(64), nullable=False),
        sa.Column("setup", sa.String(24)),
        sa.Column("score", sa.Numeric(6, 2)),
        sa.Column("status_at_creation", sa.String(10), nullable=False),
        sa.Column("committee_decision", sa.String(8)),
        sa.Column("created_for", sa.Date(), nullable=False),
        sa.Column("outcome", sa.String(14), nullable=False, server_default="PENDING"),
        sa.Column("entry_date", sa.Date()),
        sa.Column("entry_price", PRICE),
        sa.Column("exit_date", sa.Date()),
        sa.Column("exit_price", PRICE),
        sa.Column("r_gross", R),
        sa.Column("r_net", R),
        sa.Column("mfe_r", R),
        sa.Column("mae_r", R),
        sa.Column("bars_evaluated", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_bar_date", sa.Date()),
        sa.Column("data_source", sa.String(64)),
        sa.Column("is_demo", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("engine_version", sa.String(32), nullable=False),
        sa.Column("notes", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.Column("resolved_at", TS),
        sa.UniqueConstraint("trade_plan_id", name="uq_plan_outcomes_plan"),
    )
    op.create_index("ix_plan_outcomes_instrument_id", "plan_outcomes", ["instrument_id"])
    op.create_index("ix_plan_outcomes_outcome", "plan_outcomes", ["outcome"])


def downgrade() -> None:
    op.drop_index("ix_plan_outcomes_outcome", table_name="plan_outcomes")
    op.drop_index("ix_plan_outcomes_instrument_id", table_name="plan_outcomes")
    op.drop_table("plan_outcomes")
