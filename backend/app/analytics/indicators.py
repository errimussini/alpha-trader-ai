"""Indicatori tecnici deterministici.

Tutte le funzioni sono "causali": il valore al giorno t usa solo dati fino a t incluso
(nessun look-ahead). Input: Series/DataFrame pandas indicizzati per data in ordine crescente.
Convenzioni:
- EMA: smoothing esponenziale standard, alpha = 2/(n+1), inizializzazione con la prima osservazione
  (adjust=False) e valori NaN finché non sono disponibili n osservazioni.
- RSI, ATR, ADX: smoothing di Wilder (alpha = 1/n), come nella definizione originale.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

TRADING_DAYS = 252


def ema(s: pd.Series, n: int) -> pd.Series:
    out = s.ewm(span=n, adjust=False).mean()
    out.iloc[: n - 1] = np.nan
    return out


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def _wilder(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    gain = _wilder(d.clip(lower=0), n)
    loss = _wilder(-d.clip(upper=0), n)
    rs = gain / loss
    out = 100 - 100 / (1 + rs)
    out[(loss == 0) & gain.notna()] = 100.0
    return out


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    line = ema(close, fast) - ema(close, slow)
    sig = line.ewm(span=signal, adjust=False).mean()
    sig[line.isna()] = np.nan
    return pd.DataFrame({"macd": line, "signal": sig, "hist": line - sig})


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev = close.shift(1)
    return pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 14) -> pd.Series:
    return _wilder(true_range(high, low, close), n)


def adx(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 14) -> pd.DataFrame:
    up = high.diff()
    down = -low.diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=high.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=high.index)
    tr = _wilder(true_range(high, low, close), n)
    plus_di = 100 * _wilder(plus_dm, n) / tr
    minus_di = 100 * _wilder(minus_dm, n) / tr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    return pd.DataFrame({"adx": _wilder(dx, n), "plus_di": plus_di, "minus_di": minus_di})


def bollinger(close: pd.Series, n: int = 20, k: float = 2.0) -> pd.DataFrame:
    mid = sma(close, n)
    sd = close.rolling(n, min_periods=n).std(ddof=0)
    upper, lower = mid + k * sd, mid - k * sd
    return pd.DataFrame({"mid": mid, "upper": upper, "lower": lower,
                         "pct_b": (close - lower) / (upper - lower), "width": (upper - lower) / mid})


def vwap_rolling(high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series, n: int = 20) -> pd.Series:
    """VWAP su finestra mobile di n giorni (su dati giornalieri il VWAP intraday non è calcolabile)."""
    tp = (high + low + close) / 3
    return (tp * volume).rolling(n, min_periods=n).sum() / volume.rolling(n, min_periods=n).sum()


def relative_volume(volume: pd.Series, n: int = 20) -> pd.Series:
    """Volume del giorno rispetto alla media dei n giorni PRECEDENTI."""
    return volume / volume.shift(1).rolling(n, min_periods=n).mean()


def momentum(close: pd.Series, bars: int) -> pd.Series:
    return close / close.shift(bars) - 1


def momentum_12_1(close: pd.Series) -> pd.Series:
    """Rendimento 12 mesi escluso l'ultimo mese (classico fattore momentum)."""
    return close.shift(21) / close.shift(252) - 1


def hist_volatility(close: pd.Series, n: int = 20) -> pd.Series:
    r = np.log(close / close.shift(1))
    return r.rolling(n, min_periods=n).std(ddof=1) * np.sqrt(TRADING_DAYS)


def relative_strength(close: pd.Series, bench: pd.Series, bars: int = 63) -> pd.Series:
    """Sovra/sotto-performance rispetto al benchmark sugli ultimi `bars` giorni (allineata per data)."""
    b = bench.reindex(close.index).ffill()
    return (close / close.shift(bars)) / (b / b.shift(bars)) - 1


def rolling_high(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).max()


def rolling_low(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).min()


def swing_points(high: pd.Series, low: pd.Series, k: int = 5) -> tuple[pd.Series, pd.Series]:
    """Massimi/minimi locali confermati: un pivot al giorno t è noto solo al giorno t+k.

    Restituisce serie indicizzate alla DATA DI CONFERMA (t+k), quindi utilizzabili senza look-ahead.
    """
    win = 2 * k + 1
    hmax = high.rolling(win, min_periods=win).max()
    lmin = low.rolling(win, min_periods=win).min()
    center_h = high.shift(k)
    center_l = low.shift(k)
    piv_h = center_h.where(center_h == hmax)
    piv_l = center_l.where(center_l == lmin)
    return piv_h, piv_l


def support_resistance(high: pd.Series, low: pd.Series, close: pd.Series, k: int = 5,
                       lookback: int = 126) -> tuple[float | None, float | None]:
    """Supporto = pivot low confermato più vicino sotto l'ultima chiusura; resistenza = pivot high più vicino sopra."""
    ph, pl = swing_points(high, low, k)
    last = float(close.iloc[-1])
    highs = ph.iloc[-lookback:].dropna()
    lows = pl.iloc[-lookback:].dropna()
    above = highs[highs > last]
    below = lows[lows < last]
    res = float(above.min()) if len(above) else None
    sup = float(below.max()) if len(below) else None
    return sup, res


def compute_all(df: pd.DataFrame, bench_close: pd.Series | None = None) -> pd.DataFrame:
    """df con colonne open, high, low, close, adj_close, volume. Restituisce un DataFrame di indicatori.

    I rendimenti (momentum, volatilità, forza relativa) usano adj_close se disponibile;
    i livelli di prezzo (EMA, ATR, supporti) usano i prezzi non rettificati per dividendi.
    """
    c, h, l, v = df["close"], df["high"], df["low"], df["volume"]
    rc = df["adj_close"].fillna(df["close"]) if "adj_close" in df else c
    out = pd.DataFrame(index=df.index)
    out["close"] = c
    for n in (20, 50, 200):
        out[f"ema{n}"] = ema(c, n)
    out["rsi14"] = rsi(c, 14)
    out = out.join(macd(c))
    out["atr14"] = atr(h, l, c, 14)
    out = out.join(adx(h, l, c, 14))
    out = out.join(bollinger(c).add_prefix("bb_"))
    out["vwap20"] = vwap_rolling(h, l, c, v, 20)
    out["rvol20"] = relative_volume(v, 20)
    out["avg_volume20"] = v.rolling(20, min_periods=20).mean()
    out["traded_value20"] = (c * v).rolling(20, min_periods=20).mean()
    for name, bars in (("mom_1m", 21), ("mom_3m", 63), ("mom_6m", 126), ("mom_12m", 252)):
        out[name] = momentum(rc, bars)
    out["mom_12_1"] = momentum_12_1(rc)
    out["vol20"] = hist_volatility(rc, 20)
    out["vol60"] = hist_volatility(rc, 60)
    out["high_52w"] = rolling_high(h, 252)
    out["low_52w"] = rolling_low(l, 252)
    out["high_55"] = rolling_high(h, 55).shift(1)  # massimo dei 55 giorni PRECEDENTI (per i breakout)
    out["ema50_slope"] = out["ema50"] / out["ema50"].shift(10) - 1
    if bench_close is not None:
        out["rs_3m"] = relative_strength(rc, bench_close, 63)
        out["rs_6m"] = relative_strength(rc, bench_close, 126)
    return out
