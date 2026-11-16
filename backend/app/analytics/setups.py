"""Rilevamento dei setup tecnici e punteggio di qualità (fattore "Qualità tecnica del setup").

I setup descrivono una struttura di prezzo; NON sono segnali di acquisto.
Soglie iniziali, da validare statisticamente in Fase 5.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import pandas as pd

BREAKOUT, PULLBACK, TREND = "BREAKOUT", "PULLBACK", "TREND_FOLLOWING"
STYLE = {BREAKOUT: "SWING", PULLBACK: "SWING", TREND: "POSITION"}
HORIZON = {BREAKOUT: "3-20 giorni", PULLBACK: "5-30 giorni", TREND: "1-3 mesi"}


def _ok(*vals) -> bool:
    return all(v is not None and not (isinstance(v, float) and math.isnan(v)) for v in vals)


@dataclass
class SetupResult:
    setup: str | None
    matched: list[str]
    quality: float
    notes: list[str] = field(default_factory=list)


def detect(ind: pd.DataFrame, low: pd.Series) -> SetupResult:
    """`ind` è l'output di indicators.compute_all; usa solo l'ultima riga (e le 3 precedenti per i pullback)."""
    r = ind.iloc[-1]
    c, e20, e50, e200 = r.close, r.ema20, r.ema50, r.ema200
    atr_, adx_, rsi_, rvol = r.atr14, r.adx, r.rsi14, r.rvol20
    notes: list[str] = []
    matched: list[str] = []
    if not _ok(c, e20, e50, e200, atr_, adx_, rsi_):
        return SetupResult(None, [], 0.0, ["Indicatori incompleti"])

    if _ok(r.high_55, rvol) and c > r.high_55 and rvol >= 1.5:
        matched.append(BREAKOUT)
    recent_low = float(low.iloc[-3:].min())
    near_ma = min(abs(recent_low - e20), abs(recent_low - e50)) <= atr_
    if c > e200 and e50 > e200 and c > e50 and near_ma and 35 <= rsi_ <= 55:
        matched.append(PULLBACK)
    if e20 > e50 > e200 and c > e20 and adx_ >= 20 and _ok(r.ema50_slope) and r.ema50_slope > 0:
        matched.append(TREND)

    setup = next((s for s in (BREAKOUT, PULLBACK, TREND) if s in matched), None)
    if setup is None:
        return SetupResult(None, [], 10.0, ["Nessun setup riconosciuto"])

    q = 50.0
    q += min(15.0, max(0.0, (adx_ - 20) * 0.75))                # forza del trend
    ext = (c - e20) / atr_                                        # estensione dalla media in ATR
    if ext > 3:
        q -= min(25.0, (ext - 3) * 10)
        notes.append(f"Prezzo esteso: {ext:.1f} ATR sopra EMA20")
    if rsi_ > 75:
        q -= 15
        notes.append(f"RSI {rsi_:.0f}: ipercomprato")
    if _ok(rvol) and setup == BREAKOUT:
        q += min(15.0, (rvol - 1.5) * 10)
    if setup == PULLBACK and 40 <= rsi_ <= 50:
        q += 10
    if len(matched) > 1:
        q += 5
        notes.append("Più setup concordanti: " + ", ".join(matched))
    return SetupResult(setup, matched, max(0.0, min(100.0, q)), notes)
