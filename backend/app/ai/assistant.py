"""AI Trading Assistant (specifica §10).

Il modello risponde SOLO attraverso strumenti tipizzati in sola lettura eseguiti dal backend sui dati
dell'utente: ogni risposta restituisce le fonti (chiamate agli strumenti con input e output) che la
giustificano. Nessuno strumento può inviare ordini. Senza chiave API un instradamento deterministico
riconosce le domande tipiche e risponde con gli stessi strumenti.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.ai.llm import LLM, LLMError
from app.core.config import get_settings
from app.models.entities import (
    AiRun,
    Execution,
    Instrument,
    Order,
    Position,
    ScanRun,
    TradePlan,
    TradingSignal,
    User,
)
from app.services.portfolio import (
    get_or_create_paper_portfolio,
    risk_param,
    value_portfolio,
)
from app.services.risk import (
    Costs,
    PortfolioContext,
    RiskLimits,
    SizingRequest,
    compute_position_size,
)

PROMPT_VERSION = "assistant-prompts/1.0.0"
SYSTEM = """Sei l'AI Trading Assistant di ALPHA TRADER AI, piattaforma di analisi e paper trading (simulato) su azioni ed ETF USA ed europei.

Regole vincolanti:
- Ogni numero, prezzo, livello, quantità o risultato che citi deve provenire dall'output di uno strumento chiamato in questa conversazione. Se un dato non è disponibile, dillo.
- Non inventare probabilità: la probabilità è disponibile solo se calibrata fuori campione (campo calibrated_probability non nullo).
- Non presentare mai rendimenti come garantiti. La perdita allo stop è una stima: con gap può essere maggiore.
- Non puoi inviare ordini. Un piano READY si esegue solo in simulazione dalla pagina Trade Detail, con conferma umana.
- Cita i piani come "piano #ID" e i titoli con il ticker, così l'utente può verificarli.
- Se i dati sono DEMO (sintetici) dichiaralo.
- Rispondi in italiano, in modo conciso e ordinato (elenchi brevi). Non sei un consulente finanziario: le informazioni sono di supporto alla decisione."""


def _f(x):
    return None if x is None else float(x)


def _iso(x):
    return x.isoformat() if x else None


def _plan_brief(p: TradePlan) -> dict:
    c = p.committee or {}
    dec = (c.get("decision") or {}) if c.get("status") == "DONE" else ((c.get("previous") or {}).get("decision") or {})
    return {"plan_id": p.id, "ticker": f"{p.instrument.symbol}.{p.instrument.exchange}", "name": p.instrument.name,
            "status": p.status, "status_reason": p.status_reason, "setup": p.setup, "score": _f(p.score),
            "risk_level": p.risk_level, "entry": _f(p.entry_price), "entry_range": [_f(p.entry_low), _f(p.entry_high)],
            "stop": _f(p.stop_loss), "tp1": _f(p.take_profit_1), "tp2": _f(p.take_profit_2), "quantity": _f(p.quantity),
            "currency": p.currency, "notional_eur": _f(p.notional_base), "est_loss_at_stop_eur": _f(p.est_loss_at_stop_base),
            "est_gain_tp1_eur": _f(p.est_gain_tp1_base), "reward_risk_net": _f(p.reward_risk_net),
            "calibrated_probability": _f(p.calibrated_probability), "horizon_days": p.horizon_days,
            "valid_until": _iso(p.valid_until), "price_reference": _f(p.price_reference), "price_source": p.price_source,
            "price_time": _iso(p.price_time), "is_demo": p.is_demo,
            "committee_decision": dec.get("decision"), "committee_mode": dec.get("mode")}


class Tools:
    """Strumenti in sola lettura, sempre filtrati sul portafoglio dell'utente."""

    def __init__(self, db: Session, user: User):
        self.db, self.user = db, user
        self.portfolio = get_or_create_paper_portfolio(db, user)

    # --- definizioni per l'API (strict: tutti i campi richiesti, null ammesso dove opzionale)
    @staticmethod
    def definitions() -> list[dict]:
        def tool(name, desc, props):
            return {"name": name, "description": desc, "strict": True,
                    "input_schema": {"type": "object", "additionalProperties": False,
                                     "properties": props, "required": list(props)}}
        return [
            tool("get_market_regime", "Regime di mercato dell'ultima scansione completata (benchmark, VIX, tassi, note).", {}),
            tool("list_opportunities", "Piani operativi del portafoglio ordinati per stato (READY prima) e punteggio.",
                 {"status": {"type": ["string", "null"], "enum": ["READY", "WAIT", "ACTIVE", "REJECTED", "EXPIRED", None],
                             "description": "ACTIVE = READY+WAIT; null = ACTIVE"},
                  "limit": {"type": ["integer", "null"], "description": "massimo 20; null = 5"}}),
            tool("get_trade_plan", "Dettaglio completo di un piano: livelli, size, scenari, invalidazione, valutazioni del comitato.",
                 {"plan_id": {"type": "integer"}}),
            tool("explain_selection", "Perché un titolo è stato selezionato: punteggi per fattore, motivazioni a favore e contro dell'ultima scansione.",
                 {"ticker": {"type": "string", "description": "es. AAPL.US, ENI.MI"}}),
            tool("check_budget", "Ricalcola con il motore di rischio deterministico quali piani attivi sono compatibili con un importo disponibile (in EUR) e i limiti di rischio.",
                 {"available_eur": {"type": "number"}}),
            tool("get_positions", "Posizioni aperte con P&L, distanza da stop e target e segnalazioni che richiedono attenzione.", {}),
            tool("get_performance", "Andamento del portafoglio simulato: equity, rendimento cumulato, drawdown, operazioni chiuse nel periodo.",
                 {"days": {"type": ["integer", "null"], "description": "null = 90"}}),
        ]

    def call(self, name: str, args: dict):
        fn = getattr(self, f"t_{name}", None)
        if fn is None:
            raise ValueError(f"Strumento sconosciuto: {name}")
        return fn(**args)

    # --- implementazioni
    def t_get_market_regime(self):
        sr = self.db.execute(select(ScanRun).where(ScanRun.status == "DONE").order_by(ScanRun.finished_at.desc()).limit(1)).scalar_one_or_none()
        if sr is None:
            return {"available": False, "note": "Nessuna scansione completata"}
        r = sr.regime or {}
        return {"available": True, "scan_id": sr.id, "as_of": _iso(sr.as_of), "regime": r.get("regime"), "score": r.get("score"),
                "benchmarks": [{k: b.get(k) for k in ("ticker", "trend", "above_ema200", "vol20_percentile_1y")} for b in r.get("benchmarks", [])],
                "vix": r.get("vix"), "rates": r.get("rates"), "notes": r.get("notes"), "data_source": sr.data_source}

    def _plans(self, status: str | None):
        stmt = select(TradePlan).where(TradePlan.portfolio_id == self.portfolio.id)
        if status in (None, "ACTIVE"):
            stmt = stmt.where(TradePlan.status.in_(("READY", "WAIT")))
        else:
            stmt = stmt.where(TradePlan.status == status)
        order = case((TradePlan.status == "READY", 0), (TradePlan.status == "WAIT", 1), else_=2)
        return stmt.order_by(order, TradePlan.score.desc().nulls_last(), TradePlan.created_at.desc())

    def t_list_opportunities(self, status=None, limit=None):
        lim = max(1, min(int(limit or 5), 20))
        rows = self.db.execute(self._plans(status).limit(lim)).scalars().all()
        return {"count": len(rows), "plans": [_plan_brief(p) for p in rows],
                "note": "Un punteggio elevato non dimostra che l'operazione sia profittevole."}

    def t_get_trade_plan(self, plan_id: int):
        p = self.db.get(TradePlan, int(plan_id))
        if p is None or p.portfolio_id != self.portfolio.id:
            return {"found": False}
        c = p.committee or {}
        com = c if c.get("status") == "DONE" else c.get("previous") or {}
        return _plan_brief(p) | {
            "found": True, "entry_conditions": p.entry_conditions, "avoid_conditions": p.avoid_conditions,
            "early_exit_conditions": p.early_exit_conditions, "invalidation_conditions": p.invalidation_conditions,
            "rationale_for": p.rationale_for, "rationale_against": p.rationale_against, "scenarios": p.scenarios,
            "stop_rationale": p.stop_rationale, "correlations": p.correlations, "trailing_stop": p.trailing_stop,
            "market_regime": p.market_regime, "model_version": p.model_version,
            "committee": {"mode": com.get("mode"), "decision": (com.get("decision") or {}).get("decision"),
                          "summary": (com.get("decision") or {}).get("summary"),
                          "could_be_wrong_if": (com.get("decision") or {}).get("could_be_wrong_if"),
                          "agents": [{k: a.get(k) for k in ("title", "stance", "summary")} for a in com.get("agents", [])]} if com else None,
        }

    def t_explain_selection(self, ticker: str):
        sym, _, exch = ticker.strip().upper().partition(".")
        stmt = select(TradingSignal).join(Instrument, TradingSignal.instrument_id == Instrument.id).where(Instrument.symbol == sym)
        if exch:
            stmt = stmt.where(Instrument.exchange == exch)
        sg = self.db.execute(stmt.order_by(TradingSignal.analysis_time.desc()).limit(1)).scalar_one_or_none()
        if sg is None:
            return {"found": False, "note": f"Nessun segnale per {ticker}"}
        return {"found": True, "signal_id": sg.id, "ticker": f"{sg.instrument.symbol}.{sg.instrument.exchange}",
                "analysis_time": _iso(sg.analysis_time), "eligible": sg.eligible, "score": _f(sg.score),
                "factor_scores": sg.factor_scores, "coverage": _f(sg.coverage), "setup": sg.setup, "market_regime": sg.market_regime,
                "rationale": sg.rationale, "exclusions": (sg.details or {}).get("exclusions"),
                "data_source": sg.data_source, "is_demo": sg.is_demo, "strategy_version_id": sg.strategy_version_id}

    def t_check_budget(self, available_eur: float):
        s = get_settings()
        val = value_portfolio(self.db, self.portfolio)
        cash = min(val.cash, Decimal(str(available_eur)))
        limits = RiskLimits(*(risk_param(self.portfolio, k) for k in (
            "risk_per_trade_pct", "max_aggregate_risk_pct", "max_position_exposure_pct",
            "max_sector_exposure_pct", "min_cash_reserve_pct")))
        costs = Costs(s.commission_per_order, s.commission_pct, s.slippage_bps)
        out = []
        for p in self.db.execute(self._plans("ACTIVE").limit(20)).scalars():
            if p.quantity <= 0 or p.stop_loss >= p.entry_price:
                continue
            ctx = PortfolioContext(val.equity, cash, val.aggregate_risk_base)
            req = SizingRequest(entry_price=p.entry_price, stop_price=p.stop_loss, fx_rate=p.fx_rate or Decimal("1"),
                                take_profit_1=p.take_profit_1, take_profit_2=p.take_profit_2,
                                tp1_close_fraction=p.tp1_close_fraction or Decimal("0.5"), max_quantity=p.quantity)
            r = compute_position_size(req, ctx, limits, costs)
            out.append({"plan_id": p.id, "ticker": f"{p.instrument.symbol}.{p.instrument.exchange}", "status": p.status,
                        "compatible": r.approved, "quantity": float(r.quantity), "notional_eur": float(r.notional_base),
                        "est_loss_at_stop_eur": float(r.est_loss_at_stop_base), "binding_constraint": r.binding_constraint,
                        "blocking_reasons": r.blocking_reasons, "is_demo": p.is_demo})
        return {"available_eur": float(available_eur), "cash_used_eur": float(cash), "portfolio_cash_eur": float(val.cash),
                "equity_eur": float(val.equity), "min_cash_reserve_pct": float(limits.min_cash_reserve_pct),
                "risk_per_trade_pct": float(limits.risk_per_trade_pct), "results": out,
                "note": "Calcolo del motore di rischio deterministico; la riserva minima di liquidità resta calcolata sull'equity."}

    def t_get_positions(self):
        val = value_portfolio(self.db, self.portfolio)
        rows = []
        for pv in val.positions:
            p = pv.position
            price = _f(pv.price)
            flags = []
            if pv.stale:
                flags.append("prezzo non aggiornato")
            if p.stop_loss is None:
                flags.append("stop loss assente")
            elif price is not None and price <= float(p.stop_loss) * 1.02:
                flags.append("prezzo entro il 2% dallo stop")
            if p.take_profit_1 is not None and price is not None and price >= float(p.take_profit_1) * 0.98:
                flags.append("prezzo vicino o oltre TP1")
            if p.trade_plan_id:
                tp = self.db.get(TradePlan, p.trade_plan_id)
                if tp and tp.status == "EXPIRED":
                    flags.append("piano di origine scaduto: rivalutare")
            rows.append({"ticker": f"{p.instrument.symbol}.{p.instrument.exchange}", "quantity": float(p.quantity),
                         "avg_price": float(p.avg_price), "price": price, "price_time": _iso(pv.price_time),
                         "stop": _f(p.stop_loss), "tp1": _f(p.take_profit_1), "market_value_eur": float(pv.market_value_base),
                         "unrealized_pnl_eur": float(pv.unrealized_pnl_base),
                         "distance_to_stop_pct": round((price / float(p.stop_loss) - 1) * 100, 2) if price and p.stop_loss else None,
                         "needs_attention": flags, "is_demo": pv.is_demo, "trade_plan_id": p.trade_plan_id})
        return {"count": len(rows), "positions": rows, "aggregate_risk_pct": float(val.aggregate_risk_pct),
                "warnings": val.warnings}

    def t_get_performance(self, days=None):
        d = max(1, min(int(days or 90), 3650))
        since = datetime.now(UTC) - timedelta(days=d)
        val = value_portfolio(self.db, self.portfolio)
        ex = self.db.execute(select(func.count(Execution.id), func.coalesce(func.sum(Execution.realized_pnl_base), 0))
                             .join(Order, Execution.order_id == Order.id)
                             .where(Order.portfolio_id == self.portfolio.id, Execution.executed_at >= since,
                                    Order.side == "SELL")).one()
        closed = self.db.execute(select(func.count(Position.id)).where(
            Position.portfolio_id == self.portfolio.id, Position.status == "CLOSED", Position.closed_at >= since)).scalar_one()
        return {"period_days": d, "equity_eur": float(val.equity), "initial_capital_eur": float(self.portfolio.initial_capital),
                "cumulative_return_pct": float(val.cumulative_return_pct), "drawdown_pct": float(val.drawdown_pct),
                "realized_pnl_total_eur": float(val.realized_pnl), "unrealized_pnl_eur": float(val.unrealized_pnl),
                "sell_executions_in_period": int(ex[0]), "realized_pnl_in_period_eur": float(ex[1]),
                "positions_closed_in_period": int(closed),
                "note": "Portafoglio simulato (paper). I risultati passati non garantiscono risultati futuri."}


