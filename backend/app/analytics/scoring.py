"""Stock Ranking Engine: filtri di eleggibilità e punteggio composito 0–100.

Pesi iniziali della specifica (§5), da validare in Fase 5:
    trend_momentum 25 · setup 20 · fundamentals 15 · macro_sector 15 · liquidity_vol 10 · catalysts 10 · sentiment 5

Un fattore senza dati affidabili vale None (mai stimato). Il punteggio è la media pesata dei soli
fattori disponibili; se la copertura dei pesi è inferiore a MIN_COVERAGE il titolo non riceve punteggio.
Il punteggio ordina i candidati: NON è una probabilità né una previsione di rendimento.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import date

import pandas as pd

from app.analytics.setups import HORIZON, STYLE, SetupResult

WEIGHTS = {
    "trend_momentum": 25, "setup": 20, "fundamentals": 15, "macro_sector": 15,
    "liquidity_vol": 10, "catalysts": 10, "sentiment": 5,
}
MODEL_VERSION = "ranking/2.0.0"
MIN_COVERAGE = 0.70


@dataclass
class FilterConfig:
    min_price: float = 5.0
    min_market_cap: float = 1e9           # nella valuta dello strumento
    min_traded_value: float = 5e6         # controvalore medio giornaliero a 20 giorni
    max_spread_pct: float = 0.5
    min_history_bars: int = 260
    max_bar_age_days: int = 4
    exclude_earnings_within_days: int = 2
    max_abs_daily_return: float = 0.5     # oltre: probabile anomalia del dato


@dataclass
class Candidate:
    instrument_id: int
    ticker: str
    instrument_type: str
    sector: str | None
    exchange: str
    price: float | None
    price_date: date | None
    source: str
    is_demo: bool
    eligible: bool = True
    exclusions: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    features: dict = field(default_factory=dict)
    setup: SetupResult | None = None
    fundamentals: dict | None = None
    days_to_earnings: int | None = None
    earnings_known: bool = False
    spread_pct: float | None = None
    market_cap: float | None = None
    factors: dict = field(default_factory=dict)
    score: float | None = None
    coverage: float = 0.0
    risk_level: str | None = None
    rationale_for: list[str] = field(default_factory=list)
    rationale_against: list[str] = field(default_factory=list)

    @property
    def style(self) -> str | None:
        return STYLE.get(self.setup.setup) if self.setup and self.setup.setup else None

    @property
    def horizon(self) -> str | None:
        return HORIZON.get(self.setup.setup) if self.setup and self.setup.setup else None


def _f(v) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or math.isinf(f) else f


def apply_filters(c: Candidate, bars: pd.DataFrame, as_of: date, cfg: FilterConfig) -> None:
    if len(bars) < cfg.min_history_bars:
        c.exclusions.append(f"Storico insufficiente ({len(bars)} barre, minimo {cfg.min_history_bars})")
    if c.price_date is None or (as_of - c.price_date).days > cfg.max_bar_age_days:
        c.exclusions.append(f"Dati non aggiornati (ultima barra {c.price_date})")
    if c.price is not None and c.price < cfg.min_price:
        c.exclusions.append(f"Prezzo {c.price:.2f} sotto il minimo {cfg.min_price} (penny stock)")
    tv = _f(c.features.get("traded_value20"))
    if tv is None or tv < cfg.min_traded_value:
        c.exclusions.append("Controvalore medio giornaliero insufficiente" if tv is not None else "Volumi non disponibili")
    if c.instrument_type == "STOCK":
        if c.market_cap is None:
            c.flags.append("Capitalizzazione non disponibile")
        elif c.market_cap < cfg.min_market_cap:
            c.exclusions.append(f"Capitalizzazione {c.market_cap / 1e9:.2f} mld sotto il minimo")
    if c.spread_pct is None:
        c.flags.append("Spread bid/ask non disponibile")
    elif c.spread_pct > cfg.max_spread_pct:
        c.exclusions.append(f"Spread {c.spread_pct:.2f}% oltre il massimo {cfg.max_spread_pct}%")
    rets = bars["close"].pct_change().abs()
    if len(rets) and float(rets.iloc[-260:].max(skipna=True) or 0) > cfg.max_abs_daily_return:
        c.exclusions.append("Anomalia nei dati: variazione giornaliera oltre il 50% (possibile split non rettificato)")
    if c.days_to_earnings is not None and 0 <= c.days_to_earnings <= cfg.exclude_earnings_within_days:
        c.exclusions.append(f"Utili tra {c.days_to_earnings} giorni: nuovi ingressi sospesi")
    c.eligible = not c.exclusions


def _pct_rank(values: dict[int, float | None], higher_is_better: bool = True) -> dict[int, float]:
    s = pd.Series({k: v for k, v in values.items() if v is not None}, dtype=float)
    if s.empty:
        return {}
    r = s.rank(pct=True, ascending=higher_is_better) * 100
    return r.to_dict()


def _vol_score(v: float | None) -> float | None:
    if v is None:
        return None
    if 0.15 <= v <= 0.40:
        return 100.0
    if v < 0.15:
        return max(0.0, 100 * (v - 0.05) / 0.10)
    return max(0.0, 100 * (0.80 - v) / 0.40)


def _risk_level(v: float | None) -> str | None:
    if v is None:
        return None
    return "LOW" if v < 0.20 else "MEDIUM" if v < 0.35 else "HIGH"


FUND_METRICS = (  # chiave, migliore se alto?
    ("returnOnEquity", True), ("operatingMargins", True), ("revenueGrowth", True), ("earningsGrowth", True),
    ("debtToEquity", False), ("forwardPE", False), ("fcf_yield", True),
)


def score_universe(cands: list[Candidate], regime_score: float | None) -> None:
    pool = [c for c in cands if c.eligible]
    ids = {c.instrument_id: c for c in pool}
    if not pool:
        return
    feat = lambda k: {c.instrument_id: _f(c.features.get(k)) for c in pool}  # noqa: E731
    mom = [_pct_rank(feat(k)) for k in ("mom_12_1", "mom_6m", "mom_3m")]
    rs = _pct_rank(feat("rs_3m"))
    tv = _pct_rank(feat("traded_value20"))

    # fondamentali: rank all'interno del settore se ci sono almeno 5 pari, altrimenti dell'universo azionario
    fund_scores: dict[int, float] = {}
    stocks = [c for c in pool if c.instrument_type == "STOCK" and c.fundamentals]
    groups: dict[str, list[Candidate]] = {}
    for c in stocks:
        groups.setdefault(c.sector or "_", []).append(c)
    for c in stocks:
        peers = groups.get(c.sector or "_", [])
        peers = peers if len(peers) >= 5 else stocks
        parts = []
        for key, hib in FUND_METRICS:
            vals = {p.instrument_id: _f(p.fundamentals.get(key)) for p in peers}
            if key == "forwardPE":
                vals = {k: v for k, v in vals.items() if v is not None and v > 0}
            if vals.get(c.instrument_id) is None or len([v for v in vals.values() if v is not None]) < 3:
                continue
            parts.append(_pct_rank(vals, hib)[c.instrument_id])
        if len(parts) >= 3:
            fund_scores[c.instrument_id] = sum(parts) / len(parts)

    for iid, c in ids.items():
        f = c.features
        # 1) trend e momentum
        ranks = [m[iid] for m in mom if iid in m]
        align = None
        if all(_f(f.get(k)) is not None for k in ("close", "ema20", "ema50", "ema200")):
            align = (40 if f["close"] > f["ema200"] else 0) + (35 if f["ema50"] > f["ema200"] else 0) + \
                    (25 if f["ema20"] > f["ema50"] else 0)
        tm = 0.6 * (sum(ranks) / len(ranks)) + 0.4 * align if ranks and align is not None else None
        # 2) setup
        st = c.setup.quality if c.setup else None
        # 3) fondamentali
        fu = fund_scores.get(iid)
        # 4) macro e settore
        ms = None
        if regime_score is not None and not math.isnan(regime_score):
            ms = 0.6 * regime_score + 0.4 * rs[iid] if iid in rs else regime_score
        # 5) liquidità e volatilità
        vs = _vol_score(_f(f.get("vol20")))
        lv = 0.5 * tv[iid] + 0.5 * vs if iid in tv and vs is not None else None
        # 6) catalizzatori (solo rischio evento utili, con calendario disponibile)
        ca = None
        if c.earnings_known:
            d = c.days_to_earnings
            ca = 50.0 if d is None or d > 20 else 25.0 if d <= 5 else 60.0
        # 7) sentiment: nessuna fonte affidabile integrata -> non disponibile
        se = None
        c.factors = {"trend_momentum": tm, "setup": st, "fundamentals": fu, "macro_sector": ms,
                     "liquidity_vol": lv, "catalysts": ca, "sentiment": se}
        avail = {k: v for k, v in c.factors.items() if v is not None}
        cov = sum(WEIGHTS[k] for k in avail) / sum(WEIGHTS.values())
        c.coverage = round(cov, 4)
        c.risk_level = _risk_level(_f(f.get("vol20")))
        if cov < MIN_COVERAGE:
            c.score = None
            c.flags.append(f"Copertura dati {cov:.0%}: punteggio non calcolato")
        else:
            c.score = round(sum(WEIGHTS[k] * v for k, v in avail.items()) / sum(WEIGHTS[k] for k in avail), 2)
        _explain(c)


def _explain(c: Candidate) -> None:
    f, fa = c.features, c.factors
    pro, con = c.rationale_for, c.rationale_against
    if c.setup and c.setup.setup:
        pro.append(f"Setup {c.setup.setup} rilevato")
    else:
        con.append("Nessun setup tecnico riconosciuto")
    if (fa.get("trend_momentum") or 0) >= 70:
        pro.append("Trend e momentum nel terzo superiore dell'universo")
    elif fa.get("trend_momentum") is not None and fa["trend_momentum"] < 40:
        con.append("Trend e momentum deboli rispetto all'universo")
    if _f(f.get("rs_3m")) is not None:
        (pro if f["rs_3m"] > 0 else con).append(f"Forza relativa a 3 mesi {f['rs_3m'] * 100:+.1f}% vs benchmark")
    if _f(f.get("rsi14")) is not None and f["rsi14"] > 75:
        con.append(f"RSI {f['rsi14']:.0f}: ipercomprato")
    if c.days_to_earnings is not None and c.days_to_earnings <= 10:
        con.append(f"Utili tra {c.days_to_earnings} giorni: rischio evento")
    if fa.get("fundamentals") is not None:
        (pro if fa["fundamentals"] >= 60 else con if fa["fundamentals"] < 40 else []).append(
            f"Fondamentali: {fa['fundamentals']:.0f}/100 rispetto ai pari")
    if c.risk_level == "HIGH":
        con.append("Volatilità storica elevata")
    if c.setup:
        con.extend(c.setup.notes if c.setup.setup else [])


def candidate_payload(c: Candidate) -> dict:
    return {
        "features": {k: _f(v) for k, v in c.features.items()},
        "setup": asdict(c.setup) if c.setup else None,
        "exclusions": c.exclusions, "flags": c.flags, "days_to_earnings": c.days_to_earnings,
        "earnings_known": c.earnings_known, "spread_pct": c.spread_pct, "market_cap": c.market_cap,
        "fundamentals_used": c.fundamentals,
    }
