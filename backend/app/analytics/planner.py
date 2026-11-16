"""Trade Plan Generator — livelli tecnici (Agent 3, versione deterministica).

Ordine delle decisioni (specifica §6):
1. ingresso ragionato sul setup (non una percentuale arbitraria dal prezzo);
2. stop sul livello tecnico di invalidazione (swing low / media / livello di breakout) con buffer ATR;
3. target su resistenze e multipli del rischio;
4. SOLO DOPO, dimensionamento della posizione (services/risk.py).
Tutte le soglie sono parametri iniziali da validare in Fase 5.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from app.analytics.indicators import swing_points
from app.analytics.setups import BREAKOUT, PULLBACK, TREND

PLANNER_VERSION = "planner/3.0.0"
STOP_BUFFER_ATR = 0.5
MIN_STOP_ATR = 1.0
MAX_STOP_ATR = 3.5
VALIDITY_DAYS = {BREAKOUT: 3, PULLBACK: 5, TREND: 3}
HORIZON_DAYS = {BREAKOUT: 20, PULLBACK: 30, TREND: 63}


@dataclass
class Levels:
    ok: bool
    reason: str | None = None
    entry: float | None = None
    entry_low: float | None = None
    entry_high: float | None = None
    stop: float | None = None
    tp1: float | None = None
    tp2: float | None = None
    tp1_fraction: float = 0.5
    trailing: dict = field(default_factory=dict)
    stop_rationale: dict = field(default_factory=dict)
    entry_conditions: list[str] = field(default_factory=list)
    avoid_conditions: list[str] = field(default_factory=list)
    early_exit: list[str] = field(default_factory=list)
    invalidation: list[str] = field(default_factory=list)
    validity_days: int = 3
    horizon_days: int = 20
    gap_stress_pct: float = 3.0


def _r(x: float, ref: float) -> float:
    """Arrotonda al tick plausibile: 2 decimali sopra 1, 4 sotto."""
    return round(x, 2 if ref >= 1 else 4)


def gap_stress(df: pd.DataFrame, lookback: int = 252) -> float:
    """99° percentile dei gap di apertura sfavorevoli nell'ultimo anno (%), minimo 2%."""
    prev = df["close"].shift(1)
    gaps = ((prev - df["open"]) / prev).iloc[-lookback:].dropna()
    down = gaps[gaps > 0]
    q = float(np.percentile(down, 99) * 100) if len(down) >= 10 else 3.0
    return max(2.0, round(q, 2))


