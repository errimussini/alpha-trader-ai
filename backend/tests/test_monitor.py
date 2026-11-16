"""Fase 6: ordini aperti, fill parziali, stop/TP/trailing, alert, riconciliazione, snapshot."""
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D

import httpx
import pytest
from sqlalchemy import select

from app.adapters.market_data.base import DataProviderError, MarketDataAdapter, Quote
from app.core.config import get_settings
from app.models.entities import (
    Alert, AuditLog, EquitySnapshot, Instrument, Order, Position, RiskAssessment, TradePlan, User,
)
from app.services import safety
from app.services.alerts import notify_pending, raise_alert
from app.services.monitor import reconcile, run_monitor
from app.services.paper_trading import place_paper_order
from app.services.portfolio import get_or_create_paper_portfolio


class FakeMarket(MarketDataAdapter):
    """Quotazioni controllate dal test (non sono dati di mercato)."""
    name = "fake"

    def __init__(self, price="100", volume=None, stale=False):
        self.price, self.volume, self.stale, self.fail = D(price), volume, stale, False

    def info(self):  # pragma: no cover
        raise NotImplementedError

    def get_instrument(self, symbol, exchange):  # pragma: no cover
        raise NotImplementedError

    def get_daily_bars(self, symbol, exchange, start, end):  # pragma: no cover
        raise NotImplementedError

    def get_quote(self, symbol, exchange):
        if self.fail:
            raise DataProviderError(self.name, "timeout simulato")
        now = datetime.now(UTC)
        t = now - timedelta(days=2) if self.stale else now
        s = (self.price * D("0.0005")).quantize(D("0.0001"))
        return Quote(symbol=symbol, exchange=exchange, price=self.price, quote_time=t, source="fake", retrieved_at=now,
                     currency="EUR", bid=self.price - s, ask=self.price + s, volume=self.volume)


@pytest.fixture
def env(db):
    u = User(email="trader@example.com", hashed_password="x", role="analyst")
    db.add(u)
    db.flush()
    pf = get_or_create_paper_portfolio(db, u)
    inst = Instrument(symbol="TEST", exchange="US", name="Test", currency="EUR", data_source="fake", sector="Tech")
    db.add(inst)
    safety.set_mode(db, "PAPER", "test")
    db.commit()
    return db, u, pf, inst, FakeMarket()


def _buy(env, qty="10", coid="b1", **kw):
    db, u, pf, inst, mk = env
    out = place_paper_order(db, adapter=mk, user=u, portfolio=pf, instrument=inst, side="BUY",
                            order_type=kw.pop("order_type", "MARKET"), quantity=D(qty), client_order_id=coid, **kw)
    db.commit()
    return out.order


def _pos(db, pf):
    return db.execute(select(Position).where(Position.portfolio_id == pf.id).order_by(Position.id.desc())).scalars().first()


def _alerts(db, kind=None):
    q = select(Alert).order_by(Alert.id)
    if kind:
        q = q.where(Alert.kind == kind)
    return list(db.execute(q).scalars())


def test_limit_order_stays_open_then_fills(env):
    db, u, pf, inst, mk = env
    o = _buy(env, order_type="LIMIT", limit_price=D("97"), stop_loss=D("90"))
    assert o.status == "OPEN" and o.valid_until is not None and o.filled_quantity == 0
    rep = run_monitor(db, mk, pf)  # prezzo ancora 100: resta aperto
    assert rep.orders_checked == 1 and not rep.orders_filled and o.status == "OPEN"
    mk.price = D("96")
    rep = run_monitor(db, mk, pf)
    db.commit()
    assert rep.orders_filled == [o.id] and o.status == "FILLED"
    pos = _pos(db, pf)
    assert pos.quantity == 10 and pos.stop_loss == D("90") and pos.avg_price <= D("97.3")
    assert reconcile(db, pf)["ok"]


def test_limit_order_cancelled_when_expired_or_below_stop(env):
    db, u, pf, inst, mk = env
    o1 = _buy(env, coid="l1", order_type="LIMIT", limit_price=D("95"), stop_loss=D("90"))
    o1.valid_until = datetime.now(UTC) - timedelta(minutes=1)
    o2 = _buy(env, coid="l2", order_type="LIMIT", limit_price=D("94"), stop_loss=D("92"))
    db.commit()
    mk.price = D("91")  # sotto lo stop del secondo ordine
    rep = run_monitor(db, mk, pf)
    db.commit()
    assert set(rep.orders_cancelled) == {o1.id, o2.id}
    assert o1.status == o2.status == "CANCELLED" and "scaduta" in o1.reject_reason and "stop" in o2.reject_reason
    assert len(_alerts(db, "SETUP_INVALIDATED")) == 2 and _pos(db, pf) is None


