"""Opportunità, scansioni, piani operativi, controllo rischio, backtest.

In Fase 1 le opportunità e i backtest sono letti dal database (vuoti finché i motori delle fasi
successive non li producono): nessun segnale viene simulato o inventato.
"""
from datetime import UTC, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.adapters.market_data.base import MarketDataAdapter
from app.api.deps import Pagination, current_user, market_data, require_role
from app.core.config import get_settings
from app.core.db import get_db
from app.core.errors import AppError, NotFound
from app.core.security import Role
from app.models.entities import Instrument, ModelEvaluation, RiskAssessment, ScanRun, TradePlan, User
from app.services.planner import generate_for_scan, generate_plan, get_signal
from app.services.scanner import latest_done_scan
from app.schemas.common import Page, RiskCheckIn, RiskCheckOut, TradePlanCreate, TradePlanOut
from app.services import audit
from app.services.market_data import fetch_fx, get_instrument_or_404, latest_fx
from app.services.portfolio import get_or_create_paper_portfolio, risk_param, value_portfolio
from app.services.risk import Costs, PortfolioContext, RiskLimits, SizingRequest, compute_position_size

router = APIRouter(prefix="/api", tags=["trading"])


def plan_out(p: TradePlan) -> TradePlanOut:
    o = TradePlanOut.model_validate(p)
    i = p.instrument
    return o.model_copy(update={"ticker": f"{i.symbol}.{i.exchange}", "name": i.name, "isin": i.isin, "sector": i.sector})


