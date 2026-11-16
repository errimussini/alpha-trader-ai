"""Validazione prospettica (Fase 7): esiti reali dei piani, confronto con il backtest, benchmark,
affidabilità di dati e segnali.

Principio: ogni piano viene giudicato SOLO con dati arrivati dopo la sua creazione. Le metriche diventano
"validate" con almeno MIN_TRADES operazioni chiuse e nessun dato DEMO. Se la validazione prospettica mostra un
peggioramento significativo rispetto al backtest, il planner smette di pubblicare probabilità ed EV calibrati.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import numpy as np
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.adapters.market_data.base import MarketDataAdapter
from app.backtest import metrics as M
from app.core.config import get_settings
from app.core.errors import DataUnavailable
from app.models.entities import (
    Alert, AlertKind, EquitySnapshot, HistoricalBar, Instrument, Job, MarketQuote, ModelEvaluation, Portfolio,
    PlanOutcome, Position, ScanRun, Severity, Strategy, StrategyVersion, TradePlan,
)
from app.services import audit, jobs
from app.services.alerts import raise_alert
from app.services.market_data import find_instrument, sync_bars
from app.validation.outcomes import CLOSED, FINAL, OUTCOME_VERSION, Bar, PlanSpec, evaluate

log = logging.getLogger("validation")
MIN_TRADES = 30
LOOKBACK_DAYS = 400
OPTIONAL_STAGES = {"fundamentals", "earnings", "news", "events"}
SCORE_BUCKETS = [(0, 60), (60, 70), (70, 80), (80, 90), (90, 101)]


def _today() -> date:
    return datetime.now(UTC).date()


def _f(x) -> float | None:
    return float(x) if x is not None else None


# ---------------------------------------------------------------- registrazione esiti
def _committee_decision(plan: TradePlan) -> str | None:
    return ((plan.committee or {}).get("decision") or {}).get("decision")


def record_plan(db: Session, plan: TradePlan) -> PlanOutcome | None:
    """Registra il piano per il tracciamento prospettico (uno per segnale: i piani dello stesso segnale su più
    portafogli sono la stessa previsione)."""
    if db.execute(select(PlanOutcome.id).where(PlanOutcome.trade_plan_id == plan.id)).scalar_one_or_none():
        return None
    if plan.signal_id is not None:
        dup = db.execute(select(PlanOutcome.id).join(TradePlan, PlanOutcome.trade_plan_id == TradePlan.id)
                         .where(TradePlan.signal_id == plan.signal_id).limit(1)).scalar_one_or_none()
        if dup:
            return None
    pt = plan.price_time if plan.price_time.tzinfo else plan.price_time.replace(tzinfo=UTC)
    o = PlanOutcome(trade_plan_id=plan.id, instrument_id=plan.instrument_id, model_version=plan.model_version[:64],
                    setup=plan.setup, score=plan.score, status_at_creation=plan.status,
                    committee_decision=_committee_decision(plan), created_for=pt.date(), outcome="PENDING",
                    is_demo=plan.is_demo, engine_version=OUTCOME_VERSION, notes=[],
                    bars_evaluated=0, created_at=datetime.now(UTC), updated_at=datetime.now(UTC))
    db.add(o)
    db.flush()
    return o


def backfill(db: Session) -> int:
    """Piani creati prima della Fase 7 (o mancati): registrati con lo stato attuale come stato iniziale."""
    since = datetime.now(UTC) - timedelta(days=LOOKBACK_DAYS)
    plans = db.execute(select(TradePlan).outerjoin(PlanOutcome, PlanOutcome.trade_plan_id == TradePlan.id)
                       .where(PlanOutcome.id.is_(None), TradePlan.created_at >= since)
                       .order_by(TradePlan.id)).scalars().all()
    n = 0
    for p in plans:
        o = record_plan(db, p)
        if o is not None:
            if p.status == "EXPIRED":
                o.notes = ["registrato dopo la creazione: stato iniziale non noto (EXPIRED)"]
            n += 1
    return n


def _bars(db: Session, instrument_id: int, after: date) -> list[Bar]:
    rows = db.execute(select(HistoricalBar).where(HistoricalBar.instrument_id == instrument_id,
                                                  HistoricalBar.timeframe == "1d", HistoricalBar.bar_date > after)
                      .order_by(HistoricalBar.bar_date)).scalars()
    return [Bar(r.bar_date, r.open, r.high, r.low, r.close) for r in rows]


def update_outcomes(db: Session, adapter: MarketDataAdapter | None = None, today: date | None = None) -> dict:
    s = get_settings()
    today = today or _today()
    pending = db.execute(select(PlanOutcome).where(PlanOutcome.outcome.not_in(FINAL))
                         .order_by(PlanOutcome.id)).scalars().all()
    synced: set[int] = set()
    errors: list[str] = []
    changed = 0
    for o in pending:
        plan = o.plan
        inst = o.instrument
        if adapter is not None and inst.id not in synced:
            synced.add(inst.id)
            last = db.execute(select(func.max(HistoricalBar.bar_date)).where(HistoricalBar.instrument_id == inst.id)).scalar()
            if last is None or last < today - timedelta(days=1):
                try:
                    sync_bars(db, adapter, inst, (last or o.created_for) + timedelta(days=1), today)
                except DataUnavailable as e:
                    errors.append(f"{inst.symbol}.{inst.exchange}: {e}")
        if o.committee_decision is None:
            o.committee_decision = _committee_decision(plan)
        spec = PlanSpec(created_for=o.created_for,
                        valid_until=(plan.valid_until if plan.valid_until.tzinfo else plan.valid_until.replace(tzinfo=UTC)).date(),
                        entry=plan.entry_price, stop=plan.stop_loss, tp1=plan.take_profit_1, horizon_days=plan.horizon_days,
                        quantity=plan.quantity, fx_rate=plan.fx_rate or Decimal("1"))
        bars = _bars(db, inst.id, o.created_for)
        r = evaluate(spec, bars, today=today, commission_per_order=s.commission_per_order, slippage_bps=s.slippage_bps)
        before = (o.outcome, o.bars_evaluated)
        for k in ("outcome", "entry_date", "entry_price", "exit_date", "exit_price", "r_gross", "r_net", "mfe_r",
                  "mae_r", "bars_evaluated", "last_bar_date"):
            setattr(o, k, getattr(r, k))
        o.notes = list(dict.fromkeys((o.notes or []) + r.notes))
        src = db.execute(select(HistoricalBar.source, HistoricalBar.is_demo).where(HistoricalBar.instrument_id == inst.id)
                         .order_by(HistoricalBar.bar_date.desc()).limit(1)).first()
        if src:
            o.data_source, o.is_demo = src[0], bool(src[1]) or plan.is_demo
        o.updated_at = datetime.now(UTC)
        if o.outcome in FINAL and o.resolved_at is None:
            o.resolved_at = datetime.now(UTC)
        if (o.outcome, o.bars_evaluated) != before:
            changed += 1
    db.flush()
    return {"pending_checked": len(pending), "changed": changed, "instruments_synced": len(synced), "errors": errors}


# ---------------------------------------------------------------- metriche
def _block(rs: list[float], demo: bool) -> dict:
    n = len(rs)
    if n == 0:
        return {"trades": 0, "validated": False}
    wins = [r for r in rs if r > 0]
    lo, hi = M.wilson(len(wins), n)
    arr = np.array(rs, dtype=float)
    pos, neg = arr[arr > 0].sum(), -arr[arr <= 0].sum()
    out = {"trades": n, "win_rate": round(len(wins) / n, 4), "win_rate_ci95": [round(lo, 4), round(hi, 4)],
           "avg_r": round(float(arr.mean()), 3), "median_r": round(float(np.median(arr)), 3),
           "profit_factor_r": round(float(pos / neg), 3) if neg > 0 else None,
           "validated": n >= MIN_TRADES and not demo}
    if n >= 5:
        rng = np.random.default_rng(11)
        means = rng.choice(arr, size=(2000, n), replace=True).mean(axis=1)
        out["avg_r_ci95"] = [round(float(np.percentile(means, 2.5)), 3), round(float(np.percentile(means, 97.5)), 3)]
    return out


def _group(rows: list[PlanOutcome], key) -> dict:
    g: dict[str, list[PlanOutcome]] = {}
    for o in rows:
        g.setdefault(str(key(o)), []).append(o)
    return {k: _block([float(x.r_net) for x in v], any(x.is_demo for x in v)) for k, v in sorted(g.items())}


def _bucket(score) -> str:
    if score is None:
        return "n/d"
    s = float(score)
    for lo, hi in SCORE_BUCKETS:
        if lo <= s < hi:
            return f"{lo}-{min(hi, 100)}"
    return "n/d"


def backtest_reference(db: Session) -> dict | None:
    """Ultima calibrazione walk-forward: validata (dati reali) se esiste, altrimenti l'ultima disponibile."""
    rows = db.execute(select(ModelEvaluation).where(ModelEvaluation.evaluation_type == "WALK_FORWARD")
                      .order_by(ModelEvaluation.created_at.desc()).limit(20)).scalars().all()
    pick = next((r for r in rows if (r.metrics or {}).get("calibration", {}).get("overall", {}).get("validated")), None)
    pick = pick or (rows[0] if rows else None)
    if pick is None:
        return None
    cal = pick.metrics.get("calibration", {})
    ov = cal.get("overall", {})
    return {"model_evaluation_id": pick.id, "period": [str(pick.period_start), str(pick.period_end)],
            "trades": ov.get("trades", 0), "win_rate": ov.get("win_rate"), "win_rate_ci95": ov.get("win_rate_ci95"),
            "ev_r": ov.get("ev_r"), "validated": bool(ov.get("validated")), "is_demo": bool(cal.get("is_demo"))}


