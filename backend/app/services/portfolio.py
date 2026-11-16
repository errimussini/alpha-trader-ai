"""Valutazione del portafoglio operativo simulato (in valuta base).

Le posizioni sono valorizzate all'ultima quotazione memorizzata; se manca o è obsoleta
la valutazione viene marcata come tale (non viene mai stimato un prezzo).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.entities import CashMovement, Portfolio, Position, User
from app.services import audit
from app.services.market_data import latest_fx, latest_quote

_M = Decimal("0.01")


def default_risk_params() -> dict:
    s = get_settings()
    return {k: str(getattr(s, k)) for k in (
        "risk_per_trade_pct", "max_aggregate_risk_pct", "max_position_exposure_pct", "max_sector_exposure_pct",
        "min_cash_reserve_pct", "max_daily_loss_pct", "max_total_drawdown_pct",
    )}


def risk_param(p: Portfolio, key: str) -> Decimal:
    v = (p.risk_params or {}).get(key)
    return Decimal(v) if v is not None else Decimal(str(getattr(get_settings(), key)))


def get_or_create_paper_portfolio(db: Session, user: User) -> Portfolio:
    p = db.execute(select(Portfolio).where(Portfolio.owner_id == user.id, Portfolio.mode == "PAPER")
                   .order_by(Portfolio.id)).scalars().first()
    if p:
        return p
    s = get_settings()
    cap = s.initial_paper_capital
    p = Portfolio(owner_id=user.id, name="Portafoglio simulato", mode="PAPER", base_currency=s.base_currency,
                  initial_capital=cap, cash=cap, peak_equity=cap, realized_pnl=Decimal("0"),
                  risk_params=default_risk_params())
    db.add(p)
    db.flush()
    db.add(CashMovement(portfolio_id=p.id, kind="DEPOSIT", amount_base=cap, currency=s.base_currency,
                        reference="capitale iniziale simulato"))
    audit.record(db, actor=user.email, action="portfolio.created", entity="portfolio", entity_id=p.id,
                 payload={"initial_capital": cap, "mode": "PAPER"})
    return p


@dataclass
class PositionValuation:
    position: Position
    price: Decimal | None
    price_time: datetime | None
    price_source: str | None
    fx_rate: Decimal
    fx_is_estimate: bool
    market_value_base: Decimal
    cost_base: Decimal
    unrealized_pnl_base: Decimal
    risk_to_stop_base: Decimal | None
    stale: bool
    is_demo: bool


@dataclass
class PortfolioValuation:
    portfolio: Portfolio
    cash: Decimal
    invested_cost: Decimal
    market_value: Decimal
    equity: Decimal
    unrealized_pnl: Decimal
    realized_pnl: Decimal
    cumulative_return_pct: Decimal
    drawdown_pct: Decimal
    aggregate_risk_base: Decimal
    aggregate_risk_pct: Decimal
    positions: list[PositionValuation] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    valued_at: datetime = field(default_factory=lambda: datetime.now(UTC))


def open_positions(db: Session, portfolio_id: int) -> list[Position]:
    return list(db.execute(select(Position).where(Position.portfolio_id == portfolio_id, Position.status == "OPEN")
                           .order_by(Position.opened_at)).scalars())


def value_position(db: Session, pos: Position, base: str) -> PositionValuation:
    s = get_settings()
    q = latest_quote(db, pos.instrument_id)
    fxq = latest_fx(db, base, pos.currency)
    fx = fxq.price if fxq else (Decimal("1") if pos.currency == base else pos.fx_rate_at_open)
    fx_est = pos.currency != base and fxq is None
    cost = pos.quantity * pos.avg_price / pos.fx_rate_at_open
    if q is not None:
        mv = pos.quantity * q.price / fx
        age = datetime.now(UTC) - (q.quote_time if q.quote_time.tzinfo else q.quote_time.replace(tzinfo=UTC))
        stale = age > timedelta(minutes=s.max_quote_age_minutes)
    else:
        mv, stale = cost, True
    risk = None
    if pos.stop_loss is not None:
        ref = q.price if q is not None else pos.avg_price
        risk = max(Decimal("0"), (ref - pos.stop_loss) * pos.quantity / fx)
    return PositionValuation(pos, q.price if q else None, q.quote_time if q else None, q.source if q else None,
                             fx, fx_est, mv, cost, mv - cost, risk, stale, pos.is_demo)


def value_portfolio(db: Session, p: Portfolio) -> PortfolioValuation:
    vals = [value_position(db, pos, p.base_currency) for pos in open_positions(db, p.id)]
    mv = sum((v.market_value_base for v in vals), Decimal("0"))
    cost = sum((v.cost_base for v in vals), Decimal("0"))
    equity = p.cash + mv
    if equity > p.peak_equity:
        p.peak_equity = equity
    dd = ((p.peak_equity - equity) / p.peak_equity * 100) if p.peak_equity > 0 else Decimal("0")
    agg = sum((v.risk_to_stop_base or Decimal("0") for v in vals), Decimal("0"))
    warnings = []
    if any(v.stale for v in vals):
        warnings.append("Alcune posizioni sono valorizzate con quotazioni mancanti o obsolete.")
    if any(v.risk_to_stop_base is None for v in vals):
        warnings.append("Alcune posizioni non hanno stop loss: il rischio aggregato è sottostimato.")
    if any(v.fx_is_estimate for v in vals):
        warnings.append("Cambio corrente non disponibile per alcune valute: usato il cambio di apertura.")
    if any(v.is_demo for v in vals):
        warnings.append("Il portafoglio contiene posizioni su dati DEMO sintetici.")
    q = lambda x: x.quantize(_M, ROUND_HALF_UP)  # noqa: E731
    return PortfolioValuation(
        portfolio=p, cash=q(p.cash), invested_cost=q(cost), market_value=q(mv), equity=q(equity),
        unrealized_pnl=q(mv - cost), realized_pnl=q(p.realized_pnl),
        cumulative_return_pct=q((equity - p.initial_capital) / p.initial_capital * 100),
        drawdown_pct=q(dd), aggregate_risk_base=q(agg),
        aggregate_risk_pct=q(agg / equity * 100) if equity > 0 else Decimal("0"),
        positions=vals, warnings=warnings,
    )
