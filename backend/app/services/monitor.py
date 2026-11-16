"""Monitoraggio del paper trading (Fase 6).

Un ciclo (`run_monitor`) per un portafoglio PAPER, eseguito dal worker ogni `monitor_interval_seconds`
oppure su richiesta (POST /api/paper/monitor):

1. Quotazioni: aggiorna le quotazioni degli strumenti con posizioni, ordini aperti o piani attivi.
   Quotazione mancante o obsoleta -> alert DATA_ANOMALY e nessuna azione su quello strumento.
2. Ordini aperti (LIMIT o residui di fill parziali): annullati se scaduti, se il piano non è più READY,
   se il prezzo è sotto lo stop autorizzato o se i limiti di perdita bloccano nuovi acquisti;
   altrimenti rivalutati dal PaperBroker con la quotazione verificata.
3. Posizioni: stop (o trailing) toccato -> uscita totale; TP2 -> uscita totale; TP1 -> uscita parziale
   (frazione del piano), stop al pareggio e trailing attivo. Lo stop si muove SOLO verso l'alto.
   Sono livelli già autorizzati all'ingresso; con `paper_auto_exits = False`, in modalità Research o con
   il kill switch attivo viene emesso solo l'alert (e il testo dice che l'uscita NON è stata eseguita).
   Posizione oltre l'orizzonte del piano o con setup bocciato dall'ultima analisi -> REVIEW_POSITION.
4. Piani attivi senza posizione: prezzo sotto lo stop -> piano EXPIRED + SETUP_INVALIDATED;
   piano READY con prezzo nell'intervallo di ingresso -> ENTRY_OPPORTUNITY (notifica, nessun ordine).
5. Portafoglio: rischio aggregato oltre il limite o drawdown oltre il limite -> AGGREGATE_RISK.
6. Riconciliazione di cassa, posizioni ed eseguiti -> BROKER_ANOMALY se non torna.
7. Snapshot giornaliero dell'equity (base del rendimento giornaliero).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import ROUND_DOWN, Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.adapters.market_data.base import MarketDataAdapter
from app.core.config import get_settings
from app.core.errors import AppError, DataUnavailable, SafetyBlock
from app.models.entities import (
    AlertKind, CashMovement, EquitySnapshot, Execution, Instrument, MarketQuote, Order, Portfolio, Position,
    Severity, TradePlan, User,
)
from app.services import audit, safety
from app.services.alerts import raise_alert
from app.services.market_data import fetch_fx, fetch_quote
from app.services.paper_trading import (
    OPEN_STATUSES, assert_buy_limits, cancel_order, open_position, place_paper_order, submit_to_broker,
)
from app.services.portfolio import risk_param, value_portfolio

log = logging.getLogger("monitor")
MONITOR_VERSION = "monitor/1.0.0"
NOT_ORDER = "Notifica: nessun ordine è stato inviato."


@dataclass
class Market:
    quote: MarketQuote
    fx: Decimal
    is_demo: bool


@dataclass
class MonitorReport:
    portfolio_id: int
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    can_execute: bool = False
    blocked_reason: str | None = None
    instruments: int = 0
    data_issues: list[str] = field(default_factory=list)
    orders_checked: int = 0
    orders_filled: list[int] = field(default_factory=list)
    orders_cancelled: list[int] = field(default_factory=list)
    exits: list[dict] = field(default_factory=list)
    stops_raised: list[dict] = field(default_factory=list)
    plans_invalidated: list[int] = field(default_factory=list)
    alerts: int = 0
    reconciliation_ok: bool = True
    snapshot: dict | None = None

    def to_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items()}
        d["started_at"] = self.started_at.isoformat()
        d["version"] = MONITOR_VERSION
        return d


class _Ctx:
    def __init__(self, db: Session, adapter: MarketDataAdapter, p: Portfolio, owner: User, rep: MonitorReport):
        self.db, self.adapter, self.p, self.owner, self.rep = db, adapter, p, owner, rep
        self.market: dict[int, Market | None] = {}

    def alert(self, kind: str, severity: str, message: str, *, instrument_id: int | None = None,
              data: dict | None = None, key: str | None = None) -> None:
        a = raise_alert(self.db, kind, severity, message, portfolio_id=self.p.id, instrument_id=instrument_id,
                        data=data, dedup_key=f"p{self.p.id}:{key}" if key else None)
        if a is not None:
            self.rep.alerts += 1

    def get_market(self, inst: Instrument) -> Market | None:
        if inst.id in self.market:
            return self.market[inst.id]
        m = None
        tk = f"{inst.symbol}.{inst.exchange}"
        try:
            q = fetch_quote(self.db, self.adapter, inst)
            safety.assert_fresh(q.quote_time, what=tk)
            fxq = fetch_fx(self.db, self.adapter, self.p.base_currency, inst.currency)
            if fxq is not None:
                safety.assert_fresh(fxq.quote_time, what=f"cambio {self.p.base_currency}/{inst.currency}")
            m = Market(q, fxq.price if fxq else Decimal("1"), q.is_demo or inst.is_demo or bool(fxq and fxq.is_demo))
        except (DataUnavailable, SafetyBlock) as e:
            self.rep.data_issues.append(f"{tk}: {e}")
            self.alert(AlertKind.DATA_ANOMALY, Severity.WARNING,
                       f"Monitoraggio sospeso su {tk}: {e}. Stop e target non sono verificabili finché i dati "
                       f"non tornano aggiornati.", instrument_id=inst.id, key=f"data:{inst.id}")
        self.market[inst.id] = m
        return m


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(d: datetime) -> datetime:
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def _tk(i: Instrument) -> str:
    return f"{i.symbol}.{i.exchange}"


def _open_sell(db: Session, p: Portfolio, instrument_id: int) -> Order | None:
    return db.execute(select(Order).where(Order.portfolio_id == p.id, Order.instrument_id == instrument_id,
                                          Order.side == "SELL", Order.status.in_(OPEN_STATUSES))
                      .limit(1)).scalar_one_or_none()


# ---------------------------------------------------------------- 2) ordini aperti
def _check_orders(c: _Ctx) -> None:
    db, p = c.db, c.p
    orders = list(db.execute(select(Order).where(Order.portfolio_id == p.id, Order.mode == "PAPER",
                                                 Order.status.in_(("OPEN", "PARTIALLY_FILLED")))
                             .order_by(Order.id)).scalars())
    for o in orders:
        c.rep.orders_checked += 1
        inst = o.instrument
        why = None
        if o.valid_until is not None and _aware(o.valid_until) < _now():
            why = "validità scaduta"
        plan = db.get(TradePlan, o.trade_plan_id) if o.trade_plan_id else None
        if why is None and o.side == "BUY" and plan is not None and plan.status != "READY":
            why = f"il piano #{plan.id} è {plan.status}" + (f" ({plan.status_reason})" if plan.status_reason else "")
        if why is None and o.side == "SELL" and open_position(db, p.id, inst.id) is None:
            why = "nessuna posizione da vendere"
        m = None
        if why is None:
            m = c.get_market(inst)
            if m is None:
                continue
            if o.side == "BUY" and o.stop_loss is not None and m.quote.price <= o.stop_loss:
                why = f"prezzo {m.quote.price} sotto lo stop autorizzato {o.stop_loss}: setup invalidato"
        if why is None and o.side == "BUY":
            try:
                assert_buy_limits(db, p)
            except SafetyBlock as e:
                why = str(e)
        if why is not None:
            cancel_order(db, o, "monitor", why)
            c.rep.orders_cancelled.append(o.id)
            c.alert(AlertKind.SETUP_INVALIDATED, Severity.INFO,
                    f"Ordine simulato #{o.id} {o.side} {_tk(inst)} annullato: {why}. "
                    f"Eseguiti {o.filled_quantity} su {o.quantity}.", instrument_id=inst.id,
                    data={"order_id": o.id}, key=f"ordcancel:{o.id}")
            continue
        if not c.rep.can_execute:
            continue
        before = o.filled_quantity
        res = submit_to_broker(db, o, p, inst, m.quote, m.fx, m.is_demo)
        if o.filled_quantity > before:
            c.rep.orders_filled.append(o.id)
            audit.record(db, actor="monitor", action="paper.order_fill", entity="order", entity_id=o.id, payload={
                "status": o.status, "filled_quantity": o.filled_quantity, "quote_id": m.quote.id,
                "fills": [{"qty": f.quantity, "price": f.price, "fee": f.fee} for f in res.fills]})
            c.alert(AlertKind.SYSTEM, Severity.INFO,
                    f"Ordine simulato #{o.id} {o.side} {_tk(inst)}: eseguite {o.filled_quantity - before} "
                    f"(totale {o.filled_quantity}/{o.quantity}) sul PaperBroker. Esecuzione simulata, non reale.",
                    instrument_id=inst.id, data={"order_id": o.id}, key=f"fill:{o.id}:{o.filled_quantity}")
        if o.status in ("REJECTED", "CANCELLED"):
            c.alert(AlertKind.BROKER_ANOMALY, Severity.WARNING,
                    f"Ordine simulato #{o.id} {_tk(inst)} {o.status}: {o.reject_reason}.", instrument_id=inst.id,
                    data={"order_id": o.id}, key=f"reject:{o.id}")


# ---------------------------------------------------------------- 3) posizioni
def _exit(c: _Ctx, pos: Position, qty: Decimal, purpose: str, kind: str, why: str) -> Order | None:
    inst = pos.instrument
    n = c.db.execute(select(func.count(Order.id)).where(Order.client_order_id.like(f"mon-{purpose}-{pos.id}-%"))).scalar_one()
    executed = None
    if c.rep.can_execute and get_settings().paper_auto_exits:
        try:
            out = place_paper_order(c.db, adapter=c.adapter, user=c.owner, portfolio=c.p, instrument=inst, side="SELL",
                                    order_type="MARKET", quantity=qty, client_order_id=f"mon-{purpose}-{pos.id}-{n + 1}",
                                    purpose=purpose, actor="monitor")
            executed = out.order
        except (AppError, DataUnavailable) as e:
            why += f" Uscita simulata non eseguita: {e}"
    elif not get_settings().paper_auto_exits:
        why += " Uscita automatica disattivata (PAPER_AUTO_EXITS=false): valuta tu se chiudere."
    else:
        why += f" Uscita simulata NON eseguita: {c.rep.blocked_reason}."
    sev = Severity.CRITICAL if purpose in ("STOP", "TRAIL") else Severity.WARNING
    if executed is not None:
        c.rep.exits.append({"position_id": pos.id, "order_id": executed.id, "purpose": purpose, "status": executed.status,
                            "quantity": str(executed.filled_quantity)})
        msg = (f"{why} Ordine simulato #{executed.id} SELL {executed.filled_quantity} {_tk(inst)}: {executed.status} "
               f"sul PaperBroker (esecuzione simulata, non reale).")
    else:
        msg = why
    c.alert(kind, sev, msg, instrument_id=inst.id,
            data={"position_id": pos.id, "purpose": purpose, "order_id": executed.id if executed else None},
            key=f"exit:{purpose}:{pos.id}:{executed.id if executed else 'pending'}")
    return executed


def _raise_stop(c: _Ctx, pos: Position, new_stop: Decimal, why: str) -> None:
    """Unico punto in cui il monitoraggio muove uno stop: solo verso l'alto, mai oltre il prezzo."""
    old = pos.stop_loss
    if old is not None and new_stop <= old:
        return
    pos.stop_loss = new_stop.quantize(Decimal("0.00000001"))
    c.rep.stops_raised.append({"position_id": pos.id, "from": str(old), "to": str(pos.stop_loss), "why": why})
    audit.record(c.db, actor="monitor", action="position.stop_raised", entity="position", entity_id=pos.id,
                 payload={"from": old, "to": pos.stop_loss, "why": why})


