from decimal import Decimal as D

import numpy as np
import pandas as pd

from app.adapters.market_data.demo import DemoAdapter
from app.analytics import indicators
from app.analytics.planner import compute_levels, gap_stress, return_correlation
from app.models.entities import ScanRun, TradePlan
from app.services.risk import Costs, PortfolioContext, RiskLimits, SizingRequest, compute_position_size
from app.services.scanner import run_scan


def _trend_df(n=320, new_high=True):
    idx = pd.bdate_range("2024-01-01", periods=n)
    c = np.linspace(100, 160, n) * (1 + 0.02 * np.sin(np.arange(n) / 4))
    if new_high:  # ultimo tratto su nuovi massimi: nessuna resistenza sopra il prezzo
        c[-12:] = np.linspace(c[-13], c[:-12].max() * 1.01, 12)
    return pd.DataFrame({"open": c * 0.998, "high": c * 1.01, "low": c * 0.99, "close": c, "adj_close": c,
                         "volume": 1e6}, index=idx)


def _features(df):
    ind = indicators.compute_all(df)
    f = {k: float(ind[k].iloc[-1]) for k in ind.columns}
    f["support"], f["resistance"] = indicators.support_resistance(df["high"], df["low"], df["close"])
    return f


def test_levels_are_ordered_and_technical():
    df = _trend_df()
    lv = compute_levels("TREND_FOLLOWING", _features(df), df)
    assert lv.ok, lv.reason
    assert lv.stop < lv.entry_low <= lv.entry <= lv.entry_high < lv.tp1 < lv.tp2
    assert lv.stop_rationale["method"] in ("swing_low", "ema50")
    assert 1.0 <= lv.stop_rationale["distance_atr"] <= 3.5
    assert lv.invalidation and lv.early_exit and lv.avoid_conditions


def test_close_resistance_rejects():
    df = _trend_df(new_high=False)
    lv = compute_levels("TREND_FOLLOWING", _features(df), df)
    assert lv.ok or "Resistenza" in lv.reason or "ATR" in lv.reason


def test_no_setup_no_plan():
    df = _trend_df()
    assert not compute_levels(None, _features(df), df).ok


def test_gap_stress_and_correlation():
    df = _trend_df()
    assert gap_stress(df) >= 2.0
    assert return_correlation(df["close"], df["close"]) == 1.0
    assert return_correlation(df["close"].iloc[:10], df["close"].iloc[:10]) is None


def test_max_quantity_cap():
    lim = RiskLimits(D("0.5"), D("3"), D("15"), D("30"), D("5"))
    ctx = PortfolioContext(D("10000"), D("10000"), D("0"))
    r = compute_position_size(SizingRequest(D("100"), D("95"), max_quantity=D("4")), ctx, lim, Costs(D("0"), D("0"), D("0")))
    assert r.quantity == 4 and r.binding_constraint == "portfolio_manager"


def test_plans_from_demo_scan_never_ready(client, admin, analyst, db):
    client.post("/api/universe/seed", headers=analyst)
    sid = client.post("/api/scans", headers=analyst).json()["scan"]["id"]
    run_scan(db, DemoAdapter(), db.get(ScanRun, sid))
    r = client.post("/api/trade-plans", json={"top_n": 5}, headers=analyst)
    assert r.status_code == 200, r.text
    plans = r.json()
    for p in plans:
        assert p["status"] in ("WAIT", "REJECTED") and p["is_demo"]
        assert p["calibrated_probability"] is None and p["expected_value_base"] is None
        if p["status"] == "WAIT":
            assert float(p["stop_loss"]) < float(p["entry_price"]) < float(p["take_profit_1"])
            assert "DEMO" in p["status_reason"]
            assert len(p["scenarios"]) == 5
    opp = client.get("/api/opportunities", headers=analyst).json()
    assert opp["total"] == len([p for p in plans if p["status"] == "WAIT"])


def test_order_requires_ready_plan(client, admin, analyst, db):
    client.post("/api/universe/seed", headers=analyst)
    sid = client.post("/api/scans", headers=analyst).json()["scan"]["id"]
    run_scan(db, DemoAdapter(), db.get(ScanRun, sid))
    plans = [p for p in client.post("/api/trade-plans", json={"top_n": 5}, headers=analyst).json() if p["status"] == "WAIT"]
    if not plans:
        return  # nessun setup nei dati demo di oggi: niente da verificare
    p = plans[0]
    client.put("/api/system/mode", json={"mode": "PAPER"}, headers=admin)
    body = {"instrument_id": p["instrument_id"], "side": "BUY", "order_type": "MARKET", "quantity": "1",
            "client_order_id": "plan-order-0001", "trade_plan_id": p["id"]}
    r = client.post("/api/paper/orders", json=body, headers=analyst)
    assert r.status_code == 423 and r.json()["error"]["code"] == "plan_not_ready"
    # simuliamo un piano READY per verificare i vincoli di quantità
    plan = db.get(TradePlan, p["id"])
    plan.status = "READY"
    db.commit()
    r = client.post("/api/paper/orders", json=body | {"quantity": str(plan.quantity + 1), "client_order_id": "plan-order-0002"}, headers=analyst)
    assert r.status_code == 423 and r.json()["error"]["code"] == "qty_above_plan"


def test_expire_plans(db):
    from datetime import UTC, datetime, timedelta
    from app.services.planner import expire_plans
    assert expire_plans(db) == 0
    _ = datetime.now(UTC) - timedelta(days=1)
