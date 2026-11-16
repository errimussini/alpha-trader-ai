"""Fase 7: esiti prospettici, deriva rispetto al backtest, benchmark, affidabilità dati e segnali."""
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D

from sqlalchemy import select

from app.models.entities import (
    EquitySnapshot, HistoricalBar, Instrument, ModelEvaluation, PlanOutcome, RiskAssessment, ScanRun, Strategy,
    StrategyVersion, TradePlan, TradingSignal, User,
)
from app.services import jobs
from app.services.portfolio import get_or_create_paper_portfolio
from app.validation import service as V
from app.validation.outcomes import Bar, PlanSpec, evaluate

T0 = date(2026, 3, 2)  # lunedì
COSTS = {"commission_per_order": D("2"), "slippage_bps": D("5")}


def _spec(**kw):
    base = dict(created_for=T0, valid_until=T0 + timedelta(days=5), entry=D("100"), stop=D("95"), tp1=D("110"),
                horizon_days=10, quantity=D("10"))
    return PlanSpec(**(base | kw))


def _b(i, o, h, lo, c):
    return Bar(T0 + timedelta(days=i), D(str(o)), D(str(h)), D(str(lo)), D(str(c)))


def test_win_loss_gap_and_costs():
    win = evaluate(_spec(), [_b(1, 101, 102, 99.5, 101), _b(2, 103, 111, 102, 110)], today=T0 + timedelta(days=9), **COSTS)
    assert win.outcome == "WIN" and win.entry_price == D("100") and win.exit_price == D("110")
    assert win.r_gross == D("2.0000") and win.r_net < win.r_gross  # i costi riducono l'R
    gap = evaluate(_spec(), [_b(1, 100.5, 101, 99, 100), _b(2, 92, 93, 90, 91)], today=T0 + timedelta(days=9), **COSTS)
    assert gap.outcome == "LOSS" and gap.exit_price == D("92") and gap.r_gross == D("-1.6000")  # gap oltre lo stop


def test_conservative_same_bar_and_no_look_ahead():
    r = evaluate(_spec(), [_b(0, 50, 200, 10, 100),  # barra del giorno di creazione: ignorata
                           _b(1, 101, 112, 94, 100)], today=T0 + timedelta(days=9), **COSTS)
    assert r.outcome == "LOSS" and any("stessa barra" in n for n in r.notes)
    assert r.entry_date == T0 + timedelta(days=1)


def test_not_triggered_invalidated_timeout_pending():
    nt = evaluate(_spec(), [_b(i, 104, 106, 103, 105) for i in range(1, 8)], today=T0 + timedelta(days=8), **COSTS)
    assert nt.outcome == "NOT_TRIGGERED"
    inv = evaluate(_spec(), [_b(1, 101, 102, 100.5, 101), _b(2, 94, 95, 93, 94)], today=T0 + timedelta(days=3), **COSTS)
    assert inv.outcome == "INVALIDATED"
    to = evaluate(_spec(horizon_days=3), [_b(1, 100, 101, 99, 100), _b(2, 100, 102, 99, 101), _b(3, 101, 103, 100, 102)],
                  today=T0 + timedelta(days=4), **COSTS)
    assert to.outcome == "TIMEOUT" and to.exit_price == D("102") and to.r_gross == D("0.4000")
    pend = evaluate(_spec(), [_b(1, 100, 101, 99, 100)], today=T0 + timedelta(days=2), **COSTS)
    assert pend.outcome == "PENDING" and pend.entry_date is not None and pend.r_net is None
    bad = evaluate(_spec(stop=D("100")), [_b(1, 100, 101, 99, 100)], today=T0 + timedelta(days=2), **COSTS)
    assert bad.outcome == "NOT_EVALUABLE"
    wait = evaluate(_spec(), [_b(1, 104, 106, 103, 105)], today=T0 + timedelta(days=2), **COSTS)
    assert wait.outcome == "PENDING" and wait.entry_date is None


def _world(db, n_plans=1, demo=False, status="READY"):
    u = User(email="val@example.com", hashed_password="x", role="analyst")
    db.add(u)
    db.flush()
    pf = get_or_create_paper_portfolio(db, u)
    inst = Instrument(symbol="VAL", exchange="US", name="Val", currency="EUR", data_source="test", is_demo=demo)
    db.add(inst)
    db.flush()
    plans = []
    for k in range(n_plans):
        ra = RiskAssessment(portfolio_id=pf.id, approved=True, inputs={}, result={}, blocking_reasons=[], engine_version="t")
        db.add(ra)
        db.flush()
        pt = datetime.combine(T0, datetime.min.time(), tzinfo=UTC) + timedelta(hours=20, minutes=k)
        p = TradePlan(instrument_id=inst.id, portfolio_id=pf.id, risk_assessment_id=ra.id, model_version="m/1+p/1",
                      strategy_style="SWING", setup="PULLBACK", score=D("75"), price_reference=D("100"), price_source="t",
                      price_time=pt, entry_price=D("100"), stop_loss=D("95"), take_profit_1=D("110"),
                      quantity=D("10"), notional_base=D("1000"), est_loss_at_stop_base=D("50"), horizon_days=10,
                      status=status, valid_until=pt + timedelta(days=5), currency="EUR", is_demo=demo,
                      committee={"decision": {"decision": "BUY"}})
        db.add(p)
        db.flush()
        plans.append(p)
    return u, pf, inst, plans