def _check_positions(c: _Ctx) -> None:
    db, p = c.db, c.p
    for pos in list(db.execute(select(Position).where(Position.portfolio_id == p.id, Position.status == "OPEN")
                               .order_by(Position.id)).scalars()):
        inst = pos.instrument
        m = c.get_market(inst)
        if m is None:
            continue
        px = m.quote.price
        pos.last_checked_at = _now()
        pos.high_water = max(pos.high_water or px, px)
        plan = db.get(TradePlan, pos.trade_plan_id) if pos.trade_plan_id else None
        pending = _open_sell(db, p, inst.id)
        if pending is None:
            tk = _tk(inst)
            if pos.stop_loss is not None and px <= pos.stop_loss:
                purpose = "TRAIL" if pos.trailing_active else "STOP"
                _exit(c, pos, pos.quantity, purpose, AlertKind.STOP_HIT,
                      f"{'Trailing stop' if purpose == 'TRAIL' else 'Stop loss'} raggiunto su {tk}: prezzo {px} "
                      f"<= stop {pos.stop_loss}.")
                continue
            if pos.take_profit_2 is not None and px >= pos.take_profit_2:
                _exit(c, pos, pos.quantity, "TP2", AlertKind.TARGET_HIT,
                      f"Take profit 2 raggiunto su {tk}: prezzo {px} >= {pos.take_profit_2}.")
                continue
            if not pos.tp1_done and pos.take_profit_1 is not None and px >= pos.take_profit_1:
                frac = (plan.tp1_close_fraction if plan and plan.tp1_close_fraction else Decimal("0.5"))
                part = (pos.quantity * frac).to_integral_value(ROUND_DOWN)
                no_tp2 = pos.take_profit_2 is None
                qty = pos.quantity if (no_tp2 or part >= pos.quantity) else part
                done = qty == 0
                if qty > 0:
                    o = _exit(c, pos, qty, "TP1", AlertKind.TARGET_HIT,
                              f"Take profit 1 raggiunto su {tk}: prezzo {px} >= {pos.take_profit_1}. "
                              f"Chiusura {'totale' if qty == pos.quantity else f'parziale di {qty}'} come da piano.")
                    done = o is not None and o.filled_quantity > 0
                if not done:
                    continue  # uscita non eseguita: si riprova al prossimo ciclo (alert già emesso una volta)
                pos.tp1_done = True  # stessa istanza aggiornata da _apply_fill (identity map)
                if pos.status == "OPEN":
                    _raise_stop(c, pos, pos.avg_price, "TP1 raggiunto: stop al pareggio (prezzo medio, commissione "
                                                       "d'ingresso inclusa)")
                    pos.trailing_active = pos.trailing_distance is not None
                continue
        if pos.trailing_active and pos.trailing_distance and pos.high_water:
            cand = pos.high_water - pos.trailing_distance
            if cand < px:
                _raise_stop(c, pos, cand, f"trailing: massimo osservato {pos.high_water} - {pos.trailing_distance}")
        # rivalutazione
        held = (_now() - _aware(pos.opened_at)).days
        if plan is not None and plan.horizon_days and held > plan.horizon_days:
            c.alert(AlertKind.REVIEW_POSITION, Severity.WARNING,
                    f"{_tk(inst)} aperta da {held} giorni, oltre l'orizzonte del piano ({plan.horizon_days}). "
                    f"Rivaluta la posizione. {NOT_ORDER}", instrument_id=inst.id, data={"position_id": pos.id},
                    key=f"horizon:{pos.id}")
        latest = db.execute(select(TradePlan).where(TradePlan.portfolio_id == p.id, TradePlan.instrument_id == inst.id,
                                                    TradePlan.created_at > pos.opened_at)
                            .order_by(TradePlan.created_at.desc()).limit(1)).scalar_one_or_none()
        if latest is not None:
            dec = ((latest.committee or {}).get("decision") or {}).get("decision")
            if latest.status == "REJECTED" or dec in ("EXIT", "REDUCE"):
                reason = f"comitato: {dec}" if dec in ("EXIT", "REDUCE") else f"piano #{latest.id} REJECTED ({latest.status_reason})"
                c.alert(AlertKind.REVIEW_POSITION, Severity.WARNING,
                        f"Rivaluta {_tk(inst)}: l'analisi più recente non conferma il setup ({reason}). {NOT_ORDER}",
                        instrument_id=inst.id, data={"position_id": pos.id, "plan_id": latest.id},
                        key=f"review:{pos.id}:{latest.id}")