def test_partial_fills_with_participation_cap(env, monkeypatch):
    db, u, pf, inst, mk = env
    monkeypatch.setattr(get_settings(), "paper_max_participation_pct", D("1"))
    mk.volume = 1000  # 1% = 10 pezzi per controllo
    o = _buy(env, qty="25", stop_loss=D("90"))
    assert o.status == "PARTIALLY_FILLED" and o.filled_quantity == 10
    run_monitor(db, mk, pf)
    assert o.filled_quantity == 20 and o.status == "PARTIALLY_FILLED"
    run_monitor(db, mk, pf)
    db.commit()
    assert o.status == "FILLED" and _pos(db, pf).quantity == 25
    assert reconcile(db, pf)["ok"]


def test_stop_hit_executes_simulated_exit(env):
    db, u, pf, inst, mk = env
    _buy(env, stop_loss=D("95"), take_profit_1=D("110"))
    mk.price = D("94")
    rep = run_monitor(db, mk, pf)
    db.commit()
    pos = _pos(db, pf)
    assert pos.status == "CLOSED" and rep.exits[0]["purpose"] == "STOP"
    a = _alerts(db, "STOP_HIT")
    assert len(a) == 1 and a[0].severity == "CRITICAL" and "simulata" in a[0].message
    exit_order = db.execute(select(Order).where(Order.purpose == "STOP")).scalar_one()
    assert exit_order.side == "SELL" and exit_order.status == "FILLED" and exit_order.created_by == "monitor"
    assert pf.realized_pnl < 0 and reconcile(db, pf)["ok"]


def test_kill_switch_blocks_exit_and_alert_is_not_repeated(env):
    db, u, pf, inst, mk = env
    _buy(env, stop_loss=D("95"))
    safety.set_kill_switch(db, True, "test", "test")
    mk.price = D("94")
    run_monitor(db, mk, pf)
    run_monitor(db, mk, pf)
    db.commit()
    assert _pos(db, pf).status == "OPEN"
    a = _alerts(db, "STOP_HIT")
    assert len(a) == 1 and "NON eseguita" in a[0].message


def test_auto_exits_disabled_only_alerts(env, monkeypatch):
    db, u, pf, inst, mk = env
    monkeypatch.setattr(get_settings(), "paper_auto_exits", False)
    _buy(env, stop_loss=D("95"))
    mk.price = D("94")
    run_monitor(db, mk, pf)
    assert _pos(db, pf).status == "OPEN" and "disattivata" in _alerts(db, "STOP_HIT")[0].message


def test_tp1_partial_breakeven_and_trailing(env):
    db, u, pf, inst, mk = env
    _buy(env, stop_loss=D("95"), take_profit_1=D("105"))
    pos = _pos(db, pf)
    pos.take_profit_2, pos.trailing_distance = D("120"), D("3")
    db.commit()
    mk.price = D("106")
    run_monitor(db, mk, pf)
    db.commit()
    db.refresh(pos)
    assert pos.quantity == 5 and pos.tp1_done and pos.trailing_active and pos.stop_loss == pos.avg_price
    assert _alerts(db, "TARGET_HIT")
    mk.price = D("110")
    rep = run_monitor(db, mk, pf)
    assert pos.stop_loss == D("107") and rep.stops_raised
    mk.price = D("108")  # lo stop non scende mai
    run_monitor(db, mk, pf)
    assert pos.stop_loss == D("107") and pos.status == "OPEN"
    mk.price = D("106.5")
    rep = run_monitor(db, mk, pf)
    db.commit()
    assert pos.status == "CLOSED" and rep.exits[0]["purpose"] == "TRAIL"
    raised = db.execute(select(AuditLog).where(AuditLog.action == "position.stop_raised")).scalars().all()
    assert all(D(r.payload["to"]) > D(r.payload["from"]) for r in raised)
    assert reconcile(db, pf)["ok"]


def _plan(db, pf, inst, status="READY"):
    ra = RiskAssessment(portfolio_id=pf.id, approved=True, inputs={}, result={}, blocking_reasons=[], engine_version="t")
    db.add(ra)
    db.flush()
    now = datetime.now(UTC)
    p = TradePlan(instrument_id=inst.id, portfolio_id=pf.id, risk_assessment_id=ra.id, model_version="t",
                  strategy_style="SWING", setup="PULLBACK", score=D("70"), price_reference=D("100"), price_source="t",
                  price_time=now, entry_price=D("100"), entry_low=D("99"), entry_high=D("101"), stop_loss=D("95"),
                  take_profit_1=D("110"), take_profit_2=D("115"), quantity=D("10"), notional_base=D("1000"),
                  est_loss_at_stop_base=D("50"), horizon_days=30, status=status, valid_until=now + timedelta(days=3),
                  currency="EUR", stop_rationale={"atr": 2.0})
    db.add(p)
    db.commit()
    return p