# ------------------------------------------------------------------ modalità deterministica
_AMOUNT = re.compile(r"(\d{1,3}(?:[.\s]\d{3})+|\d+)(?:[,.]\d+)?\s*(?:€|eur|euro)|€\s*(\d{1,3}(?:[.\s]\d{3})+|\d+)", re.I)
_TICKER = re.compile(r"\b([A-Z][A-Z0-9]{0,9}\.[A-Z]{1,6})\b")
_PLAN = re.compile(r"(?:piano|plan|#)\s*#?(\d+)", re.I)


def _eur(x) -> str:
    return "n/d" if x is None else f"{x:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")


def deterministic_answer(tools: Tools, question: str) -> tuple[str, list[dict]]:
    q = question.lower()
    calls: list[dict] = []

    def call(name, **args):
        out = tools.call(name, args)
        calls.append({"tool": name, "input": args, "output": out})
        return out

    m_amt, m_tk, m_plan = _AMOUNT.search(question), _TICKER.search(question), _PLAN.search(question)
    if m_amt and any(w in q for w in ("dispon", "compatib", "limiti", "budget", "investire", "con ")):
        raw = (m_amt.group(1) or m_amt.group(2)).replace(".", "").replace(" ", "")
        r = call("check_budget", available_eur=float(raw))
        ok = [x for x in r["results"] if x["compatible"]]
        lines = [f"Con {_eur(r['available_eur'])} disponibili (liquidità del portafoglio {_eur(r['portfolio_cash_eur'])}):"]
        lines += [f"- {x['ticker']} (piano #{x['plan_id']}, {x['status']}): {x['quantity']:g} pezzi, "
                  f"{_eur(x['notional_eur'])}, perdita stimata allo stop {_eur(x['est_loss_at_stop_eur'])}" for x in ok] or \
                 ["- Nessun piano attivo è compatibile con questo importo e con i limiti di rischio."]
        lines += [f"- {x['ticker']} non compatibile: {'; '.join(x['blocking_reasons']) or x['binding_constraint']}"
                  for x in r["results"] if not x["compatible"]][:5]
        return "\n".join(lines), calls
    if any(w in q for w in ("perché", "perche", "selezionat", "motiv")) and (m_tk or m_plan):
        if m_plan and not m_tk:
            p = call("get_trade_plan", plan_id=int(m_plan.group(1)))
            if not p.get("found"):
                return f"Piano #{m_plan.group(1)} non trovato nel tuo portafoglio.", calls
            tk = p["ticker"]
        else:
            tk = m_tk.group(1)
        s = call("explain_selection", ticker=tk)
        if not s.get("found"):
            return f"Nessun segnale registrato per {tk}.", calls
        fs = ", ".join(f"{k} {v}" for k, v in (s["factor_scores"] or {}).items() if v is not None)
        r = s["rationale"] or {}
        return "\n".join([f"{s['ticker']}: punteggio {s['score']} (copertura dati {s['coverage']}), setup {s['setup'] or 'nessuno'}, "
                          f"regime {s['market_regime']}.", f"Fattori: {fs}.",
                          *[f"- A favore: {x}" for x in r.get("for", [])[:4]], *[f"- Contro: {x}" for x in r.get("against", [])[:4]],
                          "Un punteggio elevato non dimostra che l'operazione sia profittevole."]), calls
    if "invalid" in q or "sbagliat" in q or "errat" in q:
        if m_plan:
            pid = int(m_plan.group(1))
        else:
            top = call("list_opportunities", status="ACTIVE", limit=1)["plans"]
            if not top:
                return "Non ci sono piani attivi da analizzare.", calls
            pid = top[0]["plan_id"]
        p = call("get_trade_plan", plan_id=pid)
        if not p.get("found"):
            return f"Piano #{pid} non trovato.", calls
        cw = (p.get("committee") or {}).get("could_be_wrong_if") or []
        return "\n".join([f"Piano #{pid} ({p['ticker']}) è invalidato se:", *[f"- {x}" for x in p["invalidation_conditions"]],
                          *[f"- Uscita anticipata: {x}" for x in p["early_exit_conditions"][:3]],
                          *[f"- Il comitato potrebbe sbagliare se: {x}" for x in cw[:2]]]), calls
    if "posizion" in q or "attenzione" in q:
        r = call("get_positions")
        if not r["count"]:
            return "Non ci sono posizioni aperte nel portafoglio simulato.", calls
        att = [x for x in r["positions"] if x["needs_attention"]]
        lines = [f"{r['count']} posizioni aperte, rischio aggregato {r['aggregate_risk_pct']}% del capitale."]
        lines += [f"- {x['ticker']}: {', '.join(x['needs_attention'])} (P&L {_eur(x['unrealized_pnl_eur'])})" for x in att] or \
                 ["- Nessuna posizione richiede attenzione secondo i controlli automatici."]
        return "\n".join(lines), calls
    if "perform" in q or "andat" in q or "rendiment" in q or "risultat" in q:
        dm = re.search(r"(\d+)\s*giorni", q)
        r = call("get_performance", days=int(dm.group(1)) if dm else 90)
        return (f"Ultimi {r['period_days']} giorni (portafoglio simulato): equity {_eur(r['equity_eur'])}, rendimento cumulato "
                f"{r['cumulative_return_pct']}%, drawdown {r['drawdown_pct']}%, P&L realizzato nel periodo "
                f"{_eur(r['realized_pnl_in_period_eur'])} su {r['positions_closed_in_period']} posizioni chiuse. "
                "I risultati passati non garantiscono risultati futuri."), calls
    if "regime" in q or "mercato" in q:
        r = call("get_market_regime")
        if not r["available"]:
            return "Nessuna scansione completata: regime non disponibile.", calls
        return f"Regime {r['regime']} (punteggio {r['score']}, scansione #{r['scan_id']} del {r['as_of']}). " + \
            " ".join(r.get("notes") or []), calls
    if any(w in q for w in ("miglior", "opportunit", "top", "oggi")):
        n = re.search(r"\b(\d+|tre|cinque|due)\b", q)
        lim = {"due": 2, "tre": 3, "cinque": 5}.get(n.group(1), None) if n else None
        lim = lim or (int(n.group(1)) if n and n.group(1).isdigit() else 3)
        r = call("list_opportunities", status="ACTIVE", limit=lim)
        if not r["count"]:
            return "Nessun piano READY o WAIT al momento: lancia una scansione o attendi la prossima.", calls
        lines = [f"Le {r['count']} migliori opportunità attive (ordinate per stato e punteggio):"]
        for p in r["plans"]:
            lines.append(f"- piano #{p['plan_id']} {p['ticker']} · {p['status']} · punteggio {p['score']} · ingresso {p['entry']} "
                         f"stop {p['stop']} TP1 {p['tp1']} · R/R netto {p['reward_risk_net']}"
                         + (f" · comitato: {p['committee_decision']}" if p["committee_decision"] else "")
                         + (" · DEMO" if p["is_demo"] else ""))
        lines.append(r["note"])
        return "\n".join(lines), calls
    return ("In modalità deterministica riconosco queste richieste:\n- Quali sono oggi le tre migliori opportunità?\n"
            "- Con 2.000 € disponibili, quale operazione è compatibile con i miei limiti di rischio?\n"
            "- Perché hai selezionato AAPL.US? (oppure: piano #12)\n- Cosa potrebbe invalidare il piano #12?\n"
            "- Quali posizioni richiedono attenzione?\n- Come ha performato il sistema negli ultimi 90 giorni?\n"
            "Per domande libere serve la chiave ANTHROPIC_API_KEY nel file .env."), calls