# ---------------------------------------------------------------- 4) piani attivi
def _check_plans(c: _Ctx) -> None:
    db, p = c.db, c.p
    plans = list(db.execute(select(TradePlan).where(TradePlan.portfolio_id == p.id,
                                                    TradePlan.status.in_(("READY", "WAIT")),
                                                    TradePlan.valid_until > _now())).scalars())
    for plan in plans:
        inst = plan.instrument
        if open_position(db, p.id, inst.id) is not None:
            continue
        m = c.get_market(inst)
        if m is None:
            continue
        px = m.quote.price
        if px <= plan.stop_loss:
            plan.status = "EXPIRED"
            plan.status_reason = f"Setup invalidato dal monitoraggio: prezzo {px} sotto lo stop {plan.stop_loss}."
            c.rep.plans_invalidated.append(plan.id)
            audit.record(db, actor="monitor", action="plan.invalidated", entity="trade_plan", entity_id=plan.id,
                         payload={"price": px, "stop": plan.stop_loss, "quote_id": m.quote.id})
            c.alert(AlertKind.SETUP_INVALIDATED, Severity.INFO,
                    f"Piano #{plan.id} {_tk(inst)} invalidato: prezzo {px} sotto lo stop {plan.stop_loss}. {NOT_ORDER}",
                    instrument_id=inst.id, data={"plan_id": plan.id}, key=f"invalid:{plan.id}")
            continue
        has_order = db.execute(select(Order.id).where(Order.trade_plan_id == plan.id).limit(1)).scalar_one_or_none()
        lo, hi = plan.entry_low or plan.entry_price, plan.entry_high or plan.entry_price
        if plan.status == "READY" and has_order is None and lo <= px <= hi:
            c.alert(AlertKind.ENTRY_OPPORTUNITY, Severity.INFO,
                    f"{_tk(inst)} a {px}, dentro l'intervallo di ingresso del piano #{plan.id} ({lo}-{hi}). "
                    f"Serve la tua conferma per inviare l'ordine simulato. {NOT_ORDER}",
                    instrument_id=inst.id, data={"plan_id": plan.id, "price": str(px)}, key=f"entry:{plan.id}")