@router.get("/opportunities", response_model=Page[TradePlanOut])
def opportunities(pg: Pagination = Depends(), status: str | None = Query(None, pattern="^(READY|WAIT|REJECTED|EXPIRED)$"),
                  active: bool = Query(True, description="solo READY e WAIT"),
                  sector: str | None = None, exchange: str | None = None, risk_level: str | None = None,
                  style: str | None = None, user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = get_or_create_paper_portfolio(db, user)
    db.commit()
    stmt = select(TradePlan).join(Instrument, TradePlan.instrument_id == Instrument.id).where(TradePlan.portfolio_id == p.id)
    if status:
        stmt = stmt.where(TradePlan.status == status)
    elif active:
        stmt = stmt.where(TradePlan.status.in_(("READY", "WAIT")))
    for col, val in ((Instrument.sector, sector), (Instrument.exchange, exchange.upper() if exchange else None),
                     (TradePlan.risk_level, risk_level), (TradePlan.strategy_style, style)):
        if val:
            stmt = stmt.where(col == val)
    total = db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    order = case((TradePlan.status == "READY", 0), (TradePlan.status == "WAIT", 1), else_=2)
    rows = db.execute(stmt.order_by(order, TradePlan.score.desc().nulls_last(), TradePlan.created_at.desc())
                      .offset(pg.offset).limit(pg.page_size)).scalars()
    return Page(items=[plan_out(r) for r in rows], total=total, page=pg.page, page_size=pg.page_size)


@router.get("/opportunities/{plan_id}", response_model=TradePlanOut)
def opportunity(plan_id: int, db: Session = Depends(get_db), _: User = Depends(current_user)):
    p = db.get(TradePlan, plan_id)
    if not p:
        raise NotFound("Opportunità non trovata")
    return plan_out(p)


@router.post("/trade-plans", response_model=list[TradePlanOut])
def trade_plans(body: TradePlanCreate, user: User = Depends(require_role(Role.ANALYST)), db: Session = Depends(get_db),
                adapter: MarketDataAdapter = Depends(market_data)):
    p = get_or_create_paper_portfolio(db, user)
    if body.signal_id:
        plans = [generate_plan(db, adapter, get_signal(db, body.signal_id), p, user.email)]
    else:
        scan = db.get(ScanRun, body.scan_id) if body.scan_id else latest_done_scan(db)
        if scan is None or scan.status != "DONE":
            raise AppError("Nessuna scansione completata da cui generare i piani.", code="no_scan")
        plans = generate_for_scan(db, adapter, scan, p, user.email, body.top_n)
    db.commit()
    return [plan_out(x) for x in plans]


@router.post("/risk/check", response_model=RiskCheckOut)
def risk_check(body: RiskCheckIn, user: User = Depends(current_user), db: Session = Depends(get_db),
               adapter: MarketDataAdapter = Depends(market_data)):
    s = get_settings()
    p = get_or_create_paper_portfolio(db, user)
    val = value_portfolio(db, p)
    currency, sector, inst = (body.currency or p.base_currency).upper(), body.sector, None
    if body.instrument_id is not None:
        inst = get_instrument_or_404(db, body.instrument_id)
        currency, sector = inst.currency, sector or inst.sector

    fx_source = "n/a"
    if currency == p.base_currency:
        fx = Decimal("1")
    elif body.fx_rate is not None:
        fx, fx_source = body.fx_rate, "fornito dall'utente"
    else:
        fxq = latest_fx(db, p.base_currency, currency) or fetch_fx(db, adapter, p.base_currency, currency)
        fx, fx_source = fxq.price, f"{fxq.source} @ {fxq.quote_time.isoformat()}"

    same_inst = sum((v.market_value_base for v in val.positions if inst and v.position.instrument_id == inst.id), Decimal("0"))
    same_sec = sum((v.market_value_base for v in val.positions
                    if sector and v.position.instrument and v.position.instrument.sector == sector), Decimal("0"))
    cash = min(val.cash, body.available_cash_override) if body.available_cash_override else val.cash
    ctx = PortfolioContext(equity_base=val.equity, cash_base=cash, open_risk_base=val.aggregate_risk_base,
                           exposure_same_instrument_base=same_inst, exposure_same_sector_base=same_sec)
    limits = RiskLimits(*(risk_param(p, k) for k in (
        "risk_per_trade_pct", "max_aggregate_risk_pct", "max_position_exposure_pct",
        "max_sector_exposure_pct", "min_cash_reserve_pct")))
    costs = Costs(s.commission_per_order, s.commission_pct, s.slippage_bps)
    try:
        res = compute_position_size(SizingRequest(
            entry_price=body.entry_price, stop_price=body.stop_price, fx_rate=fx, lot_size=body.lot_size,
            take_profit_1=body.take_profit_1, take_profit_2=body.take_profit_2,
            tp1_close_fraction=body.tp1_close_fraction, gap_stress_pct=body.gap_stress_pct), ctx, limits, costs)
    except ValueError as e:
        raise AppError(str(e), code="invalid_plan") from e

    inputs = body.model_dump(mode="json") | {
        "fx_rate_used": str(fx), "fx_source": fx_source, "currency": currency, "sector": sector,
        "portfolio_id": p.id, "equity_base": str(val.equity), "cash_base": str(cash),
        "open_risk_base": str(val.aggregate_risk_base), "limits": {k: str(v) for k, v in limits.__dict__.items()},
        "costs": {k: str(v) for k, v in costs.__dict__.items()}, "computed_at": datetime.now(UTC).isoformat(),
    }
    ra = RiskAssessment(portfolio_id=p.id, approved=res.approved, inputs=inputs, result=res.to_dict(),
                        blocking_reasons=res.blocking_reasons, engine_version=res.engine_version)
    db.add(ra)
    db.flush()
    audit.record(db, actor=user.email, action="risk.check", entity="risk_assessment", entity_id=ra.id,
                 payload={"approved": res.approved, "quantity": str(res.quantity), "binding": res.binding_constraint})
    db.commit()
    return RiskCheckOut(assessment_id=ra.id, approved=res.approved, result=res.to_dict(), inputs=inputs)


@router.get("/evaluations")
def evaluations(pg: Pagination = Depends(), db: Session = Depends(get_db), _: User = Depends(current_user)):
    """Valutazioni dei modelli (backtest, walk-forward, prospettiche)."""
    total = db.execute(select(func.count(ModelEvaluation.id))).scalar_one()
    rows = db.execute(select(ModelEvaluation).order_by(ModelEvaluation.created_at.desc())
                      .offset(pg.offset).limit(pg.page_size)).scalars()
    items = [{"id": r.id, "strategy_version_id": r.strategy_version_id, "type": r.evaluation_type,
              "period_start": r.period_start, "period_end": r.period_end, "out_of_sample": r.is_out_of_sample,
              "metrics": r.metrics, "created_at": r.created_at} for r in rows]
    return {"items": items, "total": total, "page": pg.page, "page_size": pg.page_size}
