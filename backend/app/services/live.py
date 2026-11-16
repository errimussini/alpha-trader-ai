"""Live assistito (Fase 8): l'UNICO modulo che può inviare ordini a un broker reale.

Flusso in due passi, sempre con un essere umano:
1. `propose_ticket`: da un piano READY prepara un ticket LIMIT con quantità ricalcolata sul conto del broker,
   limiti di importo e numero di ordini, dati freschi e non DEMO. Restituisce UNA volta un codice monouso.
2. `confirm_ticket`: l'utente rilegge il riepilogo e digita il codice entro la scadenza; tutti i controlli
   vengono ripetuti (modalità, kill switch, prezzo non spostato oltre la soglia, prezzo sopra lo stop).
   Solo allora l'ordine va al broker. Tre codici errati annullano il ticket.

Non esiste alcun percorso automatico: worker, monitoraggio, comitato e assistente non importano questo modulo.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.adapters.broker.base import MarketSnapshot, OrderRequest
from app.adapters.broker.live import LiveBrokerAdapter, get_live_broker
from app.adapters.market_data.base import MarketDataAdapter
from app.core.config import get_settings
from app.core.errors import AppError, Conflict, NotFound, SafetyBlock
from app.models.entities import AlertKind, LiveOrderTicket, Severity, TradePlan, User
from app.services import audit, safety
from app.services.alerts import raise_alert
from app.services.market_data import fetch_fx, fetch_quote
from app.services.portfolio import get_or_create_paper_portfolio
from app.services.risk import Costs, PortfolioContext, RiskLimits, SizingRequest, compute_position_size

MAX_CODE_ATTEMPTS = 3
OPEN_TICKET = "PENDING_CONFIRMATION"


def _hash(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(d: datetime) -> datetime:
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def broker_or_block() -> LiveBrokerAdapter:
    b = get_live_broker()
    if b is None:
        raise SafetyBlock("Nessun broker live configurato.", code="live_disabled")
    return b


def status(db: Session) -> dict:
    s = get_settings()
    ok, why = safety.live_availability()
    b = get_live_broker() if ok else None
    acct = None
    if b is not None:
        a = b.account_summary()
        acct = {"equity_base": str(a.equity_base), "cash_base": str(a.cash_base), "currency": a.currency,
                "as_of": a.as_of.isoformat(), "source": a.source, "is_simulated": a.is_simulated}
    today = _now().replace(hour=0, minute=0, second=0, microsecond=0)
    sent_today = db.execute(select(func.count(LiveOrderTicket.id)).where(
        LiveOrderTicket.status == "SUBMITTED", LiveOrderTicket.confirmed_at >= today)).scalar_one()
    return {
        "available": ok, "reason": why, "mode": safety.get_mode(db), "broker": s.live_broker,
        "sends_real_orders": bool(b and b.sends_real_orders), "account": acct,
        "limits": {"confirmation_ttl_seconds": s.live_confirmation_ttl_seconds,
                   "max_order_notional_base": str(s.live_max_order_notional_base),
                   "max_orders_per_day": s.live_max_orders_per_day, "orders_today": sent_today,
                   "max_price_drift_bps": str(s.live_max_price_drift_bps),
                   "require_second_approver": s.live_require_second_approver},
        "guarantees": [
            "Ogni ordine richiede la conferma umana con codice monouso entro la scadenza.",
            "Solo ordini LIMIT, solo da piani READY, solo con dati freschi e non DEMO.",
            "Nessun processo automatico (worker, monitoraggio, comitato AI, assistente) può inviare ordini live.",
            "Kill switch e limiti di importo e numero di ordini sono verificati sia alla proposta sia alla conferma.",
        ],
    }


def propose_ticket(db: Session, adapter: MarketDataAdapter, user: User, plan_id: int,
                   quantity: Decimal | None = None) -> tuple[LiveOrderTicket, str]:
    s = get_settings()
    safety.assert_can_trade_live(db)
    broker = broker_or_block()
    pf = get_or_create_paper_portfolio(db, user)
    plan = db.get(TradePlan, plan_id)
    if plan is None or plan.portfolio_id != pf.id:
        raise NotFound("Piano non trovato")
    if plan.status != "READY":
        raise SafetyBlock(f"Il piano #{plan.id} è {plan.status}: solo i piani READY possono diventare ordini live.",
                          code="plan_not_ready")
    if _aware(plan.valid_until) < _now():
        raise SafetyBlock("Validità del piano scaduta.", code="plan_expired")
    inst = plan.instrument
    if inst.is_demo or plan.is_demo:
        raise SafetyBlock("Dati DEMO: un ordine reale non può basarsi su dati sintetici.", code="demo_data")
    if not broker.is_tradable(inst.symbol, inst.exchange):
        raise SafetyBlock(f"{inst.symbol}.{inst.exchange} non negoziabile sul broker {broker.name}.", code="not_tradable")

    q = fetch_quote(db, adapter, inst)
    safety.assert_fresh(q.quote_time, what=f"{inst.symbol}.{inst.exchange}")
    if q.is_demo:
        raise SafetyBlock("Quotazione DEMO: ordine live non consentito.", code="demo_data")
    if q.price <= plan.stop_loss:
        raise SafetyBlock(f"Prezzo {q.price} non sopra lo stop {plan.stop_loss}: setup invalidato.", code="invalid_stop")
    hi = plan.entry_high or plan.entry_price
    if q.price > hi * (1 + s.live_max_price_drift_bps / Decimal("10000")):
        raise SafetyBlock(f"Prezzo {q.price} sopra l'intervallo di ingresso (max {hi}): non inseguire il prezzo.",
                          code="price_out_of_range")
    fxq = fetch_fx(db, adapter, s.base_currency, inst.currency)
    if fxq is not None:
        safety.assert_fresh(fxq.quote_time, what=f"cambio {s.base_currency}/{inst.currency}")
        if fxq.is_demo:
            raise SafetyBlock("Cambio DEMO: ordine live non consentito.", code="demo_data")
    fx = fxq.price if fxq else Decimal("1")

    # quantità ricalcolata sul conto del broker con gli stessi limiti di rischio
    acct = broker.account_summary()
    open_risk = Decimal(str(db.execute(select(func.coalesce(func.sum(LiveOrderTicket.est_loss_at_stop_base), 0)).where(
        LiveOrderTicket.status == "SUBMITTED",
        LiveOrderTicket.confirmed_at >= _now() - timedelta(days=30))).scalar_one()))
    limits = RiskLimits(s.risk_per_trade_pct, s.max_aggregate_risk_pct, s.max_position_exposure_pct,
                        s.max_sector_exposure_pct, s.min_cash_reserve_pct)
    sizing = compute_position_size(SizingRequest(entry_price=plan.entry_price, stop_price=plan.stop_loss, fx_rate=fx,
                                                 take_profit_1=plan.take_profit_1, take_profit_2=plan.take_profit_2,
                                                 max_quantity=quantity),
                                   PortfolioContext(equity_base=acct.equity_base, cash_base=acct.cash_base,
                                                    open_risk_base=open_risk),
                                   limits, Costs(s.commission_per_order, s.commission_pct, s.slippage_bps))
    if not sizing.approved or sizing.quantity <= 0:
        raise SafetyBlock("Il Risk Engine non approva l'ordine sul conto live: " + "; ".join(sizing.blocking_reasons),
                          code="risk_rejected", details=sizing.to_dict())
    if sizing.notional_base > s.live_max_order_notional_base:
        raise SafetyBlock(f"Controvalore {sizing.notional_base} oltre il limite per ordine live "
                          f"{s.live_max_order_notional_base}.", code="notional_limit")
    today = _now().replace(hour=0, minute=0, second=0, microsecond=0)
    sent = db.execute(select(func.count(LiveOrderTicket.id)).where(LiveOrderTicket.status == "SUBMITTED",
                                                                   LiveOrderTicket.confirmed_at >= today)).scalar_one()
    if sent >= s.live_max_orders_per_day:
        raise SafetyBlock(f"Raggiunto il limite di {s.live_max_orders_per_day} ordini live al giorno.", code="daily_limit")

    code = f"{secrets.randbelow(10**6):06d}"
    warnings = list(sizing.warnings)
    if not broker.sends_real_orders:
        warnings.insert(0, "Broker di collaudo: nessun ordine verrà inviato a un mercato reale.")
    if acct.is_simulated:
        warnings.append(f"Conto del broker: {acct.source}.")
    t = LiveOrderTicket(
        created_by_id=user.id, trade_plan_id=plan.id, instrument_id=inst.id, client_order_id=f"live-{secrets.token_hex(12)}",
        broker=broker.name, side="BUY", order_type="LIMIT", quantity=sizing.quantity, limit_price=plan.entry_price,
        stop_loss=plan.stop_loss, take_profit_1=plan.take_profit_1, currency=inst.currency, fx_rate=fx,
        notional_base=sizing.notional_base, est_loss_at_stop_base=sizing.est_loss_at_stop_base, quote_id=q.id,
        quote_price=q.price, quote_time=q.quote_time,
        risk_snapshot={"sizing": sizing.to_dict(), "account": {"equity_base": str(acct.equity_base),
                       "cash_base": str(acct.cash_base), "source": acct.source}, "open_live_risk_base": str(open_risk)},
        warnings=warnings, status=OPEN_TICKET, code_hash=_hash(code), failed_attempts=0,
        expires_at=_now() + timedelta(seconds=s.live_confirmation_ttl_seconds),
        broker_response={}, sends_real_orders=broker.sends_real_orders)
    db.add(t)
    db.flush()
    audit.record(db, actor=user.email, action="live.ticket_proposed", entity="live_ticket", entity_id=t.id, payload={
        "plan_id": plan.id, "instrument": f"{inst.symbol}.{inst.exchange}", "qty": t.quantity, "limit": t.limit_price,
        "notional_base": t.notional_base, "quote_id": q.id, "broker": broker.name, "expires_at": t.expires_at})
    return t, code


def confirm_ticket(db: Session, adapter: MarketDataAdapter, user: User, ticket_id: int, code: str,
                   acknowledge: bool) -> LiveOrderTicket:
    s = get_settings()
    t = db.execute(select(LiveOrderTicket).where(LiveOrderTicket.id == ticket_id).with_for_update()).scalar_one_or_none()
    if t is None:
        raise NotFound("Ticket non trovato")
    if t.status != OPEN_TICKET:
        raise Conflict(f"Ticket {t.status}: non confermabile.")
    if s.live_require_second_approver:
        if user.id == t.created_by_id:
            raise SafetyBlock("Serve la conferma di un secondo utente (quattro occhi).", code="second_approver")
    elif user.id != t.created_by_id:
        raise SafetyBlock("Solo chi ha preparato il ticket può confermarlo.", code="not_owner")
    if _aware(t.expires_at) < _now():
        t.status, t.status_reason = "EXPIRED", "conferma non arrivata entro la scadenza"
        audit.record(db, actor=user.email, action="live.ticket_expired", entity="live_ticket", entity_id=t.id)
        db.flush()
        raise SafetyBlock("Ticket scaduto: preparane uno nuovo con dati aggiornati.", code="ticket_expired")
    if not acknowledge:
        raise AppError("Conferma di aver letto il riepilogo e i rischi (acknowledge=true).", code="ack_required")
    if not hmac.compare_digest(_hash(code.strip()), t.code_hash):
        t.failed_attempts += 1
        if t.failed_attempts >= MAX_CODE_ATTEMPTS:
            t.status, t.status_reason = "CANCELLED", "troppi codici errati"
        audit.record(db, actor=user.email, action="live.ticket_bad_code", entity="live_ticket", entity_id=t.id,
                     payload={"attempts": t.failed_attempts})
        db.flush()
        raise SafetyBlock("Codice di conferma errato.", code="bad_code", details={"attempts_left": max(0, MAX_CODE_ATTEMPTS - t.failed_attempts)})

    # tutti i controlli ripetuti al momento della conferma
    safety.assert_can_trade_live(db)
    broker = broker_or_block()
    if broker.name != t.broker:
        raise SafetyBlock("Il broker configurato è cambiato dopo la preparazione del ticket.", code="broker_changed")
    inst = t.instrument
    q = fetch_quote(db, adapter, inst)
    safety.assert_fresh(q.quote_time, what=f"{inst.symbol}.{inst.exchange}")
    drift_bps = abs(q.price / t.quote_price - 1) * Decimal("10000")
    if drift_bps > s.live_max_price_drift_bps:
        t.status, t.status_reason = "CANCELLED", f"prezzo spostato di {drift_bps:.0f} bps dalla preparazione"
        audit.record(db, actor=user.email, action="live.ticket_cancelled", entity="live_ticket", entity_id=t.id,
                     payload={"reason": t.status_reason, "quote_id": q.id})
        db.flush()
        raise SafetyBlock(f"Il prezzo si è spostato di {drift_bps:.0f} bps (max {s.live_max_price_drift_bps}): ticket annullato.",
                          code="price_drift")
    if t.stop_loss is not None and q.price <= t.stop_loss:
        raise SafetyBlock("Prezzo non più sopra lo stop.", code="invalid_stop")

    t.confirmed_by_id, t.confirmed_at = user.id, _now()
    res = broker.submit_order(OrderRequest(t.client_order_id, inst.symbol, inst.exchange, t.side, "LIMIT",
                                           t.quantity, t.limit_price),
                              MarketSnapshot(q.price, q.bid, q.ask, q.quote_time, q.source, q.volume))
    t.broker_order_id, t.broker_response = res.broker_order_id, {"status": res.status, "reject_reason": res.reject_reason,
                                                                  **res.raw}
    if res.status in ("REJECTED", "UNKNOWN"):
        t.status, t.status_reason = "REJECTED", res.reject_reason or "rifiutato dal broker"
    else:
        t.status, t.status_reason = "SUBMITTED", f"inviato al broker ({res.status})"
    db.flush()
    audit.record(db, actor=user.email, action="live.order_submitted" if t.status == "SUBMITTED" else "live.order_rejected",
                 entity="live_ticket", entity_id=t.id, payload={
                     "client_order_id": t.client_order_id, "broker": t.broker, "broker_order_id": t.broker_order_id,
                     "qty": t.quantity, "limit": t.limit_price, "quote_id": q.id, "drift_bps": drift_bps,
                     "broker_status": res.status, "sends_real_orders": t.sends_real_orders})
    real = "" if t.sends_real_orders else " (broker di collaudo: nessun ordine reale)"
    raise_alert(db, AlertKind.SYSTEM, Severity.WARNING if t.status == "SUBMITTED" else Severity.CRITICAL,
                f"Ordine live #{t.id} {t.side} {t.quantity} {inst.symbol}.{inst.exchange} LIMIT {t.limit_price}: "
                f"{t.status} su conferma di {user.email}{real}.", instrument_id=inst.id, data={"ticket_id": t.id})
    return t


def cancel_ticket(db: Session, user: User, ticket_id: int) -> LiveOrderTicket:
    t = db.get(LiveOrderTicket, ticket_id)
    if t is None:
        raise NotFound("Ticket non trovato")
    if t.status != OPEN_TICKET:
        raise Conflict(f"Ticket {t.status}: non annullabile qui (per un ordine già inviato usa il broker).")
    t.status, t.status_reason = "CANCELLED", f"annullato da {user.email}"
    audit.record(db, actor=user.email, action="live.ticket_cancelled", entity="live_ticket", entity_id=t.id)
    db.flush()
    return t
