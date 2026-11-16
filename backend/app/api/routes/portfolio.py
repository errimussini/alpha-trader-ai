from datetime import UTC, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.adapters.market_data.base import MarketDataAdapter
from app.api.deps import Pagination, current_user, market_data, require_role
from app.core.db import get_db
from app.core.errors import AppError, NotFound, SafetyBlock
from app.core.security import Role
from app.models.entities import Alert, EquitySnapshot, Order, Position, User
from app.schemas.common import (
    AlertOut, EquitySnapshotOut, OrderOut, Page, PaperOrderIn, PaperOrderOut, PortfolioOut, PositionLevelsIn,
    PositionOut,
)
from app.services import audit
from app.services.market_data import get_instrument_or_404
from app.services.monitor import daily_return_pct, reconcile, run_monitor
from app.services.paper_trading import cancel_order, place_paper_order
from app.services.portfolio import (
    PositionValuation, get_or_create_paper_portfolio, risk_param, value_portfolio, value_position,
)

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
        daily_return_pct=daily_return_pct(db, p, v.equity),  # None finché non esiste lo snapshot di un giorno precedente
        drawdown_pct=v.drawdown_pct, aggregate_risk_base=v.aggregate_risk_base,
        aggregate_risk_pct=v.aggregate_risk_pct, risk_params=p.risk_params, open_positions=len(v.positions),
        warnings=v.warnings, valued_at=v.valued_at,
    )


@router.get("/portfolio/equity", response_model=list[EquitySnapshotOut])
def equity_history(days: int = Query(365, ge=1, le=3650), user: User = Depends(current_user),
                   db: Session = Depends(get_db)):
    p = get_or_create_paper_portfolio(db, user)
    db.commit()
    rows = db.execute(select(EquitySnapshot).where(EquitySnapshot.portfolio_id == p.id)
                      .order_by(EquitySnapshot.snap_date.desc()).limit(days)).scalars()
    return list(reversed(list(rows)))


def _pos_out(v: PositionValuation) -> PositionOut:
    pos = v.position
    return PositionOut(
        id=pos.id, instrument_id=pos.instrument_id, ticker=f"{pos.instrument.symbol}.{pos.instrument.exchange}",
        name=pos.instrument.name, sector=pos.instrument.sector, quantity=pos.quantity, avg_price=pos.avg_price,
        currency=pos.currency, last_price=v.price, price_time=v.price_time, price_source=v.price_source,
        market_value_base=v.market_value_base.quantize(Decimal("0.01")),
        unrealized_pnl_base=v.unrealized_pnl_base.quantize(Decimal("0.01")),
        stop_loss=pos.stop_loss, take_profit_1=pos.take_profit_1, risk_to_stop_base=v.risk_to_stop_base,
        stale=v.stale, is_demo=v.is_demo, opened_at=pos.opened_at, take_profit_2=pos.take_profit_2,
        trade_plan_id=pos.trade_plan_id, tp1_done=pos.tp1_done, trailing_active=pos.trailing_active,
        trailing_distance=pos.trailing_distance, high_water=pos.high_water, last_checked_at=pos.last_checked_at,
    )