def drift(pro: dict, ref: dict | None) -> dict:
    if pro.get("trades", 0) < MIN_TRADES:
        return {"status": "INSUFFICIENT", "message": f"Servono almeno {MIN_TRADES} operazioni chiuse "
                                                     f"(ora {pro.get('trades', 0)}): nessuna conclusione possibile."}
    ci = pro.get("avg_r_ci95")
    if ci and ci[1] < 0:
        return {"status": "NEGATIVE_EDGE", "message": "Il risultato medio per operazione è negativo con confidenza "
                                                      "al 95%: il sistema non mostra un vantaggio."}
    if ref and ref.get("win_rate") is not None and ref.get("validated") and pro["win_rate_ci95"][1] < ref["win_rate"]:
        return {"status": "DEGRADED", "message": f"Win rate prospettico {pro['win_rate']:.0%} significativamente "
                                                 f"sotto quello del backtest {ref['win_rate']:.0%}."}
    if pro.get("validated"):
        return {"status": "CONSISTENT", "message": "Risultati prospettici compatibili con il backtest (non è una garanzia)."}
    return {"status": "INSUFFICIENT", "message": "Dati DEMO: la validazione non è applicabile."}


def summary(db: Session) -> dict:
    rows = db.execute(select(PlanOutcome)).scalars().all()
    counts: dict[str, int] = {}
    for o in rows:
        counts[o.outcome] = counts.get(o.outcome, 0) + 1
    closed = [o for o in rows if o.outcome in CLOSED and o.r_net is not None]
    demo = any(o.is_demo for o in closed)
    overall = _block([float(o.r_net) for o in closed], demo)
    ref = backtest_reference(db)
    triggered = sum(counts.get(k, 0) for k in CLOSED) + sum(1 for o in rows if o.outcome == "PENDING" and o.entry_date)
    decided = triggered + counts.get("NOT_TRIGGERED", 0) + counts.get("INVALIDATED", 0)
    mv = sorted({o.model_version for o in rows})
    return {
        "engine_version": OUTCOME_VERSION, "computed_at": datetime.now(UTC).isoformat(), "min_trades": MIN_TRADES,
        "plans_tracked": len(rows), "by_outcome": counts, "is_demo": demo or any(o.is_demo for o in rows),
        "trigger_rate": round(triggered / decided, 4) if decided else None,
        "overall": overall,
        "by_status_at_creation": _group(closed, lambda o: o.status_at_creation),
        "by_setup": _group(closed, lambda o: o.setup or "n/d"),
        "by_score": _group(closed, lambda o: _bucket(o.score)),
        "by_committee": _group(closed, lambda o: o.committee_decision or "n/d"),
        "model_versions": mv,
        "backtest_reference": ref,
        "drift": drift(overall, ref),
        "paper_trading": paper_stats(db),
        "notes": ["Esiti valutati solo con barre successive alla creazione del piano (fuori campione).",
                  "R netto stimato con i costi correnti; stessa barra con stop e TP1: assunto lo stop.",
                  "Statistiche non validate con meno di 30 operazioni chiuse o con dati DEMO."],
    }


