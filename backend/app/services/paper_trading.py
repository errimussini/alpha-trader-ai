"""Esecuzione di ordini simulati sul PaperBroker con tutti i controlli di sicurezza.

Fase 6: gli ordini LIMIT non eseguibili restano OPEN e vengono rivalutati dal monitoraggio
(`services/monitor.py`); un ordine può essere eseguito in più tranche (PARTIALLY_FILLED) quando è
attivo il limite di partecipazione al volume; il residuo di un ordine si può annullare.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.adapters.broker.base import BrokerOrderResult, Fill, MarketSnapshot, OrderRequest
from app.adapters.broker.paper import PaperBroker
from app.adapters.market_data.base import MarketDataAdapter
from app.core.config import get_settings
from app.core.errors import AppError, Conflict, SafetyBlock
from app.models.entities import (
    CashMovement, Execution, Instrument, MarketQuote, Order, Portfolio, Position, TradePlan, User,
)
from app.services import audit, safety
from app.services.market_data import fetch_fx, fetch_quote
from app.services.portfolio import risk_param, value_portfolio

_M = Decimal("0.0001")
OPEN_STATUSES = ("NEW", "OPEN", "PARTIALLY_FILLED")


@dataclass
class OrderOutcome:
    order: Order
    duplicate: bool


def build_paper_broker() -> PaperBroker:
    s = get_settings()
    return PaperBroker(s.commission_per_order, s.commission_pct, s.slippage_bps, s.paper_max_participation_pct)


def _todays_realized(db: Session, portfolio_id: int) -> Decimal:
    start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    v = db.execute(
        select(func.coalesce(func.sum(Execution.realized_pnl_base), 0)).join(Order, Execution.order_id == Order.id)
        .where(Order.portfolio_id == portfolio_id, Execution.executed_at >= start)
    ).scalar_one()
    return Decimal(str(v))


def open_position(db: Session, portfolio_id: int, instrument_id: int) -> Position | None:
    return db.execute(select(Position).where(Position.portfolio_id == portfolio_id,
                                             Position.instrument_id == instrument_id,
                                             Position.status == "OPEN")).scalar_one_or_none()


_open_position = open_position  # compatibilità


def assert_buy_limits(db: Session, portfolio: Portfolio) -> None:
    """Limiti di perdita giornaliera e di drawdown: bloccano nuove esposizioni (anche gli ordini già aperti)."""
    val = value_portfolio(db, portfolio)
    max_dd = risk_param(portfolio, "max_total_drawdown_pct")
    if val.drawdown_pct >= max_dd:
        raise SafetyBlock(f"Drawdown {val.drawdown_pct}% oltre il limite {max_dd}%: nuovi acquisti bloccati.",
                          code="drawdown_limit")
    daily = _todays_realized(db, portfolio.id)
    max_daily = val.equity * risk_param(portfolio, "max_daily_loss_pct") / 100
    if daily < 0 and -daily >= max_daily:
        raise SafetyBlock(f"Perdita realizzata giornaliera {(-daily).quantize(_M)} oltre il limite: acquisti bloccati.",
                          code="daily_loss_limit")


def snapshot(q: MarketQuote) -> MarketSnapshot:
    return MarketSnapshot(q.price, q.bid, q.ask, q.quote_time, q.source, q.volume)


def _apply_fill(db: Session, order: Order, portfolio: Portfolio, instrument: Instrument, fill: Fill,
                fx: Decimal, is_demo: bool) -> str | None:
    """Registra un eseguito su cassa, posizione e movimenti. Restituisce un motivo di rifiuto oppure None."""
    gross_base = fill.price * fill.quantity / fx
    fee_base = fill.fee / fx
    if order.side == "BUY":
        total = gross_base + fee_base
        if total > portfolio.cash:
            return "liquidità insufficiente"
        pos = open_position(db, portfolio.id, instrument.id)
        if pos is None:
            pos = Position(portfolio_id=portfolio.id, instrument_id=instrument.id, trade_plan_id=order.trade_plan_id,
                           quantity=Decimal("0"), avg_price=Decimal("0"), currency=instrument.currency,
                           fx_rate_at_open=fx, is_demo=is_demo, status="OPEN", tp1_done=False, trailing_active=False)
            db.add(pos)
        new_qty = pos.quantity + fill.quantity
        # prezzo medio in valuta strumento, commissioni incluse nel costo
        cost_ccy = pos.avg_price * pos.quantity + fill.price * fill.quantity + fill.fee
        pos.avg_price = (cost_ccy / new_qty).quantize(Decimal("0.00000001"))
        pos.fx_rate_at_open = ((pos.fx_rate_at_open * pos.quantity + fx * fill.quantity) / new_qty) if pos.quantity else fx
        pos.quantity = new_qty
        pos.is_demo = pos.is_demo or is_demo
        pos.high_water = max(pos.high_water or fill.price, fill.price)
        if order.stop_loss is not None and (pos.stop_loss is None or order.stop_loss > pos.stop_loss):
            pos.stop_loss = order.stop_loss
        if order.take_profit_1 is not None:
            pos.take_profit_1 = order.take_profit_1
        if order.trade_plan_id is not None:
            tp = db.get(TradePlan, order.trade_plan_id)
            pos.trade_plan_id = order.trade_plan_id
            if tp is not None:
                pos.take_profit_2 = tp.take_profit_2
                atr = (tp.stop_rationale or {}).get("atr")
                if atr:
                    pos.trailing_distance = (Decimal(str(atr)) * Decimal("2.5")).quantize(Decimal("0.00000001"))
        portfolio.cash -= total
        db.add(CashMovement(portfolio_id=portfolio.id, kind="TRADE", amount_base=-gross_base.quantize(_M),
                            currency=portfolio.base_currency, reference=f"order:{order.id}"))
        db.add(CashMovement(portfolio_id=portfolio.id, kind="FEE", amount_base=-fee_base.quantize(_M),
                            currency=portfolio.base_currency, reference=f"order:{order.id}"))
        db.add(Execution(order_id=order.id, quantity=fill.quantity, price=fill.price, fee=fill.fee,
                         slippage=fill.slippage, fx_rate=fx, executed_at=fill.executed_at, venue="PAPER"))
        db.flush()
        return None

    pos = open_position(db, portfolio.id, instrument.id)
    if pos is None or pos.quantity < fill.quantity:
        return "posizione insufficiente per la vendita"
    proceeds_base = gross_base - fee_base
    cost_base = fill.quantity * pos.avg_price / pos.fx_rate_at_open
    realized = proceeds_base - cost_base
    pos.quantity -= fill.quantity
    pos.realized_pnl_base += realized
    if pos.quantity == 0:
        pos.status, pos.closed_at = "CLOSED", datetime.now(UTC)
    portfolio.cash += proceeds_base
    portfolio.realized_pnl += realized
    db.add(CashMovement(portfolio_id=portfolio.id, kind="TRADE", amount_base=gross_base.quantize(_M),
                        currency=portfolio.base_currency, reference=f"order:{order.id}"))
    db.add(CashMovement(portfolio_id=portfolio.id, kind="FEE", amount_base=-fee_base.quantize(_M),
                        currency=portfolio.base_currency, reference=f"order:{order.id}"))
    db.add(Execution(order_id=order.id, quantity=fill.quantity, price=fill.price, fee=fill.fee,
                     slippage=fill.slippage, fx_rate=fx, executed_at=fill.executed_at, venue="PAPER",
                     realized_pnl_base=realized.quantize(_M, ROUND_HALF_UP)))
    db.flush()
    return None


def submit_to_broker(db: Session, order: Order, portfolio: Portfolio, instrument: Instrument, quote: MarketQuote,
                     fx: Decimal, is_demo: bool, broker: PaperBroker | None = None) -> BrokerOrderResult:
    """Invia al broker il residuo dell'ordine con la quotazione verificata e applica gli eseguiti."""
    broker = broker or build_paper_broker()
    remaining = order.quantity - order.filled_quantity
    res = broker.submit_order(
        OrderRequest(order.client_order_id, instrument.symbol, instrument.exchange, order.side, order.order_type,
                     remaining, order.limit_price),
        snapshot(quote),
    )
    order.last_checked_at = datetime.now(UTC)
    applied: list[Fill] = []
    for f in res.fills:
        reason = _apply_fill(db, order, portfolio, instrument, f, fx, is_demo)
        if reason:
            res.reject_reason = reason
            break
        applied.append(f)
    res.fills = applied
    order.filled_quantity += sum((f.quantity for f in applied), Decimal("0"))
    if order.filled_quantity >= order.quantity:
        order.status = "FILLED"
    elif res.reject_reason and res.status != "REJECTED":
        # eseguito non registrabile (es. liquidità): il residuo non resta in coda
        order.status = "CANCELLED" if order.filled_quantity > 0 else "REJECTED"
    elif res.status == "REJECTED":
        order.status = "CANCELLED" if order.filled_quantity > 0 else "REJECTED"
    elif order.filled_quantity > 0:
        order.status = "PARTIALLY_FILLED"
    else:
        order.status = "OPEN"
    order.reject_reason = res.reject_reason
    db.flush()
    return res


