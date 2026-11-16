"""AI Investment Committee: 7 agenti separati (specifica §4).

Principi:
- Ogni agente riceve SOLO il proprio fascicolo di dati verificati (prezzi, indicatori, livelli e
  dimensionamento calcolati dai motori deterministici). L'LLM sintetizza e ragiona; non calcola e non
  inventa: ogni numero nel testo viene confrontato con il fascicolo e, se assente, segnalato.
- Output JSON con schema imposto dall'API e rivalidato con Pydantic.
- Le decisioni ammesse dall'Agent 7 sono calcolate in modo deterministico PRIMA della chiamata
  (veto del Risk Manager => solo REJECT; piano WAIT => mai BUY; posizione aperta => HOLD/REDUCE/EXIT).
- Senza chiave API (o se una chiamata fallisce) ogni agente produce una valutazione deterministica
  con le stesse regole: la modalità è sempre dichiarata nell'output.
- Ogni chiamata è registrata in ai_runs (modello, versione e hash del prompt, hash dei dati, output, token).
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.llm import LLM, LLMError
from app.models.entities import (
    AiRun,
    Position,
    RiskAssessment,
    ScanRun,
    TradePlan,
    TradingSignal,
)
from app.services import audit

COMMITTEE_VERSION = "committee/1.0.0"
PROMPT_VERSION = "committee-prompts/1.0.0"
Stance = Literal["FAVOREVOLE", "NEUTRALE", "CONTRARIO"]
DECISIONS = ("BUY", "WAIT", "HOLD", "REDUCE", "EXIT", "REJECT")

AGENTS = [
    ("market_analyst", "Agent 1 · Market Analyst", "Identifica il regime di mercato (rialzista, ribassista, laterale, alta volatilità) e il suo impatto sull'operazione."),
    ("stock_selector", "Agent 2 · Stock Selector", "Valuta se il titolo soddisfa i criteri quantitativi del ranking e quanto è solido il punteggio."),
    ("technical_analyst", "Agent 3 · Technical Analyst", "Valuta ingresso, invalidazione del setup, supporti, resistenze e obiettivi del piano."),
    ("fundamental_analyst", "Agent 4 · Fundamental Analyst", "Esamina solidità economica, valutazione ed eventi societari (utili imminenti)."),
    ("risk_manager", "Agent 5 · Risk Manager", "Valuta dimensione, perdita stimata allo stop, scenario di gap, esposizione e rischio aggregato. Ha potere di veto."),
    ("portfolio_manager", "Agent 6 · Portfolio Manager", "Valuta l'operazione nel contesto delle posizioni aperte: correlazioni, concentrazioni, rischi duplicati."),
]
COMMITTEE = ("investment_committee", "Agent 7 · Investment Committee",
             "Confronta le valutazioni degli altri agenti e produce la decisione finale.")


# ------------------------------------------------------------------ schemi di output
class AgentOpinion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stance: Stance
    summary: str
    points_for: list[str]
    points_against: list[str]
    risks: list[str]
    data_refs: list[str]


class CommitteeDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["BUY", "WAIT", "HOLD", "REDUCE", "EXIT", "REJECT"]
    summary: str
    reasons_for: list[str]
    reasons_against: list[str]
    could_be_wrong_if: list[str]
    conditions_to_act: list[str]
    dissent: list[str]
    data_refs: list[str]


_STR_LIST = {"type": "array", "items": {"type": "string"}}


def opinion_schema() -> dict:
    return {"type": "object", "additionalProperties": False,
            "required": ["stance", "summary", "points_for", "points_against", "risks", "data_refs"],
            "properties": {"stance": {"type": "string", "enum": ["FAVOREVOLE", "NEUTRALE", "CONTRARIO"]},
                           "summary": {"type": "string"}, "points_for": _STR_LIST, "points_against": _STR_LIST,
                           "risks": _STR_LIST, "data_refs": _STR_LIST}}


def decision_schema(allowed: list[str]) -> dict:
    keys = ["decision", "summary", "reasons_for", "reasons_against", "could_be_wrong_if", "conditions_to_act",
            "dissent", "data_refs"]
    props = {k: _STR_LIST for k in keys} | {"decision": {"type": "string", "enum": allowed}, "summary": {"type": "string"}}
    return {"type": "object", "additionalProperties": False, "required": keys, "properties": props}


# ------------------------------------------------------------------ prompt
BASE_RULES = """Sei {title} del comitato di investimento di ALPHA TRADER AI, piattaforma di analisi e paper trading.
Compito: {task}