# ------------------------------------------------------------------ entry point
def answer(db: Session, user: User, messages: list[dict], llm: LLM | None) -> dict:
    tools = Tools(db, user)
    question = messages[-1]["content"]
    started = time.monotonic()
    mode, model, meta, error, status = "deterministic", None, {}, None, "OK"
    if llm is not None:
        try:
            r = llm.run_tools(system=SYSTEM, messages=messages, tools=Tools.definitions(), handler=tools.call,
                              max_steps=get_settings().ai_assistant_max_steps)
            text, calls, meta, mode, model = r.data, r.meta.get("tool_calls", []), r.meta, "ai", r.meta.get("model") or llm.model
            if not text:
                raise LLMError("Risposta vuota", meta=r.meta)
        except LLMError as e:
            error, status = str(e), "FALLBACK" if e.status == "ERROR" else e.status
            text, calls = deterministic_answer(tools, question)
            mode, meta = "deterministic", e.meta
            text = f"(Modello AI non disponibile: {e}. Risposta deterministica.)\n\n{text}"
    else:
        text, calls = deterministic_answer(tools, question)
    run = AiRun(kind="assistant", agent="assistant", user_id=user.id, mode=mode, model=model, prompt_version=PROMPT_VERSION,
                prompt_hash=hashlib.sha256(SYSTEM.encode()).hexdigest(),
                input_hash=hashlib.sha256(json.dumps(messages, ensure_ascii=False).encode()).hexdigest(),
                inputs={"messages": messages[-10:]}, output={"answer": text},
                validation={"tool_calls": json.loads(json.dumps(calls, default=str))},
                usage=meta.get("usage") or {}, request_id=meta.get("request_id"), status=status, error=error,
                latency_ms=int((time.monotonic() - started) * 1000))
    db.add(run)
    db.flush()
    return {"answer": text, "mode": mode, "model": model, "run_id": run.id,
            "sources": [{"tool": c["tool"], "input": c["input"], "output": c.get("output"), "error": c.get("error")}
                        for c in json.loads(json.dumps(calls, default=str))]}