def test_plans_entry_opportunity_and_invalidation(env):
    db, u, pf, inst, mk = env
    plan = _plan(db, pf, inst)
    run_monitor(db, mk, pf)
    run_monitor(db, mk, pf)
    e = _alerts(db, "ENTRY_OPPORTUNITY")
    assert len(e) == 1 and "nessun ordine" in e[0].message.lower()
    assert db.execute(select(Order)).first() is None  # una notifica non è mai un ordine
    mk.price = D("94")
    rep = run_monitor(db, mk, pf)
    db.commit()
    assert rep.plans_invalidated == [plan.id] and plan.status == "EXPIRED"
    assert _alerts(db, "SETUP_INVALIDATED")


def test_plan_order_sets_trailing_and_open_order_cancelled_when_plan_expires(env):
    db, u, pf, inst, mk = env
    plan = _plan(db, pf, inst)
    o = _buy(env, coid="pl1", order_type="LIMIT", limit_price=D("99"), trade_plan_id=plan.id)
    assert o.status == "OPEN" and o.purpose == "ENTRY" and o.stop_loss == D("95") and o.valid_until == plan.valid_until
    plan.status, plan.status_reason = "EXPIRED", "validità scaduta"
    db.commit()
    mk.price = D("98")
    run_monitor(db, mk, pf)
    assert o.status == "CANCELLED" and "EXPIRED" in o.reject_reason
    plan2 = _plan(db, pf, inst)
    _buy(env, coid="pl2", trade_plan_id=plan2.id)
    pos = _pos(db, pf)
    assert pos.trailing_distance == D("5") and pos.take_profit_2 == D("115")


def test_review_alert_when_latest_plan_rejected(env):
    db, u, pf, inst, mk = env
    _buy(env, stop_loss=D("90"))
    p = _plan(db, pf, inst, status="REJECTED")
    p.status_reason = "trend non confermato"
    db.commit()
    run_monitor(db, mk, pf)
    r = _alerts(db, "REVIEW_POSITION")
    assert len(r) == 1 and "trend non confermato" in r[0].message


def test_stale_data_suspends_actions(env):
    db, u, pf, inst, mk = env
    _buy(env, stop_loss=D("95"))
    stale = FakeMarket("90", stale=True)
    rep = run_monitor(db, stale, pf)
    run_monitor(db, stale, pf)
    assert _pos(db, pf).status == "OPEN" and rep.data_issues and not rep.exits
    assert len(_alerts(db, "DATA_ANOMALY")) == 1


def test_aggregate_risk_and_reconciliation_alerts(env):
    db, u, pf, inst, mk = env
    _buy(env, qty="50", stop_loss=D("80"))  # rischio a stop ~1000 su ~10000 = 10% > 3%
    run_monitor(db, mk, pf)
    assert _alerts(db, "AGGREGATE_RISK")
    pf.cash += D("100")  # manomissione: la cassa non torna con i movimenti
    db.commit()
    rec = reconcile(db, pf)
    assert not rec["ok"] and rec["issues"][0]["check"] == "cash"
    rep = run_monitor(db, mk, pf)
    assert not rep.reconciliation_ok and _alerts(db, "BROKER_ANOMALY")[0].severity == "CRITICAL"


def test_snapshot_and_daily_return(client, analyst, db):
    u = db.execute(select(User).where(User.email == "analyst@example.com")).scalar_one()
    pf = get_or_create_paper_portfolio(db, u)
    db.add(EquitySnapshot(portfolio_id=pf.id, snap_date=date.today() - timedelta(days=1), equity=D("9900"),
                          cash=D("9900"), market_value=D("0"), realized_pnl=D("0"), unrealized_pnl=D("0"),
                          drawdown_pct=D("1"), aggregate_risk_pct=D("0"), open_positions=0,
                          taken_at=datetime.now(UTC)))
    db.commit()
    p = client.get("/api/portfolio", headers=analyst).json()
    assert D(p["daily_return_pct"]) == D("1.01")  # 10000 / 9900 - 1
    r = client.post("/api/paper/monitor", headers=analyst)
    assert r.status_code == 200 and r.json()["snapshot"]["equity"] == "10000.00"
    r = client.post("/api/paper/monitor", headers=analyst)  # stesso giorno: aggiornato, non duplicato
    eq = client.get("/api/portfolio/equity", headers=analyst).json()
    assert len(eq) == 2 and eq[-1]["snap_date"] == date.today().isoformat()
    assert client.get("/api/paper/reconciliation", headers=analyst).json()["ok"]


