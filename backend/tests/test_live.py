"""Fase 8: live assistito spento di default, conferma umana obbligatoria, nessun percorso automatico."""
import pathlib
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest
from sqlalchemy import select

from app.api.deps import market_data
from app.core.config import get_settings
from app.main import app
from app.models.entities import Alert, AuditLog, Instrument, LiveOrderTicket, RiskAssessment, TradePlan, User
from app.services.portfolio import get_or_create_paper_portfolio
from tests.test_monitor import FakeMarket

APP = pathlib.Path(__file__).resolve().parents[1] / "app"


def _plan(db, email="analyst@example.com", demo=False):
    u = db.execute(select(User).where(User.email == email)).scalar_one()
    pf = get_or_create_paper_portfolio(db, u)
    inst = Instrument(symbol=f"LIV{u.id}", exchange="US", name="Live test", currency="EUR", data_source="fake", is_demo=demo)
    db.add(inst)
    db.flush()
    ra = RiskAssessment(portfolio_id=pf.id, approved=True, inputs={}, result={}, blocking_reasons=[], engine_version="t")
    db.add(ra)
    db.flush()
    now = datetime.now(UTC)
    p = TradePlan(instrument_id=inst.id, portfolio_id=pf.id, risk_assessment_id=ra.id, model_version="t", strategy_style="SWING",
                  setup="PULLBACK", score=D("75"), price_reference=D("100"), price_source="t", price_time=now,
                  entry_price=D("100"), entry_low=D("99"), entry_high=D("101"), stop_loss=D("95"), take_profit_1=D("110"),
                  take_profit_2=D("115"), quantity=D("10"), notional_base=D("1000"), est_loss_at_stop_base=D("50"),
                  horizon_days=20, status="READY", valid_until=now + timedelta(days=3), currency="EUR", is_demo=demo)
    db.add(p)
    db.commit()
    return p


@pytest.fixture
def market():
    mk = FakeMarket("100.2")
    app.dependency_overrides[market_data] = lambda: mk
    yield mk
    app.dependency_overrides.pop(market_data, None)


@pytest.fixture
def live_on(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "live_trading_enabled", True)
    monkeypatch.setattr(s, "live_broker", "dry_run")
    return s


def _enable(client, admin):
    r = client.put("/api/system/mode", json={"mode": "LIVE_ASSISTED"}, headers=admin)
    assert r.status_code == 200, r.text


def test_off_by_default(client, admin, analyst, db, market):
    s = client.get("/api/live/status", headers=analyst).json()
    assert s["available"] is False and s["broker"] == "none" and "LIVE_TRADING_ENABLED" in s["reason"]
    plan = _plan(db)
    r = client.post("/api/live/tickets", json={"trade_plan_id": plan.id}, headers=analyst)
    assert r.status_code == 423 and r.json()["error"]["code"] == "live_disabled"
    assert client.put("/api/system/mode", json={"mode": "LIVE_ASSISTED"}, headers=admin).status_code == 400
    st = client.get("/api/system/status", headers=analyst).json()
    assert st["live_trading_enabled"] is False and "LIVE_ASSISTED" not in st["available_modes"]


def test_full_confirmation_flow(client, admin, analyst, db, market, live_on):
    plan = _plan(db)
    st = client.get("/api/system/status", headers=analyst).json()
    assert "LIVE_ASSISTED" in st["available_modes"]
    r = client.post("/api/live/tickets", json={"trade_plan_id": plan.id}, headers=analyst)
    assert r.status_code == 423 and r.json()["error"]["code"] == "wrong_mode"  # serve anche la modalità
    _enable(client, admin)
    r = client.post("/api/live/tickets", json={"trade_plan_id": plan.id}, headers=analyst)
    assert r.status_code == 201, r.text
    t = r.json()
    code = t["confirmation_code"]
    assert t["status"] == "PENDING_CONFIRMATION" and t["order_type"] == "LIMIT" and not t["sends_real_orders"]
    assert D(t["est_loss_at_stop_base"]) <= D("50") and any("collaudo" in w for w in t["warnings"])
    assert "confirmation_code" not in client.get("/api/live/tickets", headers=analyst).json()["items"][0]

    url = f"/api/live/tickets/{t['id']}/confirm"
    wrong = "000000" if code != "000000" else "111111"
    r = client.post(url, json={"confirmation_code": wrong, "acknowledge": True}, headers=analyst)
    assert r.status_code == 423 and r.json()["error"]["code"] == "bad_code"
    assert db.get(LiveOrderTicket, t["id"]).failed_attempts == 1  # persistito nonostante l'errore
    r = client.post(url, json={"confirmation_code": code, "acknowledge": False}, headers=analyst)
    assert r.status_code == 400 and r.json()["error"]["code"] == "ack_required"
    r = client.post(url, json={"confirmation_code": code, "acknowledge": True}, headers=analyst)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["status"] == "SUBMITTED" and out["broker_order_id"].startswith("DRY-") and out["broker_response"]["dry_run"]
    assert client.post(url, json={"confirmation_code": code, "acknowledge": True}, headers=analyst).status_code == 409
    actions = set(db.execute(select(AuditLog.action)).scalars())
    assert {"live.ticket_proposed", "live.ticket_bad_code", "live.order_submitted"} <= actions
    a = db.execute(select(Alert).where(Alert.message.like("Ordine live%"))).scalar_one()
    assert "collaudo" in a.message and "analyst@example.com" in a.message
    s = client.get("/api/live/status", headers=analyst).json()
    assert s["available"] and s["limits"]["orders_today"] == 1 and s["account"]["is_simulated"]


