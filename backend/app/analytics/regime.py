"""Agent 1 — Market Analyst (versione deterministica).

Classifica il regime di mercato a partire dagli indici/ETF di riferimento:
- BULL            : prezzo sopra EMA200 e EMA50 crescente su tutti i benchmark
- BEAR            : prezzo sotto EMA200 e EMA50 decrescente su tutti i benchmark
- HIGH_VOLATILITY : volatilità a 20 giorni oltre il 85° percentile dell'ultimo anno, oppure VIX > 25
- SIDEWAYS        : tutti gli altri casi
Le soglie sono parametri iniziali da validare in Fase 5.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

import pandas as pd

from app.analytics.indicators import ema, hist_volatility

REGIME_SCORE = {"BULL": 80.0, "SIDEWAYS": 50.0, "HIGH_VOLATILITY": 30.0, "BEAR": 20.0}
VOL_PCTL_THRESHOLD = 0.85
VIX_THRESHOLD = 25.0


@dataclass
class BenchmarkState:
    ticker: str
    as_of: str
    close: float
    above_ema200: bool
    ema50_slope_10d: float
    vol20: float
    vol20_percentile_1y: float
    trend: str  # UP / DOWN / FLAT
    source: str


@dataclass
class Regime:
    regime: str
    score: float
    benchmarks: list[BenchmarkState] = field(default_factory=list)
    vix: dict | None = None
    rates: dict | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def benchmark_state(ticker: str, close: pd.Series, source: str) -> BenchmarkState | None:
    close = close.dropna()
    if len(close) < 260:
        return None
    e200, e50 = ema(close, 200), ema(close, 50)
    slope = float(e50.iloc[-1] / e50.iloc[-11] - 1)
    vol = hist_volatility(close, 20)
    last_year = vol.iloc[-252:].dropna()
    pctl = float((last_year <= vol.iloc[-1]).mean()) if len(last_year) else float("nan")
    above = bool(close.iloc[-1] > e200.iloc[-1])
    trend = "UP" if above and slope > 0 else "DOWN" if (not above and slope < 0) else "FLAT"
    return BenchmarkState(ticker, str(close.index[-1].date() if hasattr(close.index[-1], "date") else close.index[-1]),
                          float(close.iloc[-1]), above, slope, float(vol.iloc[-1]), pctl, trend, source)


def classify(states: list[BenchmarkState], vix_close: pd.Series | None = None,
             rates_close: pd.Series | None = None, vix_source: str | None = None,
             rates_source: str | None = None) -> Regime:
    notes: list[str] = []
    if not states:
        return Regime("UNKNOWN", float("nan"), notes=["Storico dei benchmark insufficiente: regime non determinabile."])
    vix = None
    high_vol = any(s.vol20_percentile_1y >= VOL_PCTL_THRESHOLD for s in states)
    if vix_close is not None and len(vix_close.dropna()):
        v = vix_close.dropna()
        vix = {"value": float(v.iloc[-1]), "as_of": str(v.index[-1])[:10], "source": vix_source}
        if vix["value"] > VIX_THRESHOLD:
            high_vol = True
            notes.append(f"VIX {vix['value']:.1f} sopra {VIX_THRESHOLD:.0f}.")
    rates = None
    if rates_close is not None and len(rates_close.dropna()) > 63:
        r = rates_close.dropna()
        rates = {"us10y": float(r.iloc[-1]), "change_3m": float(r.iloc[-1] - r.iloc[-64]),
                 "as_of": str(r.index[-1])[:10], "source": rates_source}
    if high_vol:
        reg = "HIGH_VOLATILITY"
    elif all(s.trend == "UP" for s in states):
        reg = "BULL"
    elif all(s.trend == "DOWN" for s in states):
        reg = "BEAR"
    else:
        reg = "SIDEWAYS"
    return Regime(reg, REGIME_SCORE[reg], states, vix, rates, notes)
