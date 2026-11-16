from datetime import UTC, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.adapters.market_data.base import MarketDataAdapter
from app.api.deps import Pagination, current_user, market_data, require_role
from app.core.db import get_db
from app.core.errors import NotFound
from app.core.security import Role
from app.models.entities import Alert, Order, User
from app.schemas.common import AlertOut, OrderOut, Page, PaperOrderIn, PaperOrderOut, PortfolioOut, PositionOut
from app.services import audit
from app.services.market_data import get_instrument_or_404
from app.services.paper_trading import place_paper_order
from app.services.portfolio import get_or_create_paper_portfolio, value_portfolio

router = APIRouter(prefix="/api", tags=["portfolio"])


@router.get("/portfolio", response_model=PortfolioOut)
def portfolio(user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = get_or_create_paper_portfolio(db, user)
    v = value_portfolio(db, p)
    db.commit()
    return PortfolioOut(
        id=p.id, name=p.name, mode=p.mode, base_currency=p.base_currency, initial_capital=p.initial_capital,
        cash=v.cash, invested_cost=v.invested_cost, market_value=v.market_value, equity=v.equity,
        unrealized_pnl=v.unrealized_pnl, realized_pnl=v.realized_pnl, cumulative_return_pct=v.cumulative_return_pct,
        daily_return_pct=None,  # richiede snapshot giornalieri dell'equity (Fase 6)
        drawdown_pct=v.drawdown_pct, aggregate_risk_base=v.aggregate_risk_base,
        aggregate_risk_pct=v.aggregate_risk_pct, risk_params=p.risk_params, open_positions=len(v.positions),
        warnings=v.warnings, valued_at=v.valued_at,
    )


@router.get("/positions", response_model=Page[PositionOut])
def positions(pg: Pagination = Depends(), user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = get_or_create_paper_portfolio(db, user)
    vals = value_portfolio(db, p).positions
    db.commit()
    page = vals[pg.offset: pg.offset + pg.page_size]
    items = [PositionOut(
        id=v.position.id, instrument_id=v.position.instrument_id,
        ticker=f"{v.position.instrument.symbol}.{v.position.instrument.exchange}", name=v.position.instrument.name,
        sector=v.position.instrument.sector, quantity=v.position.quantity, avg_price=v.position.avg_price,
        currency=v.position.currency, last_price=v.price, price_time=v.price_time, price_source=v.price_source,
        market_value_base=v.market_value_base.quantize(Decimal("0.01")),
        unrealized_pnl_base=v.unrealized_pnl_base.quantize(Decimal("0.01")),
        stop_loss=v.position.stop_loss, take_profit_1=v.position.take_profit_1,
        risk_to_stop_base=v.risk_to_stop_base, stale=v.stale, is_demo=v.is_demo, opened_at=v.position.opened_at,
    ) for v in page]
    return Page(items=items, total=len(vals), page=pg.page, page_size=pg.page_size)


@router.post("/paper/orders", response_model=PaperOrderOut)
def paper_order(body: PaperOrderIn, user: User = Depends(require_role(Role.ANALYST)), db: Session = Depends(get_db),
                adapter: MarketDataAdapter = Depends(market_data)):
    p = get_or_create_paper_portfolio(db, user)
    inst = get_instrument_or_404(db, body.instrument_id)
    out = place_paper_order(db, adapter=adapter, user=user, portfolio=p, instrument=inst, side=body.side,
                            order_type=body.order_type, quantity=body.quantity, client_order_id=body.client_order_id,
                            limit_price=body.limit_price, stop_loss=body.stop_loss,
                            take_profit_1=body.take_profit_1, trade_plan_id=body.trade_plan_id)
    db.commit()
    return PaperOrderOut(order=OrderOut.model_validate(out.order), duplicate=out.duplicate)


@router.get("/paper/orders", response_model=Page[OrderOut])
def list_orders(pg: Pagination = Depends(), user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = get_or_create_paper_portfolio(db, user)
    db.commit()
    total = db.execute(select(func.count(Order.id)).where(Order.portfolio_id == p.id)).scalar_one()
    rows = db.execute(select(Order).where(Order.portfolio_id == p.id).order_by(Order.id.desc())
                      .offset(pg.offset).limit(pg.page_size)).scalars()
    return Page(items=list(rows), total=total, page=pg.page, page_size=pg.page_size)


@router.get("/alerts", response_model=Page[AlertOut])
def alerts(pg: Pagination = Depends(), active_only: bool = Query(True), db: Session = Depends(get_db),
           _: User = Depends(current_user)):
    stmt = select(Alert)
    if active_only:
        stmt = stmt.where(Alert.acknowledged_at.is_(None))
    total = db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    rows = db.execute(stmt.order_by(Alert.created_at.desc()).offset(pg.offset).limit(pg.page_size)).scalars()
    return Page(items=list(rows), total=total, page=pg.page, page_size=pg.page_size)


@router.post("/alerts/{alert_id}/ack", response_model=AlertOut)
def ack_alert(alert_id: int, user: User = Depends(require_role(Role.ANALYST)), db: Session = Depends(get_db)):
    a = db.get(Alert, alert_id)
    if not a:
        raise NotFound("Alert non trovato")
    a.acknowledged_at = datetime.now(UTC)
    audit.record(db, actor=user.email, action="alert.ack", entity="alert", entity_id=a.id)
    db.commit()
    return a