def _bars(db, inst, rows, demo=False):
    for i, (o, h, lo, c) in enumerate(rows, 1):
        db.add(HistoricalBar(instrument_id=inst.id, bar_date=T0 + timedelta(days=i), open=D(str(o)), high=D(str(h)),
                             low=D(str(lo)), close=D(str(c)), adj_close=D(str(c)), source="test", is_demo=demo))
    db.flush()


def test_run_validation_records_outcomes_and_evaluation(db):
    u, pf, inst, (plan,) = _world(db)
    _bars(db, inst, [(101, 102, 99, 101), (103, 111, 102, 110)])
    res = V.run_validation(db, None, actor="test")
    db.commit()
    o = db.execute(select(PlanOutcome)).scalar_one()
    assert o.outcome == "WIN" and o.status_at_creation == "READY" and o.committee_decision == "BUY"
    assert res["by_outcome"] == {"WIN": 1} and res["overall"]["trades"] == 1 and not res["overall"]["validated"]
    assert res["drift"]["status"] == "INSUFFICIENT"
    ev = db.execute(select(ModelEvaluation).where(ModelEvaluation.evaluation_type == "PROSPECTIVE")).scalar_one()
    assert ev.is_out_of_sample and ev.metrics["plans_tracked"] == 1
    V.run_validation(db, None, actor="test")  # stesso giorno: aggiorna, non duplica
    db.commit()
    assert db.execute(select(ModelEvaluation).where(ModelEvaluation.evaluation_type == "PROSPECTIVE")).scalars().all() == [ev]


def test_one_outcome_per_signal(db):
    u, pf, inst, plans = _world(db, n_plans=2)
    sv = StrategyVersion(strategy_id=_strategy(db).id, version="v", parameters={})
    db.add(sv)
    db.flush()
    sr = ScanRun(status="DONE", trigger="MANUAL", requested_by="t")
    db.add(sr)
    db.flush()
    sg = TradingSignal(scan_run_id=sr.id, instrument_id=inst.id, strategy_version_id=sv.id, data_source="t",
                       data_as_of=datetime.now(UTC), score=D("75"), eligible=True, decision="CANDIDATE",
                       setup="PULLBACK", market_regime="BULL", price=D("100"), price_date=T0,
                       analysis_time=datetime.now(UTC))
    db.add(sg)
    db.flush()
    for p in plans:
        p.signal_id = sg.id
    db.flush()
    assert V.record_plan(db, plans[0]) is not None and V.record_plan(db, plans[1]) is None
    assert V.backfill(db) == 0


def _strategy(db):
    st = Strategy(name="s", style="MULTI")
    db.add(st)
    db.flush()
    return st


def test_negative_edge_suspends_calibrated_probabilities(db):
    u, pf, inst, plans = _world(db, n_plans=35)
    _bars(db, inst, [(100, 101, 99, 100), (96, 96, 94, 94)])  # tutte LOSS
    res = V.run_validation(db, None, actor="test")
    db.commit()
    assert res["overall"]["trades"] == 35 and res["overall"]["validated"]
    assert res["drift"]["status"] == "NEGATIVE_EDGE"
    gate = V.validation_gate(db)
    assert gate and gate["status"] == "NEGATIVE_EDGE"
    assert res["by_score"]["70-80"]["trades"] == 35 and res["by_committee"]["BUY"]["trades"] == 35


def test_demo_never_validated_and_no_gate(db):
    u, pf, inst, plans = _world(db, n_plans=35, demo=True)
    _bars(db, inst, [(100, 101, 99, 100), (96, 96, 94, 94)], demo=True)
    res = V.run_validation(db, None, actor="test")
    assert res["is_demo"] and not res["overall"]["validated"] and V.validation_gate(db) is None


def test_validation_api(client, analyst, viewer, db):
    me = db.execute(select(User).where(User.email == "analyst@example.com")).scalar_one()
    pf = get_or_create_paper_portfolio(db, me)
    u, _, inst, (plan,) = _world(db)
    _bars(db, inst, [(101, 102, 99, 101), (103, 111, 102, 110)])
    for i, eq in enumerate(("10000", "10100", "10050")):
        db.add(EquitySnapshot(portfolio_id=pf.id, snap_date=T0 + timedelta(days=i + 1), equity=D(eq), cash=D(eq),
                              market_value=D("0"), realized_pnl=D("0"), unrealized_pnl=D("0"), drawdown_pct=D("0"),
                              aggregate_risk_pct=D("0"), open_positions=0, taken_at=datetime.now(UTC)))
    db.commit()
    assert client.post("/api/validation/run", headers=viewer).status_code == 403
    r = client.post("/api/validation/run", headers=analyst)
    assert r.status_code == 202
    job = jobs.run_one(db)
    assert job.status == "DONE", job.error
    s = client.get("/api/validation/summary", headers=viewer).json()
    assert s["by_outcome"] == {"WIN": 1} and s["last_run"] and s["job"]["status"] == "DONE" and s["gate"] is None
    o = client.get("/api/validation/outcomes", headers=viewer).json()
    assert o["total"] == 1 and o["items"][0]["outcome"] == "WIN" and o["items"][0]["ticker"] == "VAL.US"
    b = client.get("/api/validation/benchmark", headers=analyst).json()
    assert b["portfolio_return_pct"] == 0.5 and len(b["portfolio"]) == 3 and b["benchmarks"] == []
    assert any("nessun dato" in n for n in b["notes"])  # benchmark non sincronizzati: dichiarato, non inventato
    h = client.get("/api/validation/data-health", headers=viewer).json()
    assert h["instruments"] >= 1 and h["items"][0]["status"] in ("OK", "WARN", "BAD")
    assert client.get("/api/validation/signals", headers=viewer).json()["scans"] == []
