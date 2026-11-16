import math
from datetime import date

import pandas as pd
import pytest
from sqlalchemy import select

from app.adapters.market_data.demo import DemoAdapter
from app.backtest import metrics as M
from app.backtest.engine import BTParams, Simulator, prepare, regime_series
from app.backtest.runner import calibration, execute, latest_calibration
from app.models.entities import BacktestRun, ModelEvaluation, Strategy, StrategyVersion
from app.services import jobs

TICKERS = ["DEMO1", "DEMO2", "DEMO3", "DEMO4", "DEMO5"]


def _df(symbol: str, start: date, end: date) -> pd.DataFrame:
    s = DemoAdapter().get_daily_bars(symbol, "DEMO", start, end)
    return pd.DataFrame([{"date": pd.Timestamp(b.bar_date), "open": float(b.open), "high": float(b.high), "low": float(b.low),
                          "close": float(b.close), "adj_close": float(b.adj_close), "volume": float(b.volume)}
                         for b in s.bars]).set_index("date")


def _md(end=date(2024, 12, 31), mutate_after: pd.Timestamp | None = None):
    frames = {t: _df(t, date(2021, 1, 1), end) for t in TICKERS}
    if mutate_after is not None:  # dati "futuri" alterati: non devono influenzare il passato
        for df in frames.values():
            m = df.index > mutate_after
            df.loc[m, ["open", "high", "low", "close", "adj_close"]] *= 0.7
    rows = [(i, f"{t}.DEMO", f"S{i % 2}", "EUR", frames[t], frames["DEMO5"]["close"]) for i, t in enumerate(TICKERS, 1)]
    bench = {"DEMO5.DEMO": frames["DEMO5"]["close"], "DEMO4.DEMO": frames["DEMO4"]["close"]}
    return prepare(rows, bench, {}, "EUR", True)


@pytest.fixture(scope="module")
def md():
    return _md()


def test_wilson_and_metrics():
    lo, hi = M.wilson(30, 60)
    assert lo < 0.5 < hi and round(lo, 3) == 0.377
    idx = pd.bdate_range("2024-01-01", periods=253)
    eq = pd.Series([100 * 1.0004 ** i for i in range(253)], index=idx)
    m = M.equity_metrics(eq)
    assert abs(m["cagr_pct"] - (1.0004 ** 252 - 1) * 100) < 0.05 and m["max_drawdown_pct"] == 0
    t = [{"exit_date": "x", "pnl_base": v, "r_multiple": v / 10, "holding_days": 3, "exit_reason": "stop"} for v in (10, -5, 20, -5)]
    tm = M.trade_metrics(t)
    assert tm["profit_factor"] == 3.0 and tm["win_rate_pct"] == 50.0 and tm["expectancy_base"] == 5.0
    b = M.bootstrap(t * 5)
    assert b["available"] and b["expectancy_base_p2_5_p50_p97_5"][0] <= 5.0 <= b["expectancy_base_p2_5_p50_p97_5"][2]


def test_no_look_ahead(md):
    cut = pd.Timestamp("2024-06-28")
    md2 = _md(mutate_after=cut)
    start = md.calendar()[300]
    r1 = Simulator(md, BTParams(min_score=40), regime_series(md, md.calendar())).run(start, cut)
    r2 = Simulator(md2, BTParams(min_score=40), regime_series(md2, md2.calendar())).run(start, cut)
    assert r1["trades"] and r1["trades"] == r2["trades"]
    pd.testing.assert_series_equal(r1["equity"], r2["equity"])


def test_simulation_invariants(md):
    sim = Simulator(md, BTParams(min_score=40), regime_series(md, md.calendar()))
    r = sim.run(md.calendar()[300], None)
    assert r["trades"]
    for t in r["trades"]:
        assert t["entry_date"] <= t["exit_date"] and t["stop"] < t["entry_price"] < t["tp1"]
        assert t["fees_base"] >= 4.0  # almeno una commissione di ingresso e una di uscita
        # rischio iniziale entro il budget (0,5% di ~10.000 €), salvo gap oltre lo stop
        if t["exit_reason"] == "stop":
            assert -t["pnl_base"] <= 10000 * 0.005 * 1.25
    assert r["equity"].min() > 0 and (r["exposure"] <= 1.0001).all()
    # stress: costi più alti non migliorano il risultato
    hi = Simulator(md, BTParams(min_score=40, slippage_bps=50, commission_per_order=10), sim.regimes).run(md.calendar()[300], None)
    assert hi["equity"].iloc[-1] <= r["equity"].iloc[-1]