def test_alert_dedup_scope_and_webhook(client, analyst, db, monkeypatch):
    u = db.execute(select(User).where(User.email == "analyst@example.com")).scalar_one()
    pf = get_or_create_paper_portfolio(db, u)
    other = User(email="other@example.com", hashed_password="x", role="analyst")
    db.add(other)
    db.flush()
    opf = get_or_create_paper_portfolio(db, other)
    assert raise_alert(db, "SYSTEM", "WARNING", "uno", portfolio_id=pf.id, dedup_key="k") is not None
    assert raise_alert(db, "SYSTEM", "WARNING", "uno", portfolio_id=pf.id, dedup_key="k") is None
    raise_alert(db, "SYSTEM", "CRITICAL", "di un altro utente", portfolio_id=opf.id)
    db.commit()
    items = client.get("/api/alerts", headers=analyst).json()["items"]
    assert [i["message"] for i in items] == ["uno"]
    s = client.get("/api/alerts/summary", headers=analyst).json()
    assert s["active"] == 1 and s["by_severity"] == {"WARNING": 1}
    other_id = db.execute(select(Alert.id).where(Alert.portfolio_id == opf.id)).scalar_one()
    assert client.post(f"/api/alerts/{other_id}/ack", headers=analyst).status_code == 404

    from pydantic import SecretStr
    sent = []
    monkeypatch.setattr(get_settings(), "alert_webhook_url", SecretStr("https://hooks.invalid/x"))
    tr = httpx.MockTransport(lambda req: (sent.append(req), httpx.Response(200))[1])
    assert notify_pending(db, httpx.Client(transport=tr)) == 2
    assert notify_pending(db, httpx.Client(transport=tr)) == 0
    assert b"non \\u00e8 un ordine" in sent[0].content or "non è un ordine" in sent[0].content.decode()


def _api_setup(client, admin, analyst):
    r = client.post("/api/market/sync", json={"tickers": ["DEMO1.DEMO"], "years": 1}, headers=analyst)
    client.put("/api/system/mode", json={"mode": "PAPER"}, headers=admin)
    return r.json()[0]["instrument_id"]


def test_position_levels_and_cancel_api(client, admin, analyst, db):
    iid = _api_setup(client, admin, analyst)
    body = {"instrument_id": iid, "side": "BUY", "order_type": "MARKET", "quantity": "5", "client_order_id": "lv-000001"}
    r = client.post("/api/paper/orders", json=body, headers=analyst)
    assert r.status_code == 200, r.text
    pos = client.get("/api/positions", headers=analyst).json()["items"][0]
    px = D(pos["last_price"])
    url = f"/api/positions/{pos['id']}/levels"
    assert client.patch(url, json={"stop_loss": str(px * D("0.97"))}, headers=analyst).status_code == 200
    r = client.patch(url, json={"stop_loss": str(px * D("0.96"))}, headers=analyst)
    assert r.status_code == 423 and r.json()["error"]["code"] == "stop_widening"
    r = client.patch(url, json={"stop_loss": str(px * D("0.5")), "authorize_wider_stop": True, "reason": "test"},
                     headers=analyst)
    assert r.status_code == 423 and r.json()["error"]["code"] == "stop_widening_over_budget"
    r = client.patch(url, json={"stop_loss": str(px * D("0.96")), "authorize_wider_stop": True,
                                "reason": "volatilità più alta dopo gli utili"}, headers=analyst)
    assert r.status_code == 200, r.text
    assert db.execute(select(AuditLog).where(AuditLog.action == "position.stop_widened")).scalar_one()
    assert client.patch(url, json={"stop_loss": str(px * 2)}, headers=analyst).status_code == 400

    lim = body | {"order_type": "LIMIT", "limit_price": str((px * D("0.9")).quantize(D("0.01"))),
                  "client_order_id": "lv-000002"}
    o = client.post("/api/paper/orders", json=lim, headers=analyst).json()["order"]
    assert o["status"] == "OPEN" and o["ticker"] == "DEMO1.DEMO"
    assert client.get("/api/paper/orders?open_only=true", headers=analyst).json()["total"] == 1
    r = client.post(f"/api/paper/orders/{o['id']}/cancel", headers=analyst)
    assert r.status_code == 200 and r.json()["status"] == "CANCELLED"
    assert client.post(f"/api/paper/orders/{o['id']}/cancel", headers=analyst).status_code == 409