def place_paper_order(db: Session, *, adapter: MarketDataAdapter, user: User, portfolio: Portfolio,
                      instrument: Instrument, side: str, order_type: str, quantity: Decimal,
                      client_order_id: str, limit_price: Decimal | None = None,
                      stop_loss: Decimal | None = None, take_profit_1: Decimal | None = None,
                      trade_plan_id: int | None = None, purpose: str = "MANUAL",
                      actor: str | None = None) -> OrderOutcome:
    s = get_settings()
    actor = actor or user.email
    # 1) idempotenza: lo stesso client_order_id non genera mai un secondo ordine
    existing = db.execute(select(Order).where(Order.client_order_id == client_order_id)).scalar_one_or_none()
    if existing:
        if existing.portfolio_id != portfolio.id or existing.instrument_id != instrument.id or existing.side != side:
            raise Conflict("client_order_id già usato per un ordine diverso")
        return OrderOutcome(existing, duplicate=True)

    # 2) modalità + kill switch
    safety.assert_can_trade_paper(db)
    plan = None
    if trade_plan_id is not None and side == "BUY":
        plan = db.get(TradePlan, trade_plan_id)
        if plan is None or plan.portfolio_id != portfolio.id or plan.instrument_id != instrument.id:
            raise AppError("Piano operativo non valido per questo portafoglio/strumento.", code="invalid_plan")
        if plan.status != "READY":
            raise SafetyBlock(f"Il piano #{plan.id} è {plan.status}: solo i piani READY sono eseguibili. "
                              f"{plan.status_reason or ''}".strip(), code="plan_not_ready")
        if plan.valid_until < datetime.now(UTC):
            raise SafetyBlock("Validità del piano scaduta.", code="plan_expired")
        if quantity > plan.quantity:
            raise SafetyBlock(f"Quantità {quantity} superiore a quella approvata dal Risk Manager ({plan.quantity}).",
                              code="qty_above_plan")
        stop_loss = stop_loss if stop_loss is not None else plan.stop_loss
        if stop_loss < plan.stop_loss:
            raise SafetyBlock("Stop più basso di quello del piano: serve una nuova autorizzazione.", code="stop_widening")
        take_profit_1 = take_profit_1 if take_profit_1 is not None else plan.take_profit_1
    if side == "SELL" and not s.short_selling_enabled:
        pos = open_position(db, portfolio.id, instrument.id)
        pending = db.execute(select(func.coalesce(func.sum(Order.quantity - Order.filled_quantity), 0)).where(
            Order.portfolio_id == portfolio.id, Order.instrument_id == instrument.id, Order.side == "SELL",
            Order.status.in_(OPEN_STATUSES))).scalar_one()
        if pos is None or pos.quantity - Decimal(str(pending)) < quantity:
            raise SafetyBlock("Vendita allo scoperto disabilitata: quantità superiore alla posizione aperta "
                              "(al netto degli ordini di vendita già aperti).", code="short_disabled")
    if side == "BUY" and stop_loss is not None:
        cur = open_position(db, portfolio.id, instrument.id)
        if cur is not None and cur.stop_loss is not None and stop_loss < cur.stop_loss:
            raise SafetyBlock("Abbassare lo stop aumenta la perdita ammessa: serve una nuova autorizzazione.",
                              code="stop_widening")
    if instrument.instrument_type == "FX":
        raise AppError("Gli strumenti FX non sono negoziabili in questa release.")

    # 3) dati verificati e aggiornati
    quote = fetch_quote(db, adapter, instrument)
    safety.assert_fresh(quote.quote_time, what=f"{instrument.symbol}.{instrument.exchange}")
    is_demo = quote.is_demo or instrument.is_demo
    if is_demo and not s.allow_demo_paper_trading:
        raise SafetyBlock("Ordini su dati DEMO disabilitati.", code="demo_data")
    if side == "BUY" and stop_loss is not None and stop_loss >= quote.price:
        raise AppError(f"Stop loss {stop_loss} non inferiore al prezzo corrente {quote.price}: setup non valido per un long.",
                       code="invalid_stop")
    if side == "BUY" and take_profit_1 is not None and take_profit_1 <= quote.price:
        raise AppError(f"Take profit {take_profit_1} non superiore al prezzo corrente {quote.price}.", code="invalid_target")
    if order_type == "LIMIT" and side == "BUY" and stop_loss is not None and limit_price is not None \
            and limit_price <= stop_loss:
        raise AppError("Prezzo limite non superiore allo stop.", code="invalid_limit")
    fxq = fetch_fx(db, adapter, portfolio.base_currency, instrument.currency)
    if fxq is not None:
        safety.assert_fresh(fxq.quote_time, what=f"cambio {portfolio.base_currency}/{instrument.currency}")
        is_demo = is_demo or fxq.is_demo
    fx = fxq.price if fxq else Decimal("1")

    # 4) limiti di perdita giornaliera e complessiva (bloccano nuove esposizioni)
    if side == "BUY":
        assert_buy_limits(db, portfolio)

    valid_until = None
    if order_type == "LIMIT":
        valid_until = plan.valid_until if plan else datetime.now(UTC) + timedelta(days=s.paper_limit_order_days)
    broker = build_paper_broker()
    order = Order(portfolio_id=portfolio.id, instrument_id=instrument.id, trade_plan_id=trade_plan_id,
                  client_order_id=client_order_id, broker=broker.name, mode="PAPER", side=side,
                  order_type=order_type, quantity=quantity, filled_quantity=Decimal("0"), limit_price=limit_price,
                  status="NEW", reference_quote_id=quote.id, created_by=actor, is_demo=is_demo,
                  stop_loss=stop_loss if side == "BUY" else None, take_profit_1=take_profit_1 if side == "BUY" else None,
                  valid_until=valid_until, purpose=("ENTRY" if plan else purpose))
    db.add(order)
    db.flush()
    res = submit_to_broker(db, order, portfolio, instrument, quote, fx, is_demo, broker)
    audit.record(db, actor=actor, action="paper.order", entity="order", entity_id=order.id, payload={
        "client_order_id": client_order_id, "instrument": f"{instrument.symbol}.{instrument.exchange}",
        "side": side, "type": order_type, "quantity": quantity, "limit": limit_price, "status": order.status,
        "purpose": order.purpose, "reject_reason": order.reject_reason, "quote_id": quote.id,
        "quote_source": quote.source, "quote_time": quote.quote_time, "fx": fx, "is_demo": is_demo,
        "valid_until": valid_until, "fills": [{"qty": f.quantity, "price": f.price, "fee": f.fee} for f in res.fills],
    })
    return OrderOutcome(order, duplicate=False)


def cancel_order(db: Session, order: Order, actor: str, reason: str) -> Order:
    if order.status not in OPEN_STATUSES:
        raise Conflict(f"L'ordine #{order.id} è {order.status}: non annullabile.")
    order.status = "CANCELLED"
    order.reject_reason = reason
    db.flush()
    audit.record(db, actor=actor, action="paper.order_cancelled", entity="order", entity_id=order.id,
                 payload={"reason": reason, "filled_quantity": order.filled_quantity, "quantity": order.quantity})
    return order