Regole vincolanti:
- Usa SOLO i dati del fascicolo fornito. Non introdurre prezzi, percentuali, probabilità o eventi che non compaiono nel fascicolo.
- Non eseguire calcoli finanziari nuovi: livelli, quantità, perdite e rendimenti sono già calcolati dai motori deterministici.
- Se un dato è null o assente, dillo esplicitamente ("dato non disponibile") invece di stimarlo.
- Non presentare mai rendimenti come garantiti e non attribuire probabilità: la probabilità è disponibile solo se calibrata fuori campione.
- In data_refs elenca le chiavi del fascicolo su cui basi il giudizio (esattamente come scritte).
- Scrivi in italiano, frasi brevi e concrete. Massimo 4 punti per elenco."""

COMMITTEE_RULES = """Sei {title} di ALPHA TRADER AI. {task}

Regole vincolanti:
- Decidi SOLO tra le decisioni ammesse elencate nel fascicolo: sono state calcolate in modo deterministico
  (il veto del Risk Manager è definitivo; un piano non READY non può diventare BUY).
- BUY significa solo "proposta idonea all'esecuzione simulata con conferma umana": non è un ordine.
- Riporta le ragioni favorevoli, contrarie e le condizioni che renderebbero errata la decisione.
- In dissent riporta le opinioni degli agenti in disaccordo con la decisione.
- Usa solo dati presenti nel fascicolo e nelle valutazioni degli agenti; non inventare numeri né probabilità.
- In data_refs elenca le chiavi del fascicolo usate. Italiano, frasi brevi, massimo 4 punti per elenco."""


def _sha(x) -> str:
    return hashlib.sha256(json.dumps(x, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


# ------------------------------------------------------------------ fascicoli (dati verificati)
def _n(x):
    if x is None:
        return None
    if isinstance(x, Decimal):
        x = float(x)
    if isinstance(x, float):
        return None if math.isnan(x) or math.isinf(x) else round(x, 4)
    return x


def build_dossiers(db: Session, plan: TradePlan) -> dict[str, dict]:
    inst = plan.instrument
    sig = db.get(TradingSignal, plan.signal_id) if plan.signal_id else None
    scan = db.get(ScanRun, sig.scan_run_id) if sig and sig.scan_run_id else None
    ra = db.get(RiskAssessment, plan.risk_assessment_id) if plan.risk_assessment_id else None
    det = (sig.details or {}) if sig else {}
    feats = det.get("features", {})
    fund = det.get("fundamentals_used") or {}
    rres = (ra.result if ra else {}) or {}
    rin = (ra.inputs if ra else {}) or {}
    reg = (scan.regime if scan else None) or {}
    positions = db.execute(select(Position).where(Position.portfolio_id == plan.portfolio_id,
                                                  Position.status == "OPEN")).scalars().all() if plan.portfolio_id else []
    common = {
        "titolo.ticker": f"{inst.symbol}.{inst.exchange}", "titolo.nome": inst.name, "titolo.tipo": inst.instrument_type,
        "titolo.settore": inst.sector, "titolo.valuta": inst.currency, "piano.id": plan.id, "piano.stato": plan.status,
        "piano.motivo_stato": plan.status_reason, "piano.setup": plan.setup, "piano.stile": plan.strategy_style,
        "piano.orizzonte_giorni": plan.horizon_days, "prezzo.riferimento": _n(plan.price_reference),
        "prezzo.fonte": plan.price_source, "prezzo.istante": plan.price_time.isoformat() if plan.price_time else None,
        "dati.demo": plan.is_demo,
    }
    fs = (sig.factor_scores if sig else {}) or {}
    market = common | {
        "regime.etichetta": plan.market_regime or reg.get("regime"), "regime.punteggio": _n(reg.get("score")),
        "regime.note": reg.get("notes"), "regime.vix": reg.get("vix"), "regime.tassi": reg.get("rates"),
        "regime.benchmark": [{k: b.get(k) for k in ("ticker", "trend", "above_ema200", "ema50_slope_10d", "vol20_percentile_1y")}
                             for b in reg.get("benchmarks", [])],
        "fattore.macro_settore": fs.get("macro_sector"),
    }
    selector = common | {
        "ranking.punteggio": _n(plan.score), "ranking.copertura_dati": _n(sig.coverage) if sig else None,
        "ranking.livello_rischio": plan.risk_level, "ranking.fattori": fs,
        "ranking.a_favore": plan.rationale_for, "ranking.contro": plan.rationale_against,
        "ranking.esclusioni": det.get("exclusions"), "ranking.flag": det.get("flags"),
        "ranking.potrebbe_essere_errato_se": (sig.rationale or {}).get("could_be_wrong_if") if sig else None,
        "liquidita.spread_pct": _n(det.get("spread_pct")), "liquidita.controvalore_20g": _n(feats.get("traded_value20")),
    }
    sr = plan.stop_rationale or {}
    technical = common | {
        "piano.ingresso": _n(plan.entry_price), "piano.ingresso_min": _n(plan.entry_low), "piano.ingresso_max": _n(plan.entry_high),
        "piano.stop": _n(plan.stop_loss), "piano.tp1": _n(plan.take_profit_1), "piano.tp2": _n(plan.take_profit_2),
        "piano.tp1_quota_chiusura": _n(plan.tp1_close_fraction), "piano.trailing": plan.trailing_stop,
        "stop.metodo": sr.get("method"), "stop.livello_tecnico": sr.get("technical_level"), "stop.buffer_atr": sr.get("buffer_atr"),
        "stop.distanza_atr": sr.get("distance_atr"), "stop.nota": sr.get("note"), "tp1.base": sr.get("tp1_basis"),
        "piano.condizioni_ingresso": plan.entry_conditions, "piano.evitare_se": plan.avoid_conditions,
        "piano.uscita_anticipata": plan.early_exit_conditions, "piano.invalidazione": plan.invalidation_conditions,
        "rr.netto": _n(plan.reward_risk_net), "rendimento.tp1_lordo_pct": _n(plan.gross_return_tp1_pct),
        "rendimento.tp1_netto_pct": _n(plan.net_return_tp1_pct),
        **{f"indicatore.{k}": _n(feats.get(k)) for k in ("close", "ema20", "ema50", "ema200", "rsi14", "adx14", "atr14",
                                                          "high_55", "high_52w", "low_52w", "resistance", "support",
                                                          "rvol20", "mom_3m", "mom_6m", "rs_3m", "rs_6m")},
        "fattore.trend_momentum": fs.get("trend_momentum"), "fattore.setup": fs.get("setup"),
    }
    fundamental = common | {
        "fondamentali.disponibili": bool(fund), "fondamentali.dati": {k: _n(v) if not isinstance(v, (dict, list)) else v
                                                                    for k, v in fund.items()} if fund else None,
        "fondamentali.capitalizzazione": _n(det.get("market_cap")), "fattore.fondamentali": fs.get("fundamentals"),
        "eventi.giorni_a_utili": det.get("days_to_earnings"), "eventi.calendario_utili_verificato": det.get("earnings_known"),
        "fattore.catalizzatori": fs.get("catalysts"), "fattore.sentiment": fs.get("sentiment"),
    }
    risk = common | {
        "rischio.approvato": ra.approved if ra else None, "rischio.motivi_blocco": ra.blocking_reasons if ra else [],
        "rischio.vincolo_determinante": rres.get("binding_constraint"), "rischio.quantita": _n(plan.quantity),
        "rischio.quantita_teorica": rres.get("theoretical_quantity"), "rischio.budget_base": rres.get("risk_budget_base"),
        "rischio.controvalore_base": _n(plan.notional_base), "rischio.perdita_stop_base": _n(plan.est_loss_at_stop_base),
        "rischio.perdita_gap_base": rres.get("est_loss_gap_scenario_base"), "rischio.esposizione_pct": rres.get("exposure_pct"),
        "rischio.aggregato_dopo_pct": rres.get("aggregate_risk_after_pct"), "rischio.avvertenze": rres.get("warnings"),
        "rischio.capitale_base": rin.get("equity"), "rischio.liquidita_base": rin.get("cash"),
        "rischio.rischio_aperto_base": rin.get("open_risk"), "rr.netto": _n(plan.reward_risk_net),
        "scenari": plan.scenarios, "probabilita.calibrata": _n(plan.calibrated_probability),
        "expected_value": _n(plan.expected_value_base),
    }
    pf = common | {
        "portafoglio.posizioni_aperte": [{"ticker": f"{p.instrument.symbol}.{p.instrument.exchange}", "settore": p.instrument.sector,
                                          "quantita": _n(p.quantity), "prezzo_medio": _n(p.avg_price)} for p in positions],
        "portafoglio.posizione_sullo_stesso_titolo": any(p.instrument_id == plan.instrument_id for p in positions),
        "correlazioni.valori": (plan.correlations or {}).get("values"), "correlazioni.massima": (plan.correlations or {}).get("max"),
        "correlazioni.note": (plan.correlations or {}).get("notes"), "rischio.esposizione_pct": rres.get("exposure_pct"),
        "rischio.aggregato_dopo_pct": rres.get("aggregate_risk_after_pct"),
        "rischio.note_portfolio_manager": rres.get("portfolio_manager_notes"),
    }
    return {"market_analyst": market, "stock_selector": selector, "technical_analyst": technical,
            "fundamental_analyst": fundamental, "risk_manager": risk, "portfolio_manager": pf}


# ------------------------------------------------------------------ verifica numeri
_NUM = re.compile(r"(?<![A-Za-z_\d.])[-+]?\d+(?:[.,]\d+)*(?:[.,]\d+)?(?![A-Za-z_\d])")


def _collect_numbers(x, out: set[float]) -> None:
    if isinstance(x, bool) or x is None:
        return
    if isinstance(x, (int, float, Decimal)):
        out.add(float(x))
    elif isinstance(x, str):
        for m in _NUM.findall(x):
            v = _parse_num(m)
            if v is not None:
                out.add(v)
        try:
            out.add(float(x))
        except ValueError:
            pass
    elif isinstance(x, dict):
        for k, v in x.items():
            _collect_numbers(k, out)
            _collect_numbers(v, out)
    elif isinstance(x, (list, tuple)):
        for v in x:
            _collect_numbers(v, out)


def _parse_num(s: str) -> float | None:
    t = s.replace(" ", "")
    if "," in t and "." in t:  # 1.234,56 (it) oppure 1,234.56 (en)
        t = t.replace(".", "").replace(",", ".") if t.rfind(",") > t.rfind(".") else t.replace(",", "")
    elif "," in t:
        t = t.replace(",", ".") if len(t.split(",")[-1]) != 3 else t.replace(",", "")
    try:
        return float(t)
    except ValueError:
        return None


def unverified_numbers(texts: list[str], allowed_source) -> list[str]:
    allowed: set[float] = set()
    _collect_numbers(allowed_source, allowed)
    extra = set()
    for v in list(allowed):  # forme equivalenti: percentuali da frazioni e viceversa, valori assoluti
        extra |= {abs(v), round(v * 100, 4), round(v / 100, 6)}
    allowed |= extra
    bad = []
    for t in texts:
        for m in _NUM.findall(t or ""):
            v = _parse_num(m)
            if v is None or abs(v) <= 10 and float(v).is_integer():  # conteggi ed etichette (TP1, 3 giorni...)
                continue
            if not any(abs(abs(v) - a) <= max(0.011, abs(a) * 0.006) for a in allowed):
                bad.append(m)
    return sorted(set(bad))


def _texts(d: dict) -> list[str]:
    out = []
    for k, v in d.items():
        if k in ("data_refs", "stance", "decision"):
            continue
        out.extend(v if isinstance(v, list) else [v])
    return [x for x in out if isinstance(x, str)]


# ------------------------------------------------------------------ regole deterministiche
def allowed_decisions(plan: TradePlan, dossiers: dict) -> tuple[list[str], list[str]]:
    """Decisioni ammesse e vincoli applicati (deterministico, prima di qualunque LLM)."""
    notes = []
    if dossiers["portfolio_manager"]["portafoglio.posizione_sullo_stesso_titolo"]:
        notes.append("Posizione già aperta sul titolo: ammesse solo HOLD, REDUCE o EXIT")
        return ["HOLD", "REDUCE", "EXIT"], notes
    approved = dossiers["risk_manager"]["rischio.approvato"]
    if plan.status in ("REJECTED", "EXPIRED") or approved is False or plan.quantity <= 0:
        notes.append(f"Veto del Risk Manager o piano {plan.status}: ammessa solo REJECT")
        return ["REJECT"], notes
    if plan.status == "WAIT":
        notes.append("Piano in attesa (WAIT): BUY non ammessa finché le condizioni non sono verificate")
        return ["WAIT", "REJECT"], notes
    return ["BUY", "WAIT", "REJECT"], notes


def _f(x):
    return None if x is None else float(x)


def deterministic_opinion(agent: str, d: dict) -> AgentOpinion:
    pf, pa, risks, refs = [], [], [], []
    if agent == "market_analyst":
        reg = d.get("regime.etichetta") or "UNKNOWN"
        stance = {"BULL": "FAVOREVOLE", "BEAR": "CONTRARIO"}.get(reg, "NEUTRALE")
        summary = f"Regime di mercato {reg}" + (f" (punteggio {d['regime.punteggio']})" if d.get("regime.punteggio") is not None else "") + "."
        for b in d.get("regime.benchmark") or []:
            (pf if b.get("trend") == "UP" else pa).append(f"{b.get('ticker')}: trend {b.get('trend')}, sopra EMA200: {'sì' if b.get('above_ema200') else 'no'}")
        risks += list(d.get("regime.note") or [])[:3]
        if reg in ("HIGH_VOLATILITY", "UNKNOWN"):
            risks.append("Regime instabile o non determinabile: maggiore probabilità di falsi segnali")
        refs = ["regime.etichetta", "regime.punteggio", "regime.benchmark"]
    elif agent == "stock_selector":
        sc = _f(d.get("ranking.punteggio"))
        stance = "NEUTRALE" if sc is None else "FAVOREVOLE" if sc >= 65 else "NEUTRALE" if sc >= 50 else "CONTRARIO"
        summary = f"Punteggio composito {d.get('ranking.punteggio')} su 100 con copertura dati {d.get('ranking.copertura_dati')}."
        pf, pa = list(d.get("ranking.a_favore") or [])[:4], list(d.get("ranking.contro") or [])[:4]
        risks = ["Un punteggio elevato non dimostra che l'operazione sia profittevole: pesi da validare nel backtest"]
        refs = ["ranking.punteggio", "ranking.copertura_dati", "ranking.a_favore", "ranking.contro"]
    elif agent == "technical_analyst":
        rr = _f(d.get("rr.netto"))
        if d.get("piano.stop") == d.get("piano.ingresso"):
            stance, summary = "CONTRARIO", f"Nessun livello operativo valido: {d.get('piano.motivo_stato')}"
        else:
            stance = "FAVOREVOLE" if rr is not None and rr >= 2 else "NEUTRALE" if rr is not None and rr >= 1 else "CONTRARIO"
            summary = (f"Setup {d.get('piano.setup')}: ingresso {d.get('piano.ingresso')}, stop {d.get('piano.stop')} "
                       f"({d.get('stop.metodo')}, {d.get('stop.distanza_atr')} ATR), TP1 {d.get('piano.tp1')}, R/R netto {d.get('rr.netto')}.")
            pf = list(d.get("piano.condizioni_ingresso") or [])[:3]
            pa = list(d.get("piano.evitare_se") or [])[:3]
            if d.get("stop.nota"):
                risks.append(d["stop.nota"])
        risks += list(d.get("piano.invalidazione") or [])[:2]
        refs = ["piano.ingresso", "piano.stop", "piano.tp1", "stop.metodo", "stop.distanza_atr", "rr.netto"]
    elif agent == "fundamental_analyst":
        f = d.get("fattore.fondamentali")
        if not d.get("fondamentali.disponibili"):
            stance = "NEUTRALE"
            summary = "Dati fondamentali non disponibili per questo strumento: nessun giudizio sulla solidità economica."
        else:
            stance = "NEUTRALE" if f is None else "FAVOREVOLE" if f >= 60 else "NEUTRALE" if f >= 40 else "CONTRARIO"
            summary = f"Punteggio fondamentali {f} su 100 (fonte: snapshot del provider)."
        dte = d.get("eventi.giorni_a_utili")
        if dte is not None and dte <= 5:
            pa.append(f"Utili societari tra {dte} giorni: rischio di gap")
            stance = "CONTRARIO" if stance != "CONTRARIO" and dte <= 2 else stance
        elif not d.get("eventi.calendario_utili_verificato"):
            risks.append("Calendario degli utili non verificato")
        refs = ["fondamentali.disponibili", "fattore.fondamentali", "eventi.giorni_a_utili"]
    elif agent == "risk_manager":
        ok = d.get("rischio.approvato")
        stance = "FAVOREVOLE" if ok else "CONTRARIO"
        if ok:
            summary = (f"Rischio approvato: quantità {d.get('rischio.quantita')}, perdita stimata allo stop {d.get('rischio.perdita_stop_base')}, "
                       f"vincolo determinante {d.get('rischio.vincolo_determinante')}.")
            pf.append(f"Rischio aggregato dopo l'ingresso {d.get('rischio.aggregato_dopo_pct')}% del capitale")
        else:
            summary = "Veto: " + ("; ".join(d.get("rischio.motivi_blocco") or []) or str(d.get("piano.motivo_stato")))
            pa += list(d.get("rischio.motivi_blocco") or [])[:4]
        if d.get("rischio.perdita_gap_base"):
            risks.append(f"Scenario di gap sotto lo stop: perdita stimata {d['rischio.perdita_gap_base']}")
        risks.append("La perdita allo stop è una stima, non una perdita massima garantita")
        refs = ["rischio.approvato", "rischio.quantita", "rischio.perdita_stop_base", "rischio.aggregato_dopo_pct"]
    else:  # portfolio_manager
        mx = d.get("correlazioni.massima")
        same = d.get("portafoglio.posizione_sullo_stesso_titolo")
        stance = "CONTRARIO" if same or (mx is not None and mx >= 0.9) else "NEUTRALE" if mx is not None and mx >= 0.8 else "FAVOREVOLE"
        n = len(d.get("portafoglio.posizioni_aperte") or [])
        summary = f"{n} posizioni aperte; correlazione massima a 60 giorni: {mx if mx is not None else 'n/d'}."
        pa += list(d.get("correlazioni.note") or [])
        if same:
            pa.append("Esiste già una posizione sullo stesso titolo: rischio duplicato")
        if not pa:
            pf.append("Nessuna concentrazione o correlazione elevata con le posizioni aperte")
        refs = ["portafoglio.posizioni_aperte", "correlazioni.massima"]
    return AgentOpinion(stance=stance, summary=summary, points_for=pf, points_against=pa, risks=risks, data_refs=refs)


def deterministic_decision(plan: TradePlan, allowed: list[str], opinions: dict[str, dict]) -> CommitteeDecision:
    against = [k for k, o in opinions.items() if o["stance"] == "CONTRARIO"]
    if allowed == ["REJECT"]:
        dec = "REJECT"
    elif "HOLD" in allowed:
        dec = "HOLD"
    elif "BUY" in allowed and float(plan.score or 0) >= 60 and plan.market_regime in ("BULL", "SIDEWAYS") and len(against) <= 1:
        dec = "BUY"
    else:
        dec = "WAIT" if "WAIT" in allowed else allowed[0]
    names = {k: t for k, t, _ in AGENTS}
    return CommitteeDecision(
        decision=dec,
        summary=f"Decisione {dec} secondo le regole deterministiche (stato del piano {plan.status}, "
                f"{len(against)} agenti contrari).",
        reasons_for=[f"{names[k]}: {o['summary']}" for k, o in opinions.items() if o["stance"] == "FAVOREVOLE"][:4],
        reasons_against=[f"{names[k]}: {o['summary']}" for k, o in opinions.items() if o["stance"] == "CONTRARIO"][:4],
        could_be_wrong_if=list(plan.invalidation_conditions or [])[:3]
        + ["I pesi del ranking non superano la validazione fuori campione (Fase 5)"],
        conditions_to_act=list(plan.entry_conditions or [])[:3] if dec in ("BUY", "WAIT") else [],
        dissent=[names[k] for k in against] if dec in ("BUY", "WAIT", "HOLD") else
        [names[k] for k, o in opinions.items() if o["stance"] == "FAVOREVOLE"],
        data_refs=["piano.stato", "rischio.approvato"],
    )


# ------------------------------------------------------------------ esecuzione
def _log(db: Session, *, agent: str, plan: TradePlan, mode: str, model: str | None, prompt: str | None, inputs: dict,
         output: dict, validation: dict, meta: dict | None, status: str, error: str | None, user_id: int | None) -> AiRun:
    meta = meta or {}
    run = AiRun(kind="committee_agent", agent=agent, trade_plan_id=plan.id, user_id=user_id, mode=mode,
                model=meta.get("model") or model, prompt_version=PROMPT_VERSION, prompt_hash=_sha(prompt) if prompt else None,
                input_hash=_sha(inputs), inputs=json.loads(json.dumps(inputs, default=str)), output=output,
                validation=validation, usage=meta.get("usage") or {}, request_id=meta.get("request_id"), status=status,
                error=error, latency_ms=meta.get("latency_ms"))
    db.add(run)
    return run


def _ask_agent(llm: LLM, agent: str, title: str, task: str, dossier: dict):
    system = BASE_RULES.format(title=title, task=task)
    user = "Fascicolo dati verificati (JSON):\n" + json.dumps(dossier, ensure_ascii=False, indent=1, default=str) + \
           "\n\nRestituisci la tua valutazione secondo lo schema."
    try:
        r = llm.structured(system=system, user=user, schema=opinion_schema())
        return system, AgentOpinion.model_validate(r.data), r.meta, None, "OK"
    except (LLMError, ValidationError) as e:
        status = getattr(e, "status", "ERROR")
        return system, None, getattr(e, "meta", {}), f"{type(e).__name__}: {str(e)[:300]}", status


def run_committee(db: Session, plan: TradePlan, llm: LLM | None, actor: str, user_id: int | None = None) -> dict:
    """Esegue i 7 agenti e salva il risultato in plan.committee. Restituisce il dizionario salvato."""
    dossiers = build_dossiers(db, plan)
    allowed, constraints = allowed_decisions(plan, dossiers)
    agents_out: list[dict] = []
    opinions: dict[str, dict] = {}

    results = {}
    if llm is not None:
        with ThreadPoolExecutor(max_workers=len(AGENTS)) as ex:
            futs = {k: ex.submit(_ask_agent, llm, k, t, task, dossiers[k]) for k, t, task in AGENTS}
            results = {k: f.result() for k, f in futs.items()}
    for key, title, _task in AGENTS:
        d = dossiers[key]
        prompt, op, meta, err, status = results.get(key, (None, None, {}, None, "OK"))
        agent_mode = "ai" if op is not None else "deterministic"
        if op is None:
            op = deterministic_opinion(key, d)
            if llm is not None:
                status = "FALLBACK" if status == "ERROR" else status
        out = op.model_dump()
        bad_refs = [r for r in out["data_refs"] if r not in d]
        out["data_refs"] = [r for r in out["data_refs"] if r in d]
        validation = {"unverified_numbers": unverified_numbers(_texts(out), d) if agent_mode == "ai" else [],
                      "unknown_refs": bad_refs}
        run = _log(db, agent=key, plan=plan, mode=agent_mode, model=getattr(llm, "model", None), prompt=prompt, inputs=d,
                   output=out, validation=validation, meta=meta, status=status if llm is not None else "OK",
                   error=err, user_id=user_id)
        db.flush()
        opinions[key] = out
        agents_out.append({"agent": key, "title": title, "mode": agent_mode, "run_id": run.id, **out,
                           "validation": validation, "error": err})

    # Agent 7: decisione finale
    ckey, ctitle, ctask = COMMITTEE
    cinput = {"decisioni_ammesse": allowed, "vincoli_deterministici": constraints,
              "piano": {k: v for k, v in dossiers["technical_analyst"].items() if k.startswith(("titolo.", "piano.", "rr.", "prezzo."))},
              "ranking.punteggio": dossiers["stock_selector"]["ranking.punteggio"],
              "regime.etichetta": dossiers["market_analyst"]["regime.etichetta"],
              "rischio.approvato": dossiers["risk_manager"]["rischio.approvato"],
              "rischio.motivi_blocco": dossiers["risk_manager"]["rischio.motivi_blocco"],
              "valutazioni_agenti": {a["title"]: {k: a[k] for k in ("stance", "summary", "points_for", "points_against", "risks")}
                                     for a in agents_out}}
    cprompt, dec, cmeta, cerr, cstatus = None, None, {}, None, "OK"
    if llm is not None:
        cprompt = COMMITTEE_RULES.format(title=ctitle, task=ctask)
        try:
            r = llm.structured(system=cprompt, user="Fascicolo del comitato (JSON):\n" +
                               json.dumps(cinput, ensure_ascii=False, indent=1, default=str), schema=decision_schema(allowed))
            dec, cmeta = CommitteeDecision.model_validate(r.data), r.meta
            if dec.decision not in allowed:  # difesa in profondità: lo schema lo impedisce già
                cerr, dec = f"Decisione {dec.decision} non ammessa", None
        except (LLMError, ValidationError) as e:
            cerr, cmeta, cstatus = f"{type(e).__name__}: {str(e)[:300]}", getattr(e, "meta", {}), getattr(e, "status", "ERROR")
    cmode = "ai" if dec is not None else "deterministic"
    if dec is None:
        dec = deterministic_decision(plan, allowed, opinions)
        if llm is not None:
            cstatus = "FALLBACK" if cstatus == "ERROR" else cstatus
    cout = dec.model_dump()
    cvalid = {"unverified_numbers": unverified_numbers(_texts(cout), [cinput, dossiers]) if cmode == "ai" else [],
              "unknown_refs": [r for r in cout["data_refs"] if r not in cinput and not any(r in d for d in dossiers.values())]}
    crun = _log(db, agent=ckey, plan=plan, mode=cmode, model=getattr(llm, "model", None), prompt=cprompt, inputs=cinput,
                output=cout, validation=cvalid, meta=cmeta, status=cstatus if llm is not None else "OK", error=cerr,
                user_id=user_id)
    db.flush()

    modes = {a["mode"] for a in agents_out} | {cmode}
    result = {
        "status": "DONE", "version": COMMITTEE_VERSION, "prompt_version": PROMPT_VERSION,
        "mode": "ai" if modes == {"ai"} else "mixed" if "ai" in modes else "deterministic",
        "model": getattr(llm, "model", None), "completed_at": datetime.now(UTC).isoformat(),
        "allowed_decisions": allowed, "constraints": constraints, "inputs_hash": _sha(dossiers),
        "agents": agents_out,
        "decision": {"agent": ckey, "title": ctitle, "mode": cmode, "run_id": crun.id, **cout, "validation": cvalid, "error": cerr},
        "disclaimer": "Valutazione di supporto alla decisione: non è un ordine né una garanzia di rendimento. "
                      "Ogni esecuzione richiede conferma umana.",
    }
    plan.committee = result
    audit.record(db, actor=actor, action="committee.completed", entity="trade_plan", entity_id=plan.id, payload={
        "decision": cout["decision"], "mode": result["mode"], "model": result["model"], "allowed": allowed,
        "inputs_hash": result["inputs_hash"], "runs": [a["run_id"] for a in agents_out] + [crun.id]})
    return result
