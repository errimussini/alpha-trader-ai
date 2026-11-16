"""Esecuzione di ordini simulati sul PaperBroker con tutti i controlli di sicurezza."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.adapters.broker.base import MarketSnapshot, OrderRequest
from app.adapters.broker.paper import PaperBroker
from app.adapters.market_data.base import MarketDataAdapter
from app.core.config import get_settings
from app.core.errors import AppError, Conflict, SafetyBlock
from app.models.entities import CashMovement, Execution, Instrument, Order, Portfolio, Position, User
from app.services import audit, safety
from app.services.market_data import fetch_fx, fetch_quote
from app.services.portfolio import risk_param, value_portfolio

_M = Decimal("0.0001")


@dataclass
class OrderOutcome:
    order: Order
    duplicate: bool


def build_paper_broker() -> PaperBroker:
    s = get_settings()
    return PaperBroker(s.commission_per_order, s.commission_pct, s.slippage_bps)


def _todays_realized(db: Session, portfolio_id: int) -> Decimal:
    start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    v = db.execute(
        select(func.coalesce(func.sum(Execution.realized_pnl_base), 0)).join(Order, Execution.order_id == Order.id)
        .where(Order.portfolio_id == portfolio_id, Execution.executed_at >= start)
    ).scalar_one()
    return Decimal(str(v))


def _open_position(db: Session, portfolio_id: int, instrument_id: int) -> Position | None:
    return db.execute(select(Position).where(Position.portfolio_id == portfolio_id,
                                             Position.instrument_id == instrument_id,
                                             Position.status == "OPEN")).scalar_one_or_none()


def place_paper_order(db: Session, *, adapter: MarketDataAdapter, user: User, portfolio: Portfolio,
                      instrument: Instrument, side: str, order_type: str, quantity: Decimal,
                      client_order_id: str, limit_price: Decimal | None = None,
                      stop_loss: Decimal | None = None, take_profit_1: Decimal | None = None,
                      trade_plan_id: int | None = None) -> OrderOutcome:
    s = get_settings()
    # 1) idempotenza: lo stesso client_order_id non genera mai un secondo ordine
    existing = db.execute(select(Order).where(Order.client_order_id == client_order_id)).scalar_one_or_none()
    if existing:
        if existing.portfolio_id != portfolio.id or existing.instrument_id != instrument.id or existing.side != side:
            raise Conflict("client_order_id già usato per un ordine diverso")
        return OrderOutcome(existing, duplicate=True)

    # 2) modalità + kill switch
    safety.assert_can_trade_paper(db)
    if side == "SELL" and not s.short_selling_enabled:
        pos = _open_position(db, portfolio.id, instrument.id)
        if pos is None or pos.quantity < quantity:
            raise SafetyBlock("Vendita allo scoperto disabilitata: quantità superiore alla posizione aperta.",
                              code="short_disabled")
    if side == "BUY" and stop_loss is not None:
        cur = _open_position(db, portfolio.id, instrument.id)
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
    fxq = fetch_fx(db, adapter, portfolio.base_currency, instrument.currency)
    if fxq is not None:
        safety.assert_fresh(fxq.quote_time, what=f"cambio {portfolio.base_currency}/{instrument.currency}")
        is_demo = is_demo or fxq.is_demo
    fx = fxq.price if fxq else Decimal("1")

    # 4) limiti di perdita giornaliera e complessiva (bloccano nuove esposizioni)
    if side == "BUY":
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

    broker = build_paper_broker()
    order = Order(portfolio_id=portfolio.id, instrument_id=instrument.id, trade_plan_id=trade_plan_id,
                  client_order_id=client_order_id, broker=broker.name, mode="PAPER", side=side,
                  order_type=order_type, quantity=quantity, limit_price=limit_price, status="NEW",
                  reference_quote_id=quote.id, created_by=user.email, is_demo=is_demo)
    db.add(order)
    db.flush()

    res = broker.submit_order(
        OrderRequest(client_order_id, instrument.symbol, instrument.exchange, side, order_type, quantity, limit_price),
        MarketSnapshot(quote.price, quote.bid, quote.ask, quote.quote_time, quote.source),
    )

    if res.status == "FILLED":
        fill = res.fills[0]
        gross_base = fill.price * fill.quantity / fx
        fee_base = fill.fee / fx
        if side == "BUY":
            total = gross_base + fee_base
            if total > portfolio.cash:
                res.status, res.reject_reason, res.fills = "REJECTED", "liquidità insufficiente", []
            else:
                pos = _open_position(db, portfolio.id, instrument.id)
                if pos is None:
                    pos = Position(portfolio_id=portfolio.id, instrument_id=instrument.id, trade_plan_id=trade_plan_id,
                                   quantity=Decimal("0"), avg_price=Decimal("0"), currency=instrument.currency,
                                   fx_rate_at_open=fx, is_demo=is_demo, status="OPEN")
                    db.add(pos)
                new_qty = pos.quantity + fill.quantity
                # prezzo medio in valuta strumento, commissioni incluse nel costo
                cost_ccy = pos.avg_price * pos.quantity + fill.price * fill.quantity + fill.fee
                pos.avg_price = (cost_ccy / new_qty).quantize(Decimal("0.00000001"))
                pos.fx_rate_at_open = ((pos.fx_rate_at_open * pos.quantity + fx * fill.quantity) / new_qty) if pos.quantity else fx
                pos.quantity = new_qty
                pos.is_demo = pos.is_demo or is_demo
                if stop_loss is not None:
                    pos.stop_loss = stop_loss
                if take_profit_1 is not None:
                    pos.take_profit_1 = take_profit_1
                portfolio.cash -= total
                db.add(CashMovement(portfolio_id=portfolio.id, kind="TRADE", amount_base=-gross_base.quantize(_M),
                                    currency=portfolio.base_currency, reference=f"order:{order.id}"))
                db.add(CashMovement(portfolio_id=portfolio.id, kind="FEE", amount_base=-fee_base.quantize(_M),
                                    currency=portfolio.base_currency, reference=f"order:{order.id}"))
                db.add(Execution(order_id=order.id, quantity=fill.quantity, price=fill.price, fee=fill.fee,
                                 slippage=fill.slippage, fx_rate=fx, executed_at=fill.executed_at, venue="PAPER"))
        else:
            pos = _open_position(db, portfolio.id, instrument.id)
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

    order.status = res.status
    order.reject_reason = res.reject_reason
    order.filled_quantity = sum((f.quantity for f in res.fills), Decimal("0"))
    db.flush()
    audit.record(db, actor=user.email, action="paper.order", entity="order", entity_id=order.id, payload={
        "client_order_id": client_order_id, "instrument": f"{instrument.symbol}.{instrument.exchange}",
        "side": side, "type": order_type, "quantity": quantity, "limit": limit_price, "status": order.status,
        "reject_reason": order.reject_reason, "quote_id": quote.id, "quote_source": quote.source,
        "quote_time": quote.quote_time, "fx": fx, "is_demo": is_demo,
        "fills": [{"qty": f.quantity, "price": f.price, "fee": f.fee} for f in res.fills],
    })
    return OrderOutcome(order, duplicate=False)
