"""Metriche di performance, bootstrap e intervalli di confidenza (specifica §8)."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

TRADING_DAYS = 252


def _r(x, n=4):
    if x is None:
        return None
    x = float(x)
    return None if math.isnan(x) or math.isinf(x) else round(x, n)


def equity_metrics(equity: pd.Series, exposure: pd.Series | None = None, traded_notional: float = 0.0) -> dict:
    eq = equity.dropna()
    if len(eq) < 2 or eq.iloc[0] <= 0:
        return {"days": len(eq)}
    rets = eq.pct_change().dropna()
    years = max(len(eq) - 1, 1) / TRADING_DAYS
    total = eq.iloc[-1] / eq.iloc[0] - 1
    cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1 / years) - 1 if eq.iloc[-1] > 0 else -1.0
    dd = eq / eq.cummax() - 1
    sd = rets.std(ddof=1)
    downside = rets[rets < 0]
    dsd = math.sqrt((downside ** 2).sum() / len(rets)) if len(rets) else float("nan")
    avg_eq = float(eq.mean())
    return {
        "days": len(eq), "start": str(eq.index[0].date()), "end": str(eq.index[-1].date()),
        "start_equity": _r(eq.iloc[0], 2), "end_equity": _r(eq.iloc[-1], 2),
        "net_return_pct": _r(total * 100, 2), "cagr_pct": _r(cagr * 100, 2),
        "max_drawdown_pct": _r(dd.min() * 100, 2),
        "volatility_pct": _r(sd * math.sqrt(TRADING_DAYS) * 100, 2) if sd == sd else None,
        "sharpe": _r(rets.mean() / sd * math.sqrt(TRADING_DAYS), 2) if sd and sd == sd else None,
        "sortino": _r(rets.mean() / dsd * math.sqrt(TRADING_DAYS), 2) if dsd and dsd == dsd else None,
        "avg_exposure_pct": _r(float(exposure.reindex(eq.index).fillna(0).mean()) * 100, 2) if exposure is not None else None,
        "turnover_annual": _r(traded_notional / avg_eq / years, 2) if avg_eq > 0 else None,
    }


def trade_metrics(trades: list[dict]) -> dict:
    closed = [t for t in trades if t.get("exit_date")]
    n = len(closed)
    if n == 0:
        return {"trades": 0}
    pnl = np.array([t["pnl_base"] for t in closed], dtype=float)
    rmul = np.array([t["r_multiple"] for t in closed if t.get("r_multiple") is not None], dtype=float)
    wins, losses = pnl[pnl > 0], pnl[pnl <= 0]
    gp, gl = wins.sum(), -losses.sum()
    p = len(wins) / n
    lo, hi = wilson(len(wins), n)
    return {
        "trades": n, "win_rate_pct": _r(p * 100, 2), "win_rate_ci95_pct": [_r(lo * 100, 2), _r(hi * 100, 2)],
        "avg_win_base": _r(wins.mean(), 2) if len(wins) else None, "avg_loss_base": _r(losses.mean(), 2) if len(losses) else None,
        "profit_factor": _r(gp / gl, 2) if gl > 0 else None, "expectancy_base": _r(pnl.mean(), 2),
        "expectancy_r": _r(rmul.mean(), 3) if len(rmul) else None, "total_pnl_base": _r(pnl.sum(), 2),
        "avg_holding_days": _r(np.mean([t["holding_days"] for t in closed]), 1),
        "worst_trade_base": _r(pnl.min(), 2), "best_trade_base": _r(pnl.max(), 2),
        "exit_reasons": pd.Series([t["exit_reason"] for t in closed]).value_counts().to_dict(),
    }


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (float("nan"), float("nan"))
    p = k / n
    den = 1 + z * z / n
    c = (p + z * z / (2 * n)) / den
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return max(0.0, c - h), min(1.0, c + h)


def bootstrap(trades: list[dict], n_iter: int = 2000, seed: int = 7) -> dict:
    """Ricampionamento delle operazioni (con reinserimento): incertezza di expectancy, profit factor e P&L."""
    pnl = np.array([t["pnl_base"] for t in trades if t.get("exit_date")], dtype=float)
    if len(pnl) < 10:
        return {"available": False, "note": "Servono almeno 10 operazioni chiuse"}
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(pnl), size=(n_iter, len(pnl)))
    s = pnl[idx]
    exp = s.mean(axis=1)
    tot = s.sum(axis=1)
    gp = np.where(s > 0, s, 0).sum(axis=1)
    gl = -np.where(s <= 0, s, 0).sum(axis=1)
    pf = np.where(gl > 0, gp / np.where(gl > 0, gl, 1), np.nan)
    q = lambda a: [_r(np.nanpercentile(a, 2.5), 2), _r(np.nanpercentile(a, 50), 2), _r(np.nanpercentile(a, 97.5), 2)]  # noqa: E731
    return {"available": True, "iterations": n_iter, "trades": len(pnl),
            "expectancy_base_p2_5_p50_p97_5": q(exp), "total_pnl_base_p2_5_p50_p97_5": q(tot),
            "profit_factor_p2_5_p50_p97_5": q(pf), "prob_expectancy_negative_pct": _r((exp <= 0).mean() * 100, 2)}


def benchmark_metrics(close: pd.Series, index: pd.DatetimeIndex, capital: float) -> dict | None:
    c = close.reindex(index).ffill().dropna()
    if len(c) < 2:
        return None
    return equity_metrics(c / c.iloc[0] * capital)


def by_group(trades: list[dict], key: str) -> dict:
    groups: dict[str, list[dict]] = {}
    for t in trades:
        if t.get("exit_date"):
            groups.setdefault(str(t.get(key) or "N/D"), []).append(t)
    return {k: {kk: v for kk, v in trade_metrics(v).items() if kk in
                ("trades", "win_rate_pct", "expectancy_base", "expectancy_r", "profit_factor", "total_pnl_base")}
            for k, v in sorted(groups.items())}
