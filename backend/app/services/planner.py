"""Generazione dei piani operativi (TradePlan) dai segnali della scansione.

Agenti deterministici coinvolti:
- Technical Analyst (analytics/planner.py): ingresso, stop, target;
- Risk Manager (services/risk.py): dimensionamento e potere di blocco;
- Portfolio Manager (qui): correlazioni e rischi duplicati con le posizioni aperte.
Stato finale:
- READY    : prezzo verificato e aggiornato dentro l'intervallo di ingresso, rischio approvato, dati non DEMO;
- WAIT     : piano valido ma condizioni non ancora soddisfatte (prezzo fuori intervallo, quotazione da verificare, DEMO);
- REJECTED : bloccato da Risk/Portfolio Manager o livelli non validi;
- EXPIRED  : validità scaduta (job periodico).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.adapters.market_data.base import MarketDataAdapter
from app.ai.committee import run_committee
from app.analytics.scoring import MODEL_VERSION
from app.analytics.planner import PLANNER_VERSION, compute_levels, return_correlation
from app.core.config import get_settings
from app.core.errors import AppError, DataUnavailable, NotFound
from app.models.entities import Portfolio, RiskAssessment, ScanRun, TradePlan, TradingSignal, User
from app.services import audit
from app.services.market_data import fetch_fx, latest_fx, latest_quote
from app.services.portfolio import risk_param, value_portfolio
from app.services.risk import Costs, PortfolioContext, RiskLimits, SizingRequest, compute_position_size
from app.services.scanner import bars_frame

D = lambda x: Decimal(str(x)) if x is not None else None  # noqa: E731
CORR_REDUCE, CORR_BLOCK = 0.80, 0.90
MIN_SCORE = 50.0


def _bdays(start: datetime, n: int) -> datetime:
    d = start
    while n > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def generate_plan(db: Session, adapter: MarketDataAdapter, signal: TradingSignal, portfolio: Portfolio,
                  actor: str) -> TradePlan:
    s = get_settings()
    inst = signal.instrument
    if not signal.eligible or signal.score is None:
        raise AppError("Il titolo non ha superato i filtri dello scanner: nessun piano.", code="not_eligible")
    df = bars_frame(db, inst.id, signal.price_date or datetime.now(UTC).date())
    feats = (signal.details or {}).get("features", {})
    lv = compute_levels(signal.setup, feats, df)
    now = datetime.now(UTC)
    base = dict(signal_id=signal.id, instrument_id=inst.id, strategy_version_id=signal.strategy_version_id,
                portfolio_id=portfolio.id, model_version=f"{MODEL_VERSION}+{PLANNER_VERSION}",
                direction="LONG", strategy_style=signal.strategy_style or "N/A", setup=signal.setup,
                score=signal.score, risk_level=signal.risk_level, market_regime=signal.market_regime,
                price_reference=signal.price, price_source=signal.data_source, price_time=signal.data_as_of,
                currency=inst.currency, is_demo=signal.is_demo,
                rationale_for=signal.rationale.get("for", []), rationale_against=signal.rationale.get("against", []),
                horizon_days=lv.horizon_days, valid_until=_bdays(now, lv.validity_days))
    if not lv.ok:
        plan = TradePlan(**base, entry_price=signal.price, stop_loss=signal.price, take_profit_1=signal.price,
                         quantity=Decimal("0"), notional_base=Decimal("0"), est_loss_at_stop_base=Decimal("0"),
                         status="REJECTED", status_reason=lv.reason, invalidation_conditions=[])
        db.add(plan)
        db.flush()
        run_committee(db, plan, None, actor)
        return plan

    # cambio
    fx = Decimal("1")
    if inst.currency != portfolio.base_currency:
        fxq = latest_fx(db, portfolio.base_currency, inst.currency)
        if fxq is None or (now - fxq.quote_time).days >= 3:
            try:
                fxq = fetch_fx(db, adapter, portfolio.base_currency, inst.currency)
            except DataUnavailable:
                fxq = None
        if fxq is None:
            plan = TradePlan(**base, entry_price=D(lv.entry), stop_loss=D(lv.stop), take_profit_1=D(lv.tp1),
                             quantity=Decimal("0"), notional_base=Decimal("0"), est_loss_at_stop_base=Decimal("0"),
                             status="REJECTED", status_reason=f"Cambio {portfolio.base_currency}/{inst.currency} non disponibile",
                             invalidation_conditions=lv.invalidation)
            db.add(plan)
            db.flush()
            run_committee(db, plan, None, actor)
            return plan
        fx = fxq.price

    # Portfolio Manager: correlazioni con le posizioni aperte
    val = value_portfolio(db, portfolio)
    corr: dict[str, float | None] = {}
    for pv in val.positions:
        if pv.position.instrument_id == inst.id:
            continue
        other = bars_frame(db, pv.position.instrument_id, df.index[-1].date())
        if len(other):
            corr[f"{pv.position.instrument.symbol}.{pv.position.instrument.exchange}"] = \
                return_correlation(df["close"], other["close"])
    max_corr = max((v for v in corr.values() if v is not None), default=None)

    same_inst = sum((v.market_value_base for v in val.positions if v.position.instrument_id == inst.id), Decimal("0"))
    same_sec = sum((v.market_value_base for v in val.positions
                    if inst.sector and v.position.instrument.sector == inst.sector), Decimal("0"))
    ctx = PortfolioContext(val.equity, val.cash, val.aggregate_risk_base, same_inst, same_sec)
    limits = RiskLimits(*(risk_param(portfolio, k) for k in (
        "risk_per_trade_pct", "max_aggregate_risk_pct", "max_position_exposure_pct",
        "max_sector_exposure_pct", "min_cash_reserve_pct")))
    costs = Costs(s.commission_per_order, s.commission_pct, s.slippage_bps)
    req = SizingRequest(entry_price=D(lv.entry), stop_price=D(lv.stop), fx_rate=fx, take_profit_1=D(lv.tp1),
                        take_profit_2=D(lv.tp2), tp1_close_fraction=D(lv.tp1_fraction), gap_stress_pct=D(lv.gap_stress_pct))
    res = compute_position_size(req, ctx, limits, costs)
    pm_notes = []
    qty = res.quantity
    if max_corr is not None and max_corr >= CORR_BLOCK:
        res.blocking_reasons.append(f"Correlazione {max_corr:.2f} con una posizione aperta: rischio duplicato")
        res.approved, qty = False, Decimal("0")
    elif max_corr is not None and max_corr >= CORR_REDUCE and qty > 0:
        qty = (qty / 2).to_integral_value()
        pm_notes.append(f"Correlazione {max_corr:.2f} con posizioni aperte: quantità dimezzata")
        if qty == 0:
            res.approved = False
            res.blocking_reasons.append("Quantità nulla dopo la riduzione per correlazione")
    if qty != res.quantity:  # riduzione del Portfolio Manager: ricalcolo esatto con il nuovo tetto
        notes, blocks = pm_notes, res.blocking_reasons
        from dataclasses import replace
        res = compute_position_size(replace(req, max_quantity=qty), ctx, limits, costs)
        res.blocking_reasons = list(dict.fromkeys(res.blocking_reasons + blocks))
        res.approved = res.approved and qty > 0 and not blocks
        pm_notes = notes

    ra = RiskAssessment(portfolio_id=portfolio.id, approved=res.approved, engine_version=res.engine_version,
                        inputs={"signal_id": signal.id, "entry": lv.entry, "stop": lv.stop, "tp1": lv.tp1, "tp2": lv.tp2,
                                "fx": str(fx), "equity": str(val.equity), "cash": str(val.cash),
                                "open_risk": str(val.aggregate_risk_base), "correlations": corr},
                        result=res.to_dict() | {"portfolio_manager_notes": pm_notes},
                        blocking_reasons=res.blocking_reasons)
    db.add(ra)
    db.flush()

    # stato
    entry = D(lv.entry)
    status, reason = "WAIT", None
    q = latest_quote(db, inst.id)
    if not res.approved:
        status, reason = "REJECTED", "; ".join(res.blocking_reasons)
    elif signal.is_demo:
        reason = "Dati DEMO sintetici: un piano su dati dimostrativi non può essere READY"
    elif q is None or (now - q.quote_time) > timedelta(minutes=s.max_quote_age_minutes):
        reason = "Quotazione da verificare: ultimo prezzo non aggiornato (mercato chiuso o dati in ritardo)"
    elif not (D(lv.entry_low) <= q.price <= D(lv.entry_high)):
        reason = f"Prezzo {q.price} fuori dall'intervallo di ingresso {lv.entry_low}–{lv.entry_high}"
    else:
        status = "READY"
    if status != "REJECTED" and res.reward_risk_net is not None and res.reward_risk_net < 1:
        status, reason = "REJECTED", f"Rapporto rischio/rendimento netto {res.reward_risk_net} inferiore a 1"

    gross = (D(lv.tp1) / entry - 1) * 100
    net = (res.est_gain_tp1_base / res.notional_base * 100) if res.est_gain_tp1_base is not None and res.notional_base else None
    scenarios = [
        {"name": "Target 1 raggiunto", "exit": lv.tp1, "pnl_base": str(res.est_gain_tp1_base) if res.est_gain_tp1_base is not None else None},
        {"name": "Target 2 raggiunto", "exit": lv.tp2, "pnl_base": str(res.est_gain_tp2_base) if res.est_gain_tp2_base is not None else None},
        {"name": "Stop loss", "exit": lv.stop, "pnl_base": str(-res.est_loss_at_stop_base)},
        {"name": f"Gap sotto lo stop (-{lv.gap_stress_pct}%, 99° percentile dei gap a 1 anno)",
         "exit": round(lv.stop * (1 - lv.gap_stress_pct / 100), 4), "pnl_base": str(-res.est_loss_gap_scenario_base)},
        {"name": "Time stop in pareggio", "exit": lv.entry,
         "pnl_base": str(-(2 * res.entry_fee_base)) if res.entry_fee_base is not None else None},
    ]
    plan = TradePlan(
        **base, entry_price=entry, entry_low=D(lv.entry_low), entry_high=D(lv.entry_high), stop_loss=D(lv.stop),
        take_profit_1=D(lv.tp1), take_profit_2=D(lv.tp2), tp1_close_fraction=D(lv.tp1_fraction),
        trailing_stop=lv.trailing, quantity=res.quantity, notional_base=res.notional_base,
        est_loss_at_stop_base=res.est_loss_at_stop_base, est_gain_tp1_base=res.est_gain_tp1_base,
        est_gain_tp2_base=res.est_gain_tp2_base, reward_risk_net=res.reward_risk_net, calibrated_probability=None,
        expected_value_base=None, invalidation_conditions=lv.invalidation, status=status, status_reason=reason,
        risk_assessment_id=ra.id, entry_conditions=lv.entry_conditions, avoid_conditions=lv.avoid_conditions,
        early_exit_conditions=lv.early_exit, stop_rationale=lv.stop_rationale, scenarios=scenarios,
        correlations={"values": corr, "max": max_corr, "notes": pm_notes}, gross_return_tp1_pct=gross.quantize(Decimal("0.01")),
        net_return_tp1_pct=net.quantize(Decimal("0.01")) if net is not None else None, fx_rate=fx,
    )
    db.add(plan)
    db.flush()
    ra.trade_plan_id = plan.id
    audit.record(db, actor=actor, action="plan.generated", entity="trade_plan", entity_id=plan.id, payload={
        "signal_id": signal.id, "status": status, "reason": reason, "entry": lv.entry, "stop": lv.stop,
        "tp1": lv.tp1, "tp2": lv.tp2, "qty": str(res.quantity), "model_version": plan.model_version})
    # comitato deterministico immediato (gratuito); quello AI si richiede a parte o via AI_AUTO_COMMITTEE_TOP_N
    run_committee(db, plan, None, actor)
    return plan


def generate_for_scan(db: Session, adapter: MarketDataAdapter, scan: ScanRun, portfolio: Portfolio, actor: str,
                      top_n: int = 10) -> list[TradePlan]:
    sigs = db.execute(select(TradingSignal).where(
        TradingSignal.scan_run_id == scan.id, TradingSignal.eligible, TradingSignal.score >= MIN_SCORE,
        TradingSignal.setup.is_not(None)).order_by(TradingSignal.score.desc()).limit(top_n)).scalars().all()
    # i piani precedenti ancora aperti dello stesso portafoglio vengono sostituiti
    db.execute(update(TradePlan).where(TradePlan.portfolio_id == portfolio.id,
                                       TradePlan.status.in_(("READY", "WAIT"))).values(
        status="EXPIRED", status_reason="Sostituito da una scansione più recente"))
    return [generate_plan(db, adapter, sg, portfolio, actor) for sg in sigs]


def expire_plans(db: Session) -> int:
    r = db.execute(update(TradePlan).where(TradePlan.status.in_(("READY", "WAIT")),
                                           TradePlan.valid_until < datetime.now(UTC)).values(
        status="EXPIRED", status_reason="Validità del segnale scaduta"))
    return r.rowcount or 0


def get_signal(db: Session, signal_id: int) -> TradingSignal:
    sg = db.get(TradingSignal, signal_id)
    if not sg:
        raise NotFound("Segnale non trovato")
    return sg


def plans_for_user_portfolios(db: Session) -> list[tuple[Portfolio, User]]:
    return list(db.execute(select(Portfolio, User).join(User, Portfolio.owner_id == User.id)
                           .where(Portfolio.mode == "PAPER", User.is_active)).all())
