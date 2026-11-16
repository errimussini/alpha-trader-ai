"""Live assistito (Fase 8). Spento di default: senza configurazione esplicita ogni chiamata operativa è bloccata."""
from decimal import Decimal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.adapters.market_data.base import MarketDataAdapter
from app.api.deps import Pagination, current_user, market_data, require_role
from app.core.db import get_db
from app.core.errors import AppError
from app.core.security import Role
from app.models.entities import LiveOrderTicket, User
from app.services import live

router = APIRouter(prefix="/api/live", tags=["live"])


class TicketIn(BaseModel):
    trade_plan_id: int
    quantity: Decimal | None = Field(default=None, gt=0, description="tetto facoltativo; mai oltre la size del Risk Engine")


class ConfirmIn(BaseModel):
    confirmation_code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")
    acknowledge: bool = False


def _out(t: LiveOrderTicket) -> dict:
    return {"id": t.id, "trade_plan_id": t.trade_plan_id, "instrument_id": t.instrument_id,
            "ticker": f"{t.instrument.symbol}.{t.instrument.exchange}", "client_order_id": t.client_order_id,
            "broker": t.broker, "side": t.side, "order_type": t.order_type, "quantity": t.quantity,
            "limit_price": t.limit_price, "stop_loss": t.stop_loss, "take_profit_1": t.take_profit_1,
            "currency": t.currency, "fx_rate": t.fx_rate, "notional_base": t.notional_base,
            "est_loss_at_stop_base": t.est_loss_at_stop_base, "quote_price": t.quote_price, "quote_time": t.quote_time,
            "warnings": t.warnings, "status": t.status, "status_reason": t.status_reason, "expires_at": t.expires_at,
            "confirmed_at": t.confirmed_at, "broker_order_id": t.broker_order_id, "broker_response": t.broker_response,
            "sends_real_orders": t.sends_real_orders, "failed_attempts": t.failed_attempts, "created_at": t.created_at}


@router.get("/status")
def status(db: Session = Depends(get_db), _: User = Depends(current_user)):
    return live.status(db)


@router.post("/tickets", status_code=201)
def propose(body: TicketIn, user: User = Depends(require_role(Role.ANALYST)), db: Session = Depends(get_db),
            adapter: MarketDataAdapter = Depends(market_data)):
    t, code = live.propose_ticket(db, adapter, user, body.trade_plan_id, body.quantity)
    db.commit()
    return _out(t) | {"confirmation_code": code,
                      "note": "Codice mostrato una sola volta. Rileggi il riepilogo e confermalo entro la scadenza."}


@router.get("/tickets")
def tickets(pg: Pagination = Depends(), db: Session = Depends(get_db), _: User = Depends(current_user)):
    total = db.execute(select(func.count(LiveOrderTicket.id))).scalar_one()
    rows = db.execute(select(LiveOrderTicket).order_by(LiveOrderTicket.id.desc()).offset(pg.offset).limit(pg.page_size)).scalars()
    return {"items": [_out(t) for t in rows], "total": total, "page": pg.page, "page_size": pg.page_size}


@router.post("/tickets/{ticket_id}/confirm")
def confirm(ticket_id: int, body: ConfirmIn, user: User = Depends(require_role(Role.ANALYST)),
            db: Session = Depends(get_db), adapter: MarketDataAdapter = Depends(market_data)):
    try:
        t = live.confirm_ticket(db, adapter, user, ticket_id, body.confirmation_code, body.acknowledge)
    except AppError:
        db.commit()  # conserva tentativi errati, scadenze e annullamenti con il relativo audit
        raise
    db.commit()
    return _out(t)


@router.post("/tickets/{ticket_id}/cancel")
def cancel(ticket_id: int, user: User = Depends(require_role(Role.ANALYST)), db: Session = Depends(get_db)):
    t = live.cancel_ticket(db, user, ticket_id)
    db.commit()
    return _out(t)