# ---------------------------------------------------------------- 6) riconciliazione
def reconcile(db: Session, p: Portfolio) -> dict:
    """Confronta lo stato (cassa, posizioni, ordini) con il registro di movimenti ed eseguiti."""
    issues: list[dict] = []
    moved = Decimal(str(db.execute(select(func.coalesce(func.sum(CashMovement.amount_base), 0))
                                   .where(CashMovement.portfolio_id == p.id)).scalar_one()))
    if abs(moved - p.cash) > Decimal("0.01"):
        issues.append({"check": "cash", "expected": str(moved), "actual": str(p.cash),
                       "message": "La liquidità non coincide con la somma dei movimenti di cassa."})
    rows = db.execute(select(Order.instrument_id, Order.side, func.sum(Execution.quantity))
                      .join(Execution, Execution.order_id == Order.id)
                      .where(Order.portfolio_id == p.id).group_by(Order.instrument_id, Order.side)).all()
    net: dict[int, Decimal] = {}
    for iid, side, q in rows:
        net[iid] = net.get(iid, Decimal("0")) + (Decimal(str(q)) if side == "BUY" else -Decimal(str(q)))
    held: dict[int, Decimal] = {}
    for iid, q in db.execute(select(Position.instrument_id, func.sum(Position.quantity))
                             .where(Position.portfolio_id == p.id, Position.status == "OPEN")
                             .group_by(Position.instrument_id)).all():
        held[iid] = Decimal(str(q))
    for iid in set(net) | set(held):
        a, b = net.get(iid, Decimal("0")), held.get(iid, Decimal("0"))
        if a != b:
            issues.append({"check": "position", "instrument_id": iid, "expected": str(a), "actual": str(b),
                           "message": "La quantità in posizione non coincide con gli eseguiti."})
    for oid, fq, eq in db.execute(select(Order.id, Order.filled_quantity, func.coalesce(func.sum(Execution.quantity), 0))
                                  .outerjoin(Execution, Execution.order_id == Order.id)
                                  .where(Order.portfolio_id == p.id).group_by(Order.id, Order.filled_quantity)).all():
        if Decimal(str(fq)) != Decimal(str(eq)):
            issues.append({"check": "order", "order_id": oid, "expected": str(eq), "actual": str(fq),
                           "message": "La quantità eseguita dell'ordine non coincide con gli eseguiti."})
    neg = [pos.id for pos in db.execute(select(Position).where(Position.portfolio_id == p.id,
                                                               Position.status == "OPEN",
                                                               Position.quantity <= 0)).scalars()]
    if neg:
        issues.append({"check": "position_qty", "positions": neg, "message": "Posizioni aperte con quantità non positiva."})
    if p.cash < 0:
        issues.append({"check": "cash_negative", "actual": str(p.cash), "message": "Liquidità negativa (leva non ammessa)."})
    return {"ok": not issues, "checked_at": _now().isoformat(), "cash_from_movements": str(moved.quantize(Decimal("0.01"))),
            "cash": str(p.cash.quantize(Decimal("0.01"))), "issues": issues}