def compute_levels(setup: str | None, f: dict, df: pd.DataFrame) -> Levels:
    if setup not in (BREAKOUT, PULLBACK, TREND):
        return Levels(False, "Nessun setup tecnico: nessun piano operativo")
    c, atr, e20, e50, e200 = (f.get(k) for k in ("close", "atr14", "ema20", "ema50", "ema200"))
    if any(v is None or (isinstance(v, float) and math.isnan(v)) for v in (c, atr, e20, e50, e200)) or atr <= 0:
        return Levels(False, "Indicatori incompleti: piano non calcolabile")

    # 1) ingresso
    if setup == BREAKOUT:
        lvl = f.get("high_55")
        if lvl is None or math.isnan(lvl):
            return Levels(False, "Livello di breakout non disponibile")
        entry, lo, hi = lvl + 0.1 * atr, lvl, lvl + 0.5 * atr
        cond = [f"Chiusura giornaliera sopra il livello di breakout {_r(lvl, c)}",
                "Volume superiore alla media a 20 giorni"]
        avoid = [f"Apertura in gap oltre {_r(hi + atr, c)} (più di 1 ATR sopra l'intervallo)",
                 f"Ritorno sotto {_r(lvl, c)} prima dell'ingresso (falso breakout)"]
    elif setup == PULLBACK:
        entry = max(e20, c - 0.25 * atr) if c > e20 else c
        lo, hi = e20 - 0.25 * atr, e20 + 0.5 * atr
        entry = min(max(entry, lo), hi)
        cond = [f"Tenuta della EMA20 ({_r(e20, c)}) e della EMA50 ({_r(e50, c)})",
                "RSI che risale dalla zona 40–50", "Candela di inversione rialzista o chiusura sopra il massimo del giorno precedente"]
        avoid = [f"Chiusura sotto la EMA50 ({_r(e50, c)})", "Volumi in forte aumento sui ribassi"]
    else:
        entry, lo, hi = c, c - 0.5 * atr, c + 0.25 * atr
        cond = ["EMA20 > EMA50 > EMA200 confermato in chiusura", "ADX ≥ 20"]
        avoid = [f"Prezzo oltre {_r(hi, c)}: ingresso esteso, attendere un ritracciamento",
                 "RSI sopra 75"]
    avoid.append("Utili societari o eventi straordinari entro 2 giorni")

    # 2) stop: livello tecnico, poi buffer di volatilità
    _, piv_low = swing_points(df["high"], df["low"], 5)
    lows = piv_low.iloc[-126:].dropna()
    below = lows[lows < entry - 0.5 * atr]
    candidates = {}
    if len(below):
        candidates["swing_low"] = float(below.iloc[-1])  # ultimo minimo confermato sotto l'ingresso
    if setup in (PULLBACK, TREND) and e50 < entry:
        candidates["ema50"] = float(e50)
    if setup == BREAKOUT:
        candidates["breakout_level"] = float(f["high_55"]) - atr
    if not candidates:
        return Levels(False, "Nessun livello tecnico di invalidazione sotto l'ingresso")
    # il livello di invalidazione più vicino sotto l'ingresso
    name, tech = max(candidates.items(), key=lambda kv: kv[1])
    stop = tech - STOP_BUFFER_ATR * atr
    dist = (entry - stop) / atr
    note = None
    if dist < MIN_STOP_ATR:
        stop = entry - MIN_STOP_ATR * atr
        note = f"Livello tecnico troppo vicino: stop portato a {MIN_STOP_ATR} ATR (rumore di volatilità)"
    elif dist > MAX_STOP_ATR:
        return Levels(False, f"Stop tecnico a {dist:.1f} ATR dall'ingresso: rischio per azione eccessivo, setup scartato")
    risk = entry - stop

    # 3) target
    res = f.get("resistance")
    tp1 = entry + 2 * risk
    tp_note = "2R"
    if res is not None and not math.isnan(res) and entry < res:
        if res < entry + 1 * risk:
            return Levels(False, f"Resistenza a {_r(res, c)}, meno di 1R sopra l'ingresso: rapporto sfavorevole")
        if res < tp1:
            tp1, tp_note = res - 0.1 * atr, f"sotto la resistenza {_r(res, c)}"
    h52 = f.get("high_52w")
    tp2 = entry + (3.0 if setup == PULLBACK else 3.5) * risk
    if setup == PULLBACK and h52 is not None and not math.isnan(h52) and tp1 < h52 < tp2:
        tp2 = h52
    trailing = {"after": "TP1", "move_stop_to": "breakeven",
                "then": f"trailing a 2,5 ATR sotto il massimo di chiusura (≈{_r(2.5 * atr, c)})"}
    early = ["Chiusura sotto la EMA50" if setup != BREAKOUT else f"Chiusura sotto il livello di breakout {_r(f['high_55'], c)}",
             "Regime di mercato che passa a BEAR",
             f"Nessun progresso verso TP1 entro {HORIZON_DAYS[setup] // 2} giorni di borsa (time stop)",
             "Annuncio di utili o evento straordinario non previsto"]
    inval = [f"Chiusura sotto lo stop {_r(stop, c)}", f"Rottura del livello tecnico ({name}: {_r(tech, c)})"]
    return Levels(
        True, None, _r(entry, c), _r(lo, c), _r(hi, c), _r(stop, c), _r(tp1, c), _r(tp2, c), 0.5, trailing,
        {"method": name, "technical_level": _r(tech, c), "buffer_atr": STOP_BUFFER_ATR, "atr": _r(atr, c),
         "distance_atr": round((entry - stop) / atr, 2), "candidates": {k: _r(v, c) for k, v in candidates.items()},
         "note": note, "tp1_basis": tp_note},
        cond, avoid, early, inval, VALIDITY_DAYS[setup], HORIZON_DAYS[setup], gap_stress(df),
    )


def return_correlation(a: pd.Series, b: pd.Series, n: int = 60) -> float | None:
    ra, rb = a.pct_change(), b.pct_change()
    j = pd.concat([ra, rb], axis=1, join="inner").dropna().iloc[-n:]
    if len(j) < 30:
        return None
    v = float(j.iloc[:, 0].corr(j.iloc[:, 1]))
    return None if math.isnan(v) else round(v, 3)
