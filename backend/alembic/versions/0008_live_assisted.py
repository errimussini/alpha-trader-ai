"""live assistito: live_order_tickets (architettura, spenta di default)

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-16
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)
PRICE = sa.Numeric(20, 8)
MONEY = sa.Numeric(20, 4)


def upgrade() -> None:
    op.create_table(
        "live_order_tickets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_by_id", sa.Integer(), sa.ForeignKey("users.id", name="fk_live_tickets_creator"), nullable=False),
        sa.Column("trade_plan_id", sa.Integer(), sa.ForeignKey("trade_plans.id", name="fk_live_tickets_plan")),
        sa.Column("instrument_id", sa.Integer(), sa.ForeignKey("instruments.id", name="fk_live_tickets_instrument"),
                  nullable=False),
        sa.Column("client_order_id", sa.String(64), nullable=False),
        sa.Column("broker", sa.String(32), nullable=False),
        sa.Column("side", sa.String(4), nullable=False),
        sa.Column("order_type", sa.String(8), nullable=False, server_default="LIMIT"),
        sa.Column("quantity", sa.Numeric(20, 6), nullable=False),
        sa.Column("limit_price", PRICE, nullable=False),
        sa.Column("stop_loss", PRICE),
        sa.Column("take_profit_1", PRICE),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("fx_rate", sa.Numeric(20, 10), nullable=False),
        sa.Column("notional_base", MONEY, nullable=False),
        sa.Column("est_loss_at_stop_base", MONEY),
        sa.Column("quote_id", sa.Integer(), sa.ForeignKey("market_quotes.id", name="fk_live_tickets_quote")),
        sa.Column("quote_price", PRICE, nullable=False),
        sa.Column("quote_time", TS, nullable=False),
        sa.Column("risk_snapshot", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("warnings", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("status_reason", sa.Text()),
        sa.Column("code_hash", sa.String(64), nullable=False),
        sa.Column("failed_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("expires_at", TS, nullable=False),
        sa.Column("confirmed_by_id", sa.Integer(), sa.ForeignKey("users.id", name="fk_live_tickets_confirmer")),
        sa.Column("confirmed_at", TS),
        sa.Column("broker_order_id", sa.String(64)),
        sa.Column("broker_response", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("sends_real_orders", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.UniqueConstraint("client_order_id", name="uq_live_tickets_client_order_id"),
    )
    op.create_index("ix_live_order_tickets_status", "live_order_tickets", ["status"])


def downgrade() -> None:
    op.drop_index("ix_live_order_tickets_status", table_name="live_order_tickets")
    op.drop_table("live_order_tickets")