# ---------------------------------------------------------------- 7) snapshot
def take_snapshot(db: Session, p: Portfolio, day: date | None = None) -> EquitySnapshot:
    v = value_portfolio(db, p)
    day = day or _now().date()
    snap = db.execute(select(EquitySnapshot).where(EquitySnapshot.portfolio_id == p.id,
                                                   EquitySnapshot.snap_date == day)).scalar_one_or_none()
    if snap is None:
        snap = EquitySnapshot(portfolio_id=p.id, snap_date=day)
        db.add(snap)
    snap.equity, snap.cash, snap.market_value = v.equity, v.cash, v.market_value
    snap.realized_pnl, snap.unrealized_pnl = v.realized_pnl, v.unrealized_pnl
    snap.drawdown_pct, snap.aggregate_risk_pct = v.drawdown_pct, v.aggregate_risk_pct
    snap.open_positions = len(v.positions)
    snap.stale = any(x.stale for x in v.positions)
    snap.is_demo = any(x.is_demo for x in v.positions)
    snap.taken_at = _now()
    db.flush()
    return snap


def daily_return_pct(db: Session, p: Portfolio, equity: Decimal) -> Decimal | None:
    prev = db.execute(select(EquitySnapshot).where(EquitySnapshot.portfolio_id == p.id,
                                                   EquitySnapshot.snap_date < _now().date())
                      .order_by(EquitySnapshot.snap_date.desc()).limit(1)).scalar_one_or_none()
    if prev is None or prev.equity <= 0:
        return None
    return ((equity / prev.equity - 1) * 100).quantize(Decimal("0.01"))