def test_execute_full_report(md):
    res = execute(md, BTParams(min_score=45), start=None, end=None, train_days=150, test_days=40)
    assert set(res["segments"]) == {"train", "validation", "test", "full"}
    b = res["split_bounds"]
    assert b["train"][1] < b["validation"][0] <= b["validation"][1] < b["test"][0]
    assert res["walk_forward"]["windows"] and all(w["train"][1] < w["test"][0] for w in res["walk_forward"]["windows"])
    assert set(res["stress"]) == {"costi_x2", "slippage_x3", "gap_stop_1pct"}
    assert res["calibration"]["overall"]["validated"] is False  # dati DEMO: mai validato
    assert "survivorship" in res["bias_controls"] and res["equity"] and res["benchmarks"]


def test_calibration_rules():
    tr = [{"setup": "PULLBACK", "pnl_base": 10.0 if i % 2 else -5.0, "r_multiple": 1.0 if i % 2 else -0.5} for i in range(40)]
    c = calibration(tr, is_demo=False)
    assert c["overall"]["validated"] and c["overall"]["win_rate"] == 0.5 and c["overall"]["ev_r"] == 0.25
    lo, hi = c["overall"]["win_rate_ci95"]
    assert lo < 0.5 < hi
    assert not calibration(tr, is_demo=True)["overall"]["validated"]
    assert not calibration(tr[:10], is_demo=False)["overall"]["validated"]


def test_latest_calibration_ignores_demo(db):
    st = Strategy(name="x", style="MULTI")
    db.add(st)
    db.flush()
    sv = StrategyVersion(strategy_id=st.id, version="v", parameters={})
    db.add(sv)
    db.flush()
    tr = [{"setup": "PULLBACK", "pnl_base": 1.0, "r_multiple": 1.0}] * 40
    db.add(ModelEvaluation(strategy_version_id=sv.id, evaluation_type="WALK_FORWARD", period_start=date(2024, 1, 1),
                           period_end=date(2024, 6, 1), is_out_of_sample=True, metrics={"calibration": calibration(tr, True)}))
    db.flush()
    assert latest_calibration(db) is None
    db.add(ModelEvaluation(strategy_version_id=sv.id, evaluation_type="WALK_FORWARD", period_start=date(2024, 1, 1),
                           period_end=date(2024, 6, 1), is_out_of_sample=True, metrics={"calibration": calibration(tr, False)}))
    db.flush()
    ev, cal = latest_calibration(db)
    assert cal["overall"]["validated"] and not cal["is_demo"]


def test_backtest_api_job(client, analyst, viewer, db):
    from app.adapters.market_data.registry import get_market_data_adapter
    from app.services.market_data import sync_ticker
    for t in TICKERS:
        sync_ticker(db, get_market_data_adapter(), f"{t}.DEMO", years=3)
    db.commit()
    body = {"name": "test", "tickers": [f"{t}.DEMO" for t in TICKERS], "params": {"min_score": 45}, "train_days": 120, "test_days": 40}
    assert client.post("/api/backtests", json=body, headers=viewer).status_code == 403
    assert client.post("/api/backtests", json=body | {"params": {"setups": ["XX"]}}, headers=analyst).status_code == 422
    r = client.post("/api/backtests", json=body, headers=analyst)
    assert r.status_code == 202 and r.json()["status"] == "QUEUED"
    rid = r.json()["id"]
    job = jobs.run_one(db)
    assert job.status == "DONE", job.error
    d = client.get(f"/api/backtests/{rid}", headers=analyst).json()
    assert d["status"] == "DONE" and d["is_demo"] and d["universe_size"] == 5
    assert d["summary"]["calibration_validated"] is False and d["results"]["segments"]["full"]["days"] > 0
    evs = db.execute(select(ModelEvaluation)).scalars().all()
    assert {e.evaluation_type for e in evs} >= {"BACKTEST_TRAIN", "BACKTEST_VALIDATION", "BACKTEST_TEST"}
    assert client.get("/api/backtests", headers=viewer).json()["total"] == 1
    assert db.execute(select(BacktestRun)).scalar_one().strategy_version_id is not None
    assert not math.isnan(d["results"]["segments"]["full"]["net_return_pct"])