def test_expiry_drift_and_attempts(client, admin, analyst, db, market, live_on):
    _enable(client, admin)
    plan = _plan(db)

    def ticket():
        r = client.post("/api/live/tickets", json={"trade_plan_id": plan.id}, headers=analyst)
        assert r.status_code == 201, r.text
        return r.json()

    t1 = ticket()
    db.get(LiveOrderTicket, t1["id"]).expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db.commit()
    r = client.post(f"/api/live/tickets/{t1['id']}/confirm", json={"confirmation_code": t1["confirmation_code"],
                                                                     "acknowledge": True}, headers=analyst)
    assert r.status_code == 423 and r.json()["error"]["code"] == "ticket_expired"
    db.expire_all()
    assert db.get(LiveOrderTicket, t1["id"]).status == "EXPIRED"

    t2 = ticket()
    market.price = D("101.0")  # +0,8% dalla preparazione: oltre i 50 bps
    r = client.post(f"/api/live/tickets/{t2['id']}/confirm", json={"confirmation_code": t2["confirmation_code"],
                                                                     "acknowledge": True}, headers=analyst)
    assert r.status_code == 423 and r.json()["error"]["code"] == "price_drift"
    db.expire_all()
    assert db.get(LiveOrderTicket, t2["id"]).status == "CANCELLED"

    t3 = ticket()
    bad = "000000" if t3["confirmation_code"] != "000000" else "111111"
    for _ in range(3):
        client.post(f"/api/live/tickets/{t3['id']}/confirm", json={"confirmation_code": bad, "acknowledge": True}, headers=analyst)
    db.expire_all()
    assert db.get(LiveOrderTicket, t3["id"]).status == "CANCELLED"
    t4 = ticket()
    assert client.post(f"/api/live/tickets/{t4['id']}/cancel", headers=analyst).json()["status"] == "CANCELLED"


def test_guards_kill_switch_demo_price_and_limits(client, admin, analyst, db, market, live_on, monkeypatch):
    _enable(client, admin)
    plan = _plan(db)
    t = client.post("/api/live/tickets", json={"trade_plan_id": plan.id}, headers=analyst).json()
    client.post("/api/system/kill-switch", json={"active": True, "reason": "test"}, headers=admin)
    r = client.post(f"/api/live/tickets/{t['id']}/confirm", json={"confirmation_code": t["confirmation_code"],
                                                                    "acknowledge": True}, headers=analyst)
    assert r.status_code == 423 and r.json()["error"]["code"] == "kill_switch"
    assert client.post("/api/live/tickets", json={"trade_plan_id": plan.id}, headers=analyst).status_code == 423
    client.post("/api/system/kill-switch", json={"active": False}, headers=admin)

    market.price = D("103")  # sopra l'intervallo di ingresso: non si insegue il prezzo
    r = client.post("/api/live/tickets", json={"trade_plan_id": plan.id}, headers=analyst)
    assert r.json()["error"]["code"] == "price_out_of_range"
    market.price = D("100.2")
    monkeypatch.setattr(get_settings(), "live_max_order_notional_base", D("100"))
    r = client.post("/api/live/tickets", json={"trade_plan_id": plan.id}, headers=analyst)
    assert r.json()["error"]["code"] == "notional_limit"


def test_demo_plan_blocked_and_owner_rules(client, admin, analyst, db, market, live_on, monkeypatch):
    _enable(client, admin)
    demo = _plan(db, demo=True)
    r = client.post("/api/live/tickets", json={"trade_plan_id": demo.id}, headers=analyst)
    assert r.status_code == 423 and r.json()["error"]["code"] == "demo_data"
    plan = _plan(db, email="admin@example.com")
    t = client.post("/api/live/tickets", json={"trade_plan_id": plan.id}, headers=admin).json()
    body = {"confirmation_code": t["confirmation_code"], "acknowledge": True}
    r = client.post(f"/api/live/tickets/{t['id']}/confirm", json=body, headers=analyst)
    assert r.json()["error"]["code"] == "not_owner"
    monkeypatch.setattr(get_settings(), "live_require_second_approver", True)
    assert client.post(f"/api/live/tickets/{t['id']}/confirm", json=body, headers=admin).json()["error"]["code"] == "second_approver"
    assert client.post(f"/api/live/tickets/{t['id']}/confirm", json=body, headers=analyst).json()["status"] == "SUBMITTED"


def test_paper_orders_blocked_in_live_mode(client, admin, analyst, db, market, live_on):
    _enable(client, admin)
    plan = _plan(db)
    r = client.post("/api/paper/orders", json={"instrument_id": plan.instrument_id, "side": "BUY", "order_type": "MARKET",
                                               "quantity": "1", "client_order_id": "live-mode-0001"}, headers=analyst)
    assert r.status_code == 423 and r.json()["error"]["code"] == "wrong_mode"


def test_no_automatic_path_to_live_broker():
    """Solo services/live.py (e l'adapter stesso) può ottenere un broker live; solo le route live usano il servizio."""
    users_of_broker, users_of_service = set(), set()
    for f in APP.rglob("*.py"):
        src = f.read_text()
        rel = f.relative_to(APP).as_posix()
        if "get_live_broker" in src or "DryRunBroker" in src:
            users_of_broker.add(rel)
        if re.search(r"from app\.services import [^\n]*\blive\b|app\.services\.live|services\.live import", src):
            users_of_service.add(rel)
    assert users_of_broker == {"adapters/broker/live.py", "services/live.py"}
    assert users_of_service == {"api/routes/live.py"}