@router.get("/positions", response_model=Page[PositionOut])
def positions(pg: Pagination = Depends(), user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = get_or_create_paper_portfolio(db, user)
    vals = value_portfolio(db, p).positions
    db.commit()
    page = vals[pg.offset: pg.offset + pg.page_size]
    return Page(items=[_pos_out(v) for v in page], total=len(vals), page=pg.page, page_size=pg.page_size)


@router.patch("/positions/{position_id}/levels", response_model=PositionOut)
def position_levels(position_id: int, body: PositionLevelsIn, user: User = Depends(require_role(Role.ANALYST)),
                    db: Session = Depends(get_db)):
    p = get_or_create_paper_portfolio(db, user)
    pos = db.get(Position, position_id)
    if pos is None or pos.portfolio_id != p.id or pos.status != "OPEN":
        raise NotFound("Posizione aperta non trovata")
    v = value_position(db, pos, p.base_currency)
    ref = v.price if v.price is not None else pos.avg_price
    before = {"stop_loss": pos.stop_loss, "take_profit_1": pos.take_profit_1, "take_profit_2": pos.take_profit_2}
    widened = False
    if body.stop_loss is not None:
        if body.stop_loss >= ref:
            raise AppError(f"Lo stop {body.stop_loss} deve restare sotto il prezzo corrente {ref}.", code="invalid_stop")
        if pos.stop_loss is not None and body.stop_loss < pos.stop_loss:
            # più perdita ammessa: serve una nuova autorizzazione esplicita, motivata e dentro il budget di rischio
            if not body.authorize_wider_stop or not (body.reason or "").strip():
                raise SafetyBlock("Abbassare lo stop aumenta la perdita ammessa: serve authorize_wider_stop=true "
                                  "con una motivazione.", code="stop_widening")
            val = value_portfolio(db, p)
            new_risk = (ref - body.stop_loss) * pos.quantity / v.fx_rate
            budget = val.equity * risk_param(p, "risk_per_trade_pct") / 100
            agg_after = val.aggregate_risk_base - (v.risk_to_stop_base or Decimal("0")) + new_risk
            max_agg = val.equity * risk_param(p, "max_aggregate_risk_pct") / 100
            if new_risk > budget or agg_after > max_agg:
                raise SafetyBlock(f"Nuovo rischio a stop {new_risk.quantize(Decimal('0.01'))} oltre il budget per "
                                  f"operazione ({budget.quantize(Decimal('0.01'))}) o il rischio aggregato massimo: "
                                  f"stop non modificato.", code="stop_widening_over_budget")
            widened = True
        pos.stop_loss = body.stop_loss
    for k in ("take_profit_1", "take_profit_2"):
        val_k = getattr(body, k)
        if val_k is not None:
            if val_k <= ref:
                raise AppError(f"{k} deve essere sopra il prezzo corrente {ref}.", code="invalid_target")
            setattr(pos, k, val_k)
    if pos.take_profit_1 and pos.take_profit_2 and pos.take_profit_2 <= pos.take_profit_1:
        raise AppError("TP2 deve essere superiore a TP1.", code="invalid_target")
    db.flush()
    audit.record(db, actor=user.email, action="position.stop_widened" if widened else "position.levels",
                 entity="position", entity_id=pos.id,
                 payload={"before": before, "after": {"stop_loss": pos.stop_loss, "take_profit_1": pos.take_profit_1,
                                                      "take_profit_2": pos.take_profit_2},
                          "reference_price": ref, "reason": body.reason})
    db.commit()
    return _pos_out(value_position(db, pos, p.base_currency))


def _order_out(o: Order) -> OrderOut:
    out = OrderOut.model_validate(o)
    return out.model_copy(update={"ticker": f"{o.instrument.symbol}.{o.instrument.exchange}"})


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
    return PaperOrderOut(order=_order_out(out.order), duplicate=out.duplicate)


@router.get("/paper/orders", response_model=Page[OrderOut])
def list_orders(pg: Pagination = Depends(), open_only: bool = Query(False), user: User = Depends(current_user),
                db: Session = Depends(get_db)):
    p = get_or_create_paper_portfolio(db, user)
    db.commit()
    stmt = select(Order).where(Order.portfolio_id == p.id)
    if open_only:
        stmt = stmt.where(Order.status.in_(("NEW", "OPEN", "PARTIALLY_FILLED")))
    total = db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    rows = db.execute(stmt.order_by(Order.id.desc()).offset(pg.offset).limit(pg.page_size)).scalars()
    return Page(items=[_order_out(o) for o in rows], total=total, page=pg.page, page_size=pg.page_size)


@router.post("/paper/orders/{order_id}/cancel", response_model=OrderOut)
def cancel(order_id: int, user: User = Depends(require_role(Role.ANALYST)), db: Session = Depends(get_db)):
    p = get_or_create_paper_portfolio(db, user)
    o = db.get(Order, order_id)
    if o is None or o.portfolio_id != p.id:
        raise NotFound("Ordine non trovato")
    cancel_order(db, o, user.email, "annullato dall'utente")
    db.commit()
    return _order_out(o)


@router.post("/paper/monitor")
def monitor_now(user: User = Depends(require_role(Role.ANALYST)), db: Session = Depends(get_db),
                adapter: MarketDataAdapter = Depends(market_data)):
    """Esegue subito un ciclo di monitoraggio sul portafoglio dell'utente (lo stesso del worker)."""
    p = get_or_create_paper_portfolio(db, user)
    rep = run_monitor(db, adapter, p, actor=user.email)
    db.commit()
    return rep.to_dict()


@router.get("/paper/reconciliation")
def reconciliation(user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = get_or_create_paper_portfolio(db, user)
    out = reconcile(db, p)
    db.commit()
    return out


def _alert_scope(db: Session, user: User):
    p = get_or_create_paper_portfolio(db, user)
    return or_(Alert.portfolio_id == p.id, Alert.portfolio_id.is_(None))


@router.get("/alerts", response_model=Page[AlertOut])
def alerts(pg: Pagination = Depends(), active_only: bool = Query(True), kind: str | None = Query(None, max_length=32),
           severity: str | None = Query(None, pattern="^(INFO|WARNING|CRITICAL)$"),
           db: Session = Depends(get_db), user: User = Depends(current_user)):
    stmt = select(Alert).where(_alert_scope(db, user))
    db.commit()
    if active_only:
        stmt = stmt.where(Alert.acknowledged_at.is_(None))
    if kind:
        stmt = stmt.where(Alert.kind == kind)
    if severity:
        stmt = stmt.where(Alert.severity == severity)
    total = db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    rows = db.execute(stmt.order_by(Alert.created_at.desc(), Alert.id.desc()).offset(pg.offset).limit(pg.page_size)).scalars()
    return Page(items=list(rows), total=total, page=pg.page, page_size=pg.page_size)


@router.get("/alerts/summary")
def alerts_summary(db: Session = Depends(get_db), user: User = Depends(current_user)):
    scope = _alert_scope(db, user)
    db.commit()
    rows = db.execute(select(Alert.kind, Alert.severity, func.count()).where(scope, Alert.acknowledged_at.is_(None))
                      .group_by(Alert.kind, Alert.severity)).all()
    by_kind: dict[str, int] = {}
    by_sev: dict[str, int] = {}
    for k, s, n in rows:
        by_kind[k] = by_kind.get(k, 0) + n
        by_sev[s] = by_sev.get(s, 0) + n
    return {"active": sum(by_kind.values()), "by_kind": by_kind, "by_severity": by_sev}


@router.post("/alerts/{alert_id}/ack", response_model=AlertOut)
def ack_alert(alert_id: int, user: User = Depends(require_role(Role.ANALYST)), db: Session = Depends(get_db)):
    p = get_or_create_paper_portfolio(db, user)
    a = db.get(Alert, alert_id)
    if not a or (a.portfolio_id is not None and a.portfolio_id != p.id):
        raise NotFound("Alert non trovato")
    a.acknowledged_at = datetime.now(UTC)
    audit.record(db, actor=user.email, action="alert.ack", entity="alert", entity_id=a.id)
    db.commit()
    return a