def paper_stats(db: Session) -> dict:
    closed = db.execute(select(Position).where(Position.status == "CLOSED")).scalars().all()
    pnl = [float(p.realized_pnl_base) for p in closed]
    wins = [x for x in pnl if x > 0]
    return {"closed_positions": len(pnl), "wins": len(wins),
            "win_rate": round(len(wins) / len(pnl), 4) if pnl else None,
            "realized_pnl_base": round(sum(pnl), 2), "is_demo": any(p.is_demo for p in closed)}


def validation_gate(db: Session) -> dict | None:
    """Stato dell'ultima valutazione prospettica, usato dal planner per sospendere probabilità ed EV."""
    ev = db.execute(select(ModelEvaluation).where(ModelEvaluation.evaluation_type == "PROSPECTIVE")
                    .order_by(ModelEvaluation.created_at.desc()).limit(1)).scalar_one_or_none()
    if ev is None:
        return None
    d = (ev.metrics or {}).get("drift") or {}
    if d.get("status") in ("DEGRADED", "NEGATIVE_EDGE") and not (ev.metrics or {}).get("is_demo"):
        return {"status": d["status"], "message": d.get("message"), "model_evaluation_id": ev.id}
    return None


# ---------------------------------------------------------------- benchmark
def benchmark_comparison(db: Session, p: Portfolio) -> dict:
    snaps = db.execute(select(EquitySnapshot).where(EquitySnapshot.portfolio_id == p.id)
                       .order_by(EquitySnapshot.snap_date)).scalars().all()
    out = {"portfolio": [[str(s.snap_date), float(s.equity)] for s in snaps], "benchmarks": [], "notes": []}
    if len(snaps) < 2:
        out["notes"].append("Servono almeno due snapshot giornalieri dell'equity: il confronto non è ancora disponibile.")
        return out
    start, end = snaps[0].snap_date, snaps[-1].snap_date
    base = float(snaps[0].equity)
    pr = float(snaps[-1].equity) / base - 1
    out["portfolio_return_pct"] = round(pr * 100, 2)
    out["period"] = [str(start), str(end)]
    for tk in get_settings().regime_benchmarks:
        inst = find_instrument(db, tk)
        if inst is None:
            out["notes"].append(f"{tk}: nessun dato (sincronizza i benchmark da Mercati e dati).")
            continue
        bars = db.execute(select(HistoricalBar).where(HistoricalBar.instrument_id == inst.id,
                                                      HistoricalBar.bar_date <= end,
                                                      HistoricalBar.bar_date >= start - timedelta(days=7))
                          .order_by(HistoricalBar.bar_date)).scalars().all()
        first = next((b for b in reversed(bars) if b.bar_date <= start), bars[0] if bars else None)
        if first is None or len(bars) < 2:
            out["notes"].append(f"{tk}: barre insufficienti nel periodo.")
            continue
        b0 = float(first.adj_close or first.close)
        series = [[str(b.bar_date), round(base * float(b.adj_close or b.close) / b0, 2)] for b in bars if b.bar_date >= start]
        br = float(bars[-1].adj_close or bars[-1].close) / b0 - 1
        out["benchmarks"].append({"ticker": tk, "currency": inst.currency, "return_pct": round(br * 100, 2),
                                  "excess_pct": round((pr - br) * 100, 2), "series": series,
                                  "is_demo": any(b.is_demo for b in bars), "source": bars[-1].source})
    out["notes"].append("Benchmark in valuta locale, prezzi rettificati per i dividendi quando disponibili; "
                        "l'equity del portafoglio è in valuta base. Periodi brevi non sono significativi.")
    return out