# ---------------------------------------------------------------- ciclo
def run_monitor(db: Session, adapter: MarketDataAdapter, p: Portfolio, actor: str = "monitor") -> MonitorReport:
    db.execute(select(Portfolio.id).where(Portfolio.id == p.id).with_for_update())  # un ciclo alla volta
    owner = db.get(User, p.owner_id)
    rep = MonitorReport(portfolio_id=p.id)
    try:
        safety.assert_can_trade_paper(db)
        rep.can_execute = True
    except SafetyBlock as e:
        rep.blocked_reason = str(e)
    c = _Ctx(db, adapter, p, owner, rep)
    _check_orders(c)
    _check_positions(c)
    _check_plans(c)
    rep.instruments = len(c.market)

    v = value_portfolio(db, p)
    today = _now().date().isoformat()
    max_agg = risk_param(p, "max_aggregate_risk_pct")
    if v.aggregate_risk_pct > max_agg:
        c.alert(AlertKind.AGGREGATE_RISK, Severity.WARNING,
                f"Rischio aggregato a stop {v.aggregate_risk_pct}% dell'equity, oltre il limite {max_agg}%. "
                f"Nuovi piani non approvabili finché non scende. {NOT_ORDER}",
                data={"aggregate_risk_pct": str(v.aggregate_risk_pct)}, key=f"aggrisk:{today}")
    max_dd = risk_param(p, "max_total_drawdown_pct")
    if v.drawdown_pct >= max_dd:
        c.alert(AlertKind.AGGREGATE_RISK, Severity.CRITICAL,
                f"Drawdown {v.drawdown_pct}% oltre il limite {max_dd}%: nuovi acquisti bloccati. {NOT_ORDER}",
                data={"drawdown_pct": str(v.drawdown_pct)}, key=f"dd:{today}")

    rec = reconcile(db, p)
    rep.reconciliation_ok = rec["ok"]
    if not rec["ok"]:
        c.alert(AlertKind.BROKER_ANOMALY, Severity.CRITICAL,
                f"Riconciliazione non riuscita: {len(rec['issues'])} discrepanze ({rec['issues'][0]['message']}). "
                f"Verifica prima di operare.", data=rec, key=f"recon:{today}")
    snap = take_snapshot(db, p)
    rep.snapshot = {"date": snap.snap_date.isoformat(), "equity": str(snap.equity), "stale": snap.stale}
    acted = (rep.exits or rep.orders_filled or rep.orders_cancelled or rep.stops_raised or rep.plans_invalidated
             or not rep.reconciliation_ok or actor not in ("monitor", "worker"))
    if acted:  # i cicli senza azioni non riempiono l'audit (ogni azione ha comunque il proprio record)
        audit.record(db, actor=actor, action="monitor.run", entity="portfolio", entity_id=p.id, payload=rep.to_dict())
    return rep
