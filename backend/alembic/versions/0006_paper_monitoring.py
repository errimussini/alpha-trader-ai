"""paper monitoring: ordini aperti, livelli di posizione, dedup degli alert, snapshot dell'equity

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-16
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)
PRICE = sa.Numeric(20, 8)
MONEY = sa.Numeric(20, 4)


def upgrade() -> None:
    op.add_column("orders", sa.Column("stop_loss", PRICE))
    op.add_column("orders", sa.Column("take_profit_1", PRICE))
    op.add_column("orders", sa.Column("valid_until", TS))
    op.add_column("orders", sa.Column("purpose", sa.String(8), nullable=False, server_default="MANUAL"))
    op.add_column("orders", sa.Column("last_checked_at", TS))

    op.add_column("positions", sa.Column("high_water", PRICE))
    op.add_column("positions", sa.Column("tp1_done", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("positions", sa.Column("trailing_active", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("positions", sa.Column("trailing_distance", PRICE))
    op.add_column("positions", sa.Column("last_checked_at", TS))

    op.add_column("alerts", sa.Column("dedup_key", sa.String(160)))
    op.add_column("alerts", sa.Column("notified_at", TS))
    op.create_index("ix_alerts_dedup_key", "alerts", ["dedup_key"])

    op.create_table(
        "equity_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("portfolio_id", sa.Integer(), sa.ForeignKey("portfolios.id", ondelete="CASCADE",
                                                              name="fk_equity_snapshots_portfolio"), nullable=False),
        sa.Column("snap_date", sa.Date(), nullable=False),
        sa.Column("equity", MONEY, nullable=False),
        sa.Column("cash", MONEY, nullable=False),
        sa.Column("market_value", MONEY, nullable=False),
        sa.Column("realized_pnl", MONEY, nullable=False),
        sa.Column("unrealized_pnl", MONEY, nullable=False),
        sa.Column("drawdown_pct", sa.Numeric(10, 4), nullable=False),
        sa.Column("aggregate_risk_pct", sa.Numeric(10, 4), nullable=False),
        sa.Column("open_positions", sa.Integer(), nullable=False),
        sa.Column("stale", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_demo", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("taken_at", TS, nullable=False),
        sa.UniqueConstraint("portfolio_id", "snap_date", name="uq_equity_snapshot_day"),
    )
    op.create_index("ix_equity_snapshots_portfolio_id", "equity_snapshots", ["portfolio_id"])


def downgrade() -> None:
    op.drop_index("ix_equity_snapshots_portfolio_id", table_name="equity_snapshots")
    op.drop_table("equity_snapshots")
    op.drop_index("ix_alerts_dedup_key", table_name="alerts")
    for c in ("notified_at", "dedup_key"):
        op.drop_column("alerts", c)
    for c in ("last_checked_at", "trailing_distance", "trailing_active", "tp1_done", "high_water"):
        op.drop_column("positions", c)
    for c in ("last_checked_at", "purpose", "valid_until", "take_profit_1", "stop_loss"):
        op.drop_column("orders", c)
