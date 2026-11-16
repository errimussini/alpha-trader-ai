"""Orchestrazione del backtest: dati dal database, split temporali, walk-forward, stress test, bootstrap,
calibrazione fuori campione e salvataggio (BacktestRun + ModelEvaluation).

Controlli dei bias:
- look-ahead: indicatori causali, segnali in chiusura ed esecuzione dal giorno successivo, regime calcolato
  con i soli dati disponibili alla data; fondamentali/catalizzatori/sentiment esclusi (nessuno storico point-in-time);
- survivorship: l'universo è quello attuale (titoli delistati assenti) -> dichiarato nei risultati;
- prezzi: OHLC rettificati per split dal provider; dividendi non inclusi nel P&L (stima prudente per posizioni long).
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.market_data.registry import get_market_data_adapter
from app.backtest import metrics as M
from app.backtest.engine import ENGINE_VERSION, BTParams, MarketData, Simulator, prepare, regime_series
from app.core.config import get_settings
from app.models.entities import BacktestRun, Job, ModelEvaluation, Strategy, StrategyVersion
from app.services import audit, jobs
from app.services.market_data import ensure_fx_instrument, fetch_fx, find_instrument, latest_fx, sync_bars
from app.services.scanner import active_universe, bars_frame

MIN_TRADES_CALIBRATION = 30
DEMO_REGIME = ["DEMO5.DEMO", "DEMO4.DEMO"]


# ------------------------------------------------------------------ dati
def _closes(db: Session, ticker: str, end: date, days: int) -> pd.Series | None:
    inst = find_instrument(db, ticker)
    if inst is None:
        return None
    df = bars_frame(db, inst.id, end, days)
    return df["adj_close"].fillna(df["close"]) if len(df) else None


def _fx_series(db: Session, base: str, ccy: str, end: date, days: int, demo: bool, notes: list[str]) -> pd.Series | None:
    """Cambio storico (unità di valuta per 1 unità di base): dal database, altrimenti scaricato dal provider;
    se lo storico non esiste, ultimo cambio noto costante (stima dichiarata nelle note)."""
    c = _closes(db, f"{base}{ccy}.FOREX", end, days)
    if c is not None and len(c) > 250:
        return c
    adapter = get_market_data_adapter()
    try:
        inst = ensure_fx_instrument(db, base, ccy, adapter.info().name, adapter.is_demo)
        sync_bars(db, adapter, inst, end - timedelta(days=days), end)
        db.commit()
        c = _closes(db, f"{base}{ccy}.FOREX", end, days)
        if c is not None and len(c) > 250:
            return c
    except Exception:  # noqa: BLE001 - si passa al cambio costante
        db.rollback()
    q = latest_fx(db, base, ccy)
    if q is None:
        try:
            q = fetch_fx(db, adapter, base, ccy)
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
            q = None
    if q is None:
        notes.append(f"Cambio {base}/{ccy} non disponibile: strumenti in {ccy} esclusi")
        return None
    notes.append(f"Storico del cambio {base}/{ccy} non disponibile: usato il cambio costante {q.price} "
                 f"({q.source}{', DEMO' if q.is_demo or demo else ''}). Il rischio di cambio NON è simulato.")
    idx = pd.bdate_range(end - timedelta(days=days), end)
    return pd.Series(float(q.price), index=idx)


def load_market_data(db: Session, tickers: list[str] | None, end: date, years: int, base: str = "EUR") -> tuple[MarketData, list[str]]:
    s = get_settings()
    days = int(365 * years) + 400  # margine per il riscaldamento degli indicatori
    tickers = tickers or active_universe(db)
    found, missing = [], []
    for t in tickers:
        inst = find_instrument(db, t)
        (found if inst is not None else missing).append(inst or t)
    demo = any(i.is_demo for i in found)
    regime_t = DEMO_REGIME if demo else list(s.regime_benchmarks)
    bench = {t: c for t in regime_t if (c := _closes(db, t, end, days)) is not None and len(c)}
    rs_by_region = {"DEMO": _closes(db, "DEMO5.DEMO", end, days)} if demo else \
        {k: _closes(db, v, end, days) for k, v in s.rs_benchmark_by_region.items()}
    fx: dict[str, pd.Series] = {}
    notes: list[str] = []
    rows = []
    for inst in found:
        if inst.instrument_type == "FX":
            continue
        df = bars_frame(db, inst.id, end, days)
        if len(df) < 300:
            missing.append(f"{inst.symbol}.{inst.exchange} (storico insufficiente)")
            continue
        if inst.currency != base and inst.currency not in fx:
            c = _fx_series(db, base, inst.currency, end, days, demo, notes)
            if c is not None:
                fx[inst.currency] = c
        region = "DEMO" if demo else ("US" if inst.exchange == "US" else "EU")
        rows.append((inst.id, f"{inst.symbol}.{inst.exchange}", inst.sector, inst.currency, df, rs_by_region.get(region)))
    if not bench:
        notes.append("Benchmark del regime senza storico: regime UNKNOWN, fattore macro escluso")
    md = prepare(rows, bench, fx, base, demo, notes)
    return md, [str(m) for m in missing]


def download_history(db: Session, tickers: list[str], years: int) -> dict:
    adapter = get_market_data_adapter()
    end = datetime.now(UTC).date()
    added, errors = 0, []
    for t in tickers:
        inst = find_instrument(db, t)
        if inst is None:
            continue
        try:
            added += sync_bars(db, adapter, inst, end - timedelta(days=365 * years + 400), end)
            db.commit()
        except Exception as e:  # noqa: BLE001 - un ticker non blocca il backtest
            db.rollback()
            errors.append({"ticker": t, "error": str(e)[:200]})
    return {"bars_added": added, "errors": errors[:50]}


# ------------------------------------------------------------------ segmenti e walk-forward
def _segment(res: dict, start, end) -> dict:
    eq = res["equity"].loc[start:end]
    ex = res["exposure"].loc[start:end]
    tr = [t for t in res["trades"] if str(start.date()) <= t["entry_date"] <= str(end.date())]
    notional = sum(e["qty"] * e["price"] for t in tr for e in t["exits"]) + sum(t["qty"] * t["entry_price"] for t in tr)
    return M.equity_metrics(eq, ex, notional) | M.trade_metrics(tr)


def walk_forward(sim: Simulator, dates: pd.DatetimeIndex, base: BTParams, train_days: int, test_days: int,
                 grid: list[float]) -> dict:
    windows, eq_parts, trades = [], [], []
    capital = base.initial_capital
    s = 0
    while s + train_days + 5 < len(dates):
        tr_d = dates[s:s + train_days]
        te_d = dates[s + train_days:s + train_days + test_days]
        if len(te_d) < 5:
            break
        best, best_sh, scores = base.min_score, -math.inf, {}
        for ms in grid:
            sim.p = replace(base, min_score=ms)
            r = sim.run(tr_d[0], tr_d[-1])
            m = M.equity_metrics(r["equity"])
            n = len(r["trades"])
            sh = m.get("sharpe") if n >= 5 and m.get("sharpe") is not None else -math.inf
            scores[str(ms)] = {"sharpe": m.get("sharpe"), "trades": n}
            if sh > best_sh:
                best, best_sh = ms, sh
        sim.p = replace(base, min_score=best, initial_capital=capital)
        r = sim.run(te_d[0], te_d[-1])
        if len(r["equity"]):
            capital = float(r["equity"].iloc[-1])
            eq_parts.append(r["equity"])
        trades += r["trades"]
        windows.append({"train": [str(tr_d[0].date()), str(tr_d[-1].date())], "test": [str(te_d[0].date()), str(te_d[-1].date())],
                        "chosen_min_score": best, "train_scores": scores,
                        "result": M.equity_metrics(r["equity"]) | {"trades": len(r["trades"])}})
        s += test_days
    sim.p = base
    eq = pd.concat(eq_parts) if eq_parts else pd.Series(dtype=float)
    return {"windows": windows, "equity": eq, "trades": trades,
            "metrics": (M.equity_metrics(eq) | M.trade_metrics(trades)) if len(eq) else {"trades": 0}}


def calibration(trades: list[dict], is_demo: bool, min_trades: int = MIN_TRADES_CALIBRATION) -> dict:
    """Probabilità e valore atteso SOLO da operazioni fuori campione (walk-forward), con intervallo di Wilson."""
    def block(ts):
        closed = [t for t in ts if t.get("r_multiple") is not None]
        n = len(closed)
        if n == 0:
            return {"trades": 0, "validated": False}
        wins = [t["r_multiple"] for t in closed if t["pnl_base"] > 0]
        losses = [t["r_multiple"] for t in closed if t["pnl_base"] <= 0]
        p = len(wins) / n
        lo, hi = M.wilson(len(wins), n)
        aw = sum(wins) / len(wins) if wins else 0.0
        al = sum(losses) / len(losses) if losses else 0.0
        ev = p * aw + (1 - p) * al
        ev_lo = lo * aw + (1 - lo) * al
        ev_hi = hi * aw + (1 - hi) * al
        return {"trades": n, "win_rate": round(p, 4), "win_rate_ci95": [round(lo, 4), round(hi, 4)],
                "avg_win_r": round(aw, 3), "avg_loss_r": round(al, 3), "ev_r": round(ev, 3),
                "ev_r_ci95": [round(min(ev_lo, ev_hi), 3), round(max(ev_lo, ev_hi), 3)],
                "validated": n >= min_trades and not is_demo}
    by_setup = {}
    for t in trades:
        by_setup.setdefault(t["setup"], []).append(t)
    return {"source": "walk_forward_out_of_sample", "min_trades": min_trades, "is_demo": is_demo,
            "overall": block(trades), "by_setup": {k: block(v) for k, v in by_setup.items()},
            "note": "Costi inclusi nei multipli R. Dati DEMO o meno di min_trades operazioni: non validato."}


# ------------------------------------------------------------------ esecuzione completa
def _series(s: pd.Series, n: int = 600) -> list[list]:
    if not len(s):
        return []
    step = max(1, math.ceil(len(s) / n))
    s2 = s.iloc[::step]
    if s2.index[-1] != s.index[-1]:
        s2 = pd.concat([s2, s.iloc[-1:]])
    return [[str(d.date()), round(float(v), 2)] for d, v in s2.items()]


def execute(md: MarketData, params: BTParams, *, start: date | None, end: date | None, train_days: int,
            test_days: int, splits=(0.6, 0.2, 0.2)) -> dict:
    cal = md.calendar()
    if len(cal) < 320 or not md.instruments:
        raise ValueError("Storico insufficiente: servono almeno ~320 giorni di borsa (260 di riscaldamento indicatori)")
    eff = cal[260:]
    if start:
        eff = eff[eff >= pd.Timestamp(start)]
    if end:
        eff = eff[eff <= pd.Timestamp(end)]
    if len(eff) < 60:
        raise ValueError("Periodo di test troppo breve dopo il riscaldamento degli indicatori (minimo 60 giorni)")
    regimes = regime_series(md, cal)
    sim = Simulator(md, params, regimes)
    full = sim.run(eff[0], eff[-1])
    n = len(eff)
    a, b = int(n * splits[0]), int(n * (splits[0] + splits[1]))
    seg_bounds = {"train": (eff[0], eff[a - 1]), "validation": (eff[a], eff[b - 1]), "test": (eff[b], eff[-1])}
    segments = {k: _segment(full, *v) for k, v in seg_bounds.items()}
    segments["full"] = M.equity_metrics(full["equity"], full["exposure"], full["traded_notional"]) | M.trade_metrics(full["trades"])

    td = min(train_days, max(60, n // 3))
    grid = sorted({max(0.0, params.min_score - 10), params.min_score, min(100.0, params.min_score + 10)})
    wf = walk_forward(sim, eff, params, td, test_days, grid)

    stress = {}
    for name, p2 in {"costi_x2": replace(params, commission_per_order=params.commission_per_order * 2,
                                         commission_pct=params.commission_pct * 2, slippage_bps=params.slippage_bps * 2),
                     "slippage_x3": replace(params, slippage_bps=params.slippage_bps * 3),
                     "gap_stop_1pct": replace(params, extra_gap_pct=1.0)}.items():
        s2 = Simulator(md, p2, regimes)
        r = s2.run(eff[0], eff[-1])
        m = M.equity_metrics(r["equity"]) | M.trade_metrics(r["trades"])
        stress[name] = {k: m.get(k) for k in ("net_return_pct", "cagr_pct", "max_drawdown_pct", "sharpe", "profit_factor",
                                              "expectancy_base", "trades")}

    rets = full["equity"].pct_change().dropna()
    reg_days = regimes.reindex(rets.index).fillna("UNKNOWN")
    by_regime_days = {k: {"days": int(len(v)), "annualized_return_pct": round(float(v.mean() * 252 * 100), 2)}
                      for k, v in rets.groupby(reg_days)}

    bench = {}
    for t, c in md.regime_closes.items():
        bm = M.benchmark_metrics(c, full["equity"].index, params.initial_capital)
        if bm:
            bench[t] = {k: bm.get(k) for k in ("net_return_pct", "cagr_pct", "max_drawdown_pct", "sharpe", "sortino", "volatility_pct")}
    bench_curve = {}
    if md.regime_closes:
        t0, c0 = next(iter(md.regime_closes.items()))
        c = c0.reindex(full["equity"].index).ffill().dropna()
        if len(c):
            bench_curve = {"ticker": t0, "series": _series(c / c.iloc[0] * params.initial_capital)}

    dd = full["equity"] / full["equity"].cummax() - 1
    return {
        "engine_version": ENGINE_VERSION, "period": [str(eff[0].date()), str(eff[-1].date())],
        "split_bounds": {k: [str(v[0].date()), str(v[1].date())] for k, v in seg_bounds.items()},
        "segments": segments, "walk_forward": {"train_days": td, "test_days": test_days, "grid_min_score": grid,
                                               "windows": wf["windows"], "metrics": wf["metrics"],
                                               "equity": _series(wf["equity"])},
        "calibration": calibration(wf["trades"], md.is_demo),
        "bootstrap": {"full": M.bootstrap(full["trades"]), "walk_forward": M.bootstrap(wf["trades"])},
        "stress": stress, "by_regime_trades": M.by_group(full["trades"], "regime"),
        "by_regime_days": by_regime_days, "by_setup": M.by_group(full["trades"], "setup"),
        "benchmarks": bench, "benchmark_curve": bench_curve,
        "equity": _series(full["equity"]), "drawdown": _series(dd * 100),
        "trades": full["trades"][-500:], "trades_total": len(full["trades"]),
        "universe_size": len(md.instruments), "is_demo": md.is_demo,
        "bias_controls": {
            "look_ahead": "Indicatori causali, segnali in chiusura, esecuzione dal giorno successivo, regime point-in-time.",
            "survivorship": "NON controllato: universo attuale senza titoli delistati. Risultati probabilmente ottimistici.",
            "fundamentals": "Esclusi dal punteggio del backtest: nessuno storico point-in-time disponibile.",
            "adjustments": "Prezzi rettificati per split dal provider; dividendi esclusi dal P&L.",
            "execution": "Stop prima dei target nello stesso giorno; gap sotto lo stop eseguiti all'apertura; limiti eseguiti solo se toccati.",
        },
        "notes": full["notes"],
        "disclaimer": "Risultati simulati su dati storici: non garantiscono né promettono rendimenti futuri.",
    }


def _strategy_version(db: Session, params: BTParams) -> StrategyVersion:
    st = db.execute(select(Strategy).where(Strategy.name == "Backtest tecnico")).scalar_one_or_none()
    if st is None:
        st = Strategy(name="Backtest tecnico", style="MULTI",
                      description="Ranking tecnico point-in-time + Trade Planner + Risk Engine (Fase 5)")
        db.add(st)
        db.flush()
    code = hashlib.sha256(json.dumps(params.to_dict(), sort_keys=True).encode()).hexdigest()
    sv = db.execute(select(StrategyVersion).where(StrategyVersion.strategy_id == st.id,
                                                  StrategyVersion.code_hash == code)).scalar_one_or_none()
    if sv is None:
        sv = StrategyVersion(strategy_id=st.id, version=f"{ENGINE_VERSION}#{code[:8]}", parameters=params.to_dict(),
                             code_hash=code, is_validated=False)
        db.add(sv)
        db.flush()
    return sv


def enqueue_backtest(db: Session, body: dict, actor: str) -> BacktestRun:
    params = BTParams(**{k: v for k, v in (body.get("params") or {}).items() if k in BTParams.__dataclass_fields__})
    run = BacktestRun(name=body.get("name") or f"Backtest {datetime.now(UTC):%Y-%m-%d %H:%M}", status="QUEUED",
                      engine_version=ENGINE_VERSION, params={**body, "params": params.to_dict()},
                      universe=body.get("tickers") or [], requested_by=actor)
    db.add(run)
    db.flush()
    job, _ = jobs.enqueue(db, "backtest", {"run_id": run.id}, actor)
    run.job_id = job.id
    audit.record(db, actor=actor, action="backtest.requested", entity="backtest_run", entity_id=run.id,
                 payload={"params": params.to_dict(), "tickers": run.universe})
    return run


@jobs.handler("backtest")
def _backtest_job(db: Session, job: Job) -> dict:
    run = db.get(BacktestRun, job.payload["run_id"])
    run.status = "RUNNING"
    db.commit()
    body = run.params
    try:
        params = BTParams(**{k: v for k, v in body["params"].items() if k in BTParams.__dataclass_fields__})
        if isinstance(params.setups, list):
            params = replace(params, setups=tuple(params.setups))
        end = date.fromisoformat(body["end"]) if body.get("end") else datetime.now(UTC).date()
        start = date.fromisoformat(body["start"]) if body.get("start") else None
        years = max(1, math.ceil(((end - start).days / 365) if start else 3))
        tickers = body.get("tickers") or None
        dl = None
        if int(body.get("download_years") or 0) > 0:
            dl = download_history(db, tickers or active_universe(db), int(body["download_years"]))
            years = max(years, int(body["download_years"]))
        md, missing = load_market_data(db, tickers, end, years + 1)
        res = execute(md, params, start=start, end=end, train_days=int(body.get("train_days") or 252),
                      test_days=int(body.get("test_days") or 63))
        res["missing"] = missing
        res["download"] = dl
        sv = _strategy_version(db, params)
        run = db.get(BacktestRun, run.id)
        run.strategy_version_id, run.results, run.is_demo = sv.id, json.loads(json.dumps(res, default=str)), md.is_demo
        run.universe = sorted(i.ticker for i in md.instruments.values())
        run.period_start, run.period_end = date.fromisoformat(res["period"][0]), date.fromisoformat(res["period"][1])
        seg = res["segments"]
        run.summary = {k: seg["full"].get(k) for k in ("net_return_pct", "cagr_pct", "max_drawdown_pct", "sharpe", "sortino",
                                                        "profit_factor", "win_rate_pct", "trades", "expectancy_base")} | {
            "oos_walk_forward": {k: res["walk_forward"]["metrics"].get(k) for k in
                                 ("net_return_pct", "cagr_pct", "max_drawdown_pct", "sharpe", "trades", "profit_factor")},
            "test": {k: seg["test"].get(k) for k in ("net_return_pct", "max_drawdown_pct", "sharpe", "trades")},
            "calibration_validated": res["calibration"]["overall"].get("validated", False)}
        for etype, key, oos in (("BACKTEST_TRAIN", "train", False), ("BACKTEST_VALIDATION", "validation", False),
                                ("BACKTEST_TEST", "test", True)):
            b = res["split_bounds"][key]
            db.add(ModelEvaluation(strategy_version_id=sv.id, evaluation_type=etype, period_start=date.fromisoformat(b[0]),
                                   period_end=date.fromisoformat(b[1]), is_out_of_sample=oos,
                                   metrics=seg[key] | {"backtest_run_id": run.id, "is_demo": md.is_demo}))
        wfw = res["walk_forward"]["windows"]
        if wfw:
            db.add(ModelEvaluation(strategy_version_id=sv.id, evaluation_type="WALK_FORWARD",
                                   period_start=date.fromisoformat(wfw[0]["test"][0]), period_end=date.fromisoformat(wfw[-1]["test"][1]),
                                   is_out_of_sample=True, metrics=json.loads(json.dumps(res["walk_forward"]["metrics"], default=str)) | {
                                       "calibration": res["calibration"], "backtest_run_id": run.id, "is_demo": md.is_demo}))
        run.status, run.finished_at = "DONE", datetime.now(UTC)
        audit.record(db, actor=job.requested_by, action="backtest.completed", entity="backtest_run", entity_id=run.id,
                     payload={"summary": run.summary, "strategy_version_id": sv.id, "is_demo": md.is_demo})
        db.commit()
        return {"run_id": run.id, "trades": res["trades_total"]}
    except Exception as e:
        db.rollback()
        run = db.get(BacktestRun, job.payload["run_id"])
        run.status, run.error, run.finished_at = "FAILED", f"{type(e).__name__}: {str(e)[:400]}", datetime.now(UTC)
        db.commit()
        raise


def latest_calibration(db: Session) -> tuple[ModelEvaluation, dict] | None:
    """Ultima calibrazione walk-forward validata (dati reali, operazioni sufficienti)."""
    for ev in db.execute(select(ModelEvaluation).where(ModelEvaluation.evaluation_type == "WALK_FORWARD",
                                                       ModelEvaluation.is_out_of_sample)
                         .order_by(ModelEvaluation.created_at.desc()).limit(20)).scalars():
        cal = (ev.metrics or {}).get("calibration") or {}
        if cal.get("overall", {}).get("validated") and not cal.get("is_demo"):
            return ev, cal
    return None