# ---------------------------------------------------------------- affidabilità dati e segnali
def data_health(db: Session, days: int = 60) -> dict:
    s = get_settings()
    today = _today()
    since = today - timedelta(days=days)
    recent_ids = set(db.execute(select(TradePlan.instrument_id).where(TradePlan.created_at >= datetime.now(UTC) - timedelta(days=30))).scalars())
    recent_ids |= set(db.execute(select(Position.instrument_id).where(Position.status == "OPEN")).scalars())
    for tk in s.regime_benchmarks:
        inst = find_instrument(db, tk)
        if inst:
            recent_ids.add(inst.id)
    rows = []
    expected = int(np.busday_count(since + timedelta(days=1), today + timedelta(days=1)))
    for inst in db.execute(select(Instrument).where(Instrument.id.in_(recent_ids)).order_by(Instrument.symbol)).scalars():
        last_bar, n = db.execute(select(func.max(HistoricalBar.bar_date), func.count(HistoricalBar.id))
                                 .where(HistoricalBar.instrument_id == inst.id, HistoricalBar.bar_date > since)).one()
        q = db.execute(select(MarketQuote).where(MarketQuote.instrument_id == inst.id)
                       .order_by(MarketQuote.quote_time.desc()).limit(1)).scalar_one_or_none()
        anomalies = db.execute(select(func.count(Alert.id)).where(Alert.instrument_id == inst.id,
                                                                  Alert.kind == AlertKind.DATA_ANOMALY,
                                                                  Alert.created_at >= datetime.now(UTC) - timedelta(days=7))).scalar_one()
        issues = []
        lag = int(np.busday_count(last_bar, today)) if last_bar else None
        if last_bar is None:
            issues.append("nessuna barra recente")
        elif lag > 3:
            issues.append(f"ultima barra {lag} giorni lavorativi fa")
        missing = max(0, expected - n - (lag or 0)) if last_bar else expected
        if last_bar and missing > max(3, expected * 0.08):
            issues.append(f"{missing} giorni lavorativi senza barra (incluse festività di borsa)")
        q_age = None
        if q is not None:
            qt = q.quote_time if q.quote_time.tzinfo else q.quote_time.replace(tzinfo=UTC)
            q_age = int((datetime.now(UTC) - qt).total_seconds() // 60)
            close = db.execute(select(HistoricalBar.close).where(HistoricalBar.instrument_id == inst.id)
                               .order_by(HistoricalBar.bar_date.desc()).limit(1)).scalar()
            if close and abs(float(q.price) / float(close) - 1) > 0.2:
                issues.append(f"quotazione {q.price} distante oltre il 20% dall'ultima chiusura {close}: possibile dato errato")
        if anomalies:
            issues.append(f"{anomalies} anomalie dati negli ultimi 7 giorni")
        rows.append({"instrument_id": inst.id, "ticker": f"{inst.symbol}.{inst.exchange}", "source": inst.data_source,
                     "last_bar": str(last_bar) if last_bar else None, "bars_in_window": n, "expected_bdays": expected,
                     "missing_bdays": missing, "last_quote_age_min": q_age, "anomalies_7d": anomalies,
                     "is_demo": inst.is_demo, "status": "BAD" if (last_bar is None or (lag or 0) > 5) else "WARN" if issues else "OK",
                     "issues": issues})
    by_status: dict[str, int] = {}
    for r in rows:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    return {"window_days": days, "instruments": len(rows), "by_status": by_status, "items": rows,
            "notes": ["Copertura attesa calcolata sui giorni lavorativi: le festività di borsa risultano come mancanti.",
                      "Provider gratuito non ufficiale: interruzioni e correzioni dei dati sono possibili."]}


def signal_stability(db: Session, last: int = 10) -> dict:
    scans = db.execute(select(ScanRun).where(ScanRun.status == "DONE").order_by(ScanRun.id.desc()).limit(last)).scalars().all()
    scans = list(reversed(scans))
    items, prev = [], None
    for sr in scans:
        st = sr.stats or {}
        top = set(st.get("top") or [])
        overlap = round(len(top & prev) / len(top | prev), 3) if prev is not None and (top or prev) else None
        uni = st.get("universe") or 0
        errs = sr.errors or []
        core = {e.get("ticker") for e in errs if e.get("ticker") and e.get("stage") not in OPTIONAL_STAGES}
        opt = {e.get("ticker") for e in errs if e.get("ticker") and e.get("stage") in OPTIONAL_STAGES}
        items.append({"scan_id": sr.id, "as_of": str(sr.as_of) if sr.as_of else None,
                      "universe": uni, "analysed": st.get("analysed"), "eligible": st.get("eligible"),
                      "errors": len(errs), "error_rate": round(len(core) / uni, 4) if uni else None,
                      "optional_missing_rate": round(len(opt) / uni, 4) if uni else None,
                      "top_overlap_prev": overlap})
        prev = top
    ov = [i["top_overlap_prev"] for i in items if i["top_overlap_prev"] is not None]
    er = [i["error_rate"] for i in items if i["error_rate"] is not None]
    om = [i["optional_missing_rate"] for i in items if i["optional_missing_rate"] is not None]
    return {"scans": items, "avg_top_overlap": round(sum(ov) / len(ov), 3) if ov else None,
            "avg_error_rate": round(sum(er) / len(er), 4) if er else None,
            "avg_optional_missing_rate": round(sum(om) / len(om), 4) if om else None,
            "notes": ["Tasso di errore: quota di titoli con errori su prezzi o storico. Dati facoltativi: quota di titoli "
                      "senza fondamentali o calendario eventi (il punteggio usa solo i fattori disponibili).",
                      "Sovrapposizione dei primi titoli tra scansioni consecutive (indice di Jaccard): valori molto "
                      "bassi indicano segnali instabili o dati rumorosi."]}


# ---------------------------------------------------------------- ciclo completo
def _strategy_version(db: Session, model_version: str) -> StrategyVersion:
    st = db.execute(select(Strategy).where(Strategy.name == "Pipeline operativa")).scalar_one_or_none()
    if st is None:
        st = Strategy(name="Pipeline operativa", style="MULTI",
                      description="Scanner + Trade Planner + Risk Engine in uso, valutati in modo prospettico (Fase 7)")
        db.add(st)
        db.flush()
    code = hashlib.sha256(model_version.encode()).hexdigest()
    sv = db.execute(select(StrategyVersion).where(StrategyVersion.strategy_id == st.id,
                                                  StrategyVersion.code_hash == code)).scalar_one_or_none()
    if sv is None:
        sv = StrategyVersion(strategy_id=st.id, version=f"live#{code[:8]}", parameters={"model_version": model_version},
                             code_hash=code, is_validated=False)
        db.add(sv)
        db.flush()
    return sv


def run_validation(db: Session, adapter: MarketDataAdapter | None, actor: str = "worker") -> dict:
    added = backfill(db)
    upd = update_outcomes(db, adapter)
    summ = summary(db)
    summ["update"] = upd | {"backfilled": added}
    mv = summ["model_versions"][-1] if summ["model_versions"] else "n/d"
    sv = _strategy_version(db, mv)
    today = _today()
    first = db.execute(select(func.min(PlanOutcome.created_for))).scalar() or today
    ev = db.execute(select(ModelEvaluation).where(ModelEvaluation.evaluation_type == "PROSPECTIVE",
                                                  ModelEvaluation.period_end == today)).scalar_one_or_none()
    if ev is None:
        ev = ModelEvaluation(strategy_version_id=sv.id, evaluation_type="PROSPECTIVE", period_start=first,
                             period_end=today, is_out_of_sample=True, metrics={})
        db.add(ev)
    ev.strategy_version_id, ev.period_start, ev.metrics = sv.id, first, summ
    db.flush()
    sv.is_validated = summ["drift"]["status"] == "CONSISTENT"
    d = summ["drift"]
    if d["status"] in ("DEGRADED", "NEGATIVE_EDGE") and not summ["is_demo"]:
        raise_alert(db, AlertKind.SYSTEM, Severity.CRITICAL,
                    f"Validazione prospettica: {d['message']} Probabilità ed EV calibrati sospesi nei nuovi piani.",
                    data={"model_evaluation_id": ev.id, "status": d["status"]}, dedup_key=f"drift:{d['status']}:{today}")
    health = data_health(db)
    bad = [r["ticker"] for r in health["items"] if r["status"] == "BAD"]
    if bad:
        raise_alert(db, AlertKind.DATA_ANOMALY, Severity.WARNING,
                    f"Affidabilità dati: {len(bad)} strumenti senza barre recenti ({', '.join(bad[:5])}"
                    f"{'…' if len(bad) > 5 else ''}).", data={"tickers": bad}, dedup_key=f"health:{today}")
    audit.record(db, actor=actor, action="validation.run", entity="model_evaluation", entity_id=ev.id,
                 payload={"plans_tracked": summ["plans_tracked"], "by_outcome": summ["by_outcome"],
                          "drift": d["status"], "update": summ["update"]})
    return {"model_evaluation_id": ev.id, **summ}


def enqueue_validation(db: Session, actor: str, key: str | None = None) -> tuple[Job, bool]:
    return jobs.enqueue(db, "validation", {}, actor, idempotency_key=key)


@jobs.handler("validation")
def _validation_job(db: Session, job: Job) -> dict:
    from app.adapters.market_data.registry import get_market_data_adapter
    res = run_validation(db, get_market_data_adapter(), actor=job.requested_by)
    return {"model_evaluation_id": res["model_evaluation_id"], "plans_tracked": res["plans_tracked"],
            "drift": res["drift"]["status"], "update": res["update"]}
