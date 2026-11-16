import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

from app.adapters.market_data.base import Quote
from app.adapters.market_data.demo import DemoAdapter
from app.adapters.market_data.registry import get_market_data_adapter
from app.api.deps import market_data
from app.main import app


def _setup(client, admin, analyst, ticker="DEMO1.DEMO"):
    r = client.post("/api/market/sync", json={"tickers": [ticker], "years": 1}, headers=analyst)
    assert r.status_code == 200 and r.json()[0]["ok"], r.text
    assert r.json()[0]["is_demo"] is True
    client.put("/api/system/mode", json={"mode": "PAPER"}, headers=admin)
    return r.json()[0]["instrument_id"]


def _order(client, headers, iid, side="BUY", qty="10", coid=None, **kw):
    body = {"instrument_id": iid, "side": side, "order_type": "MARKET", "quantity": qty,
            "client_order_id": coid or uuid.uuid4().hex} | kw
    return client.post("/api/paper/orders", json=body, headers=headers)


def test_research_mode_blocks_orders(client, analyst):
    r = client.post("/api/market/sync", json={"tickers": ["DEMO1.DEMO"]}, headers=analyst)
    iid = r.json()[0]["instrument_id"]
    r = _order(client, analyst, iid)
    assert r.status_code == 423 and r.json()["error"]["code"] == "wrong_mode"


def test_buy_sell_cycle_and_portfolio(client, admin, analyst):
    iid = _setup(client, admin, analyst)
    bad = _order(client, analyst, iid, stop_loss="100000")
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "invalid_stop"
    r = _order(client, analyst, iid, stop_loss="1")
    assert r.status_code == 200, r.text
    o = r.json()["order"]
    assert o["status"] == "FILLED" and o["is_demo"] is True and o["mode"] == "PAPER"
    p = client.get("/api/portfolio", headers=analyst).json()
    assert D(p["cash"]) < D("10000") and p["open_positions"] == 1
    assert any("DEMO" in w for w in p["warnings"])
    pos = client.get("/api/positions", headers=analyst).json()["items"][0]
    assert pos["is_demo"] and D(pos["quantity"]) == 10
    r = _order(client, analyst, iid, side="SELL", qty="10")
    assert r.json()["order"]["status"] == "FILLED"
    assert client.get("/api/positions", headers=analyst).json()["total"] == 0
    # commissioni e slippage producono una perdita realizzata (nessun profitto "gratis")
    assert D(client.get("/api/portfolio", headers=analyst).json()["realized_pnl"]) < 0


def test_duplicate_client_order_id(client, admin, analyst):
    iid = _setup(client, admin, analyst)
    first = _order(client, analyst, iid, coid="dup-order-1").json()
    second = _order(client, analyst, iid, coid="dup-order-1").json()
    assert second["duplicate"] is True and second["order"]["id"] == first["order"]["id"]
    assert client.get("/api/paper/orders", headers=analyst).json()["total"] == 1


def test_short_selling_disabled(client, admin, analyst):
    iid = _setup(client, admin, analyst)
    r = _order(client, analyst, iid, side="SELL", qty="1")
    assert r.status_code == 423 and r.json()["error"]["code"] == "short_disabled"


def test_kill_switch_blocks(client, admin, analyst):
    iid = _setup(client, admin, analyst)
    client.post("/api/system/kill-switch", json={"active": True, "reason": "test"}, headers=admin)
    r = _order(client, analyst, iid)
    assert r.status_code == 423 and r.json()["error"]["code"] == "kill_switch"


def test_insufficient_cash_rejected(client, admin, analyst):
    iid = _setup(client, admin, analyst)
    r = _order(client, analyst, iid, qty="100000")
    assert r.json()["order"]["status"] == "REJECTED"


def test_stop_cannot_be_widened(client, admin, analyst):
    iid = _setup(client, admin, analyst)
    assert _order(client, analyst, iid, qty="1", stop_loss="10").status_code == 200
    r = _order(client, analyst, iid, qty="1", stop_loss="5")
    assert r.status_code == 423 and r.json()["error"]["code"] == "stop_widening"


class StaleDemo(DemoAdapter):
    def get_quote(self, symbol, exchange):
        q = super().get_quote(symbol, exchange)
        old = datetime.now(UTC) - timedelta(days=2)
        return Quote(**{**q.__dict__, "quote_time": old})


def test_stale_data_blocks(client, admin, analyst):
    iid = _setup(client, admin, analyst)
    app.dependency_overrides[market_data] = lambda: StaleDemo()
    try:
        r = _order(client, analyst, iid)
    finally:
        app.dependency_overrides.pop(market_data, None)
    assert r.status_code == 423 and r.json()["error"]["code"] == "stale_data"


def test_risk_check_endpoint(client, admin, analyst):
    iid = _setup(client, admin, analyst, "DEMO2.DEMO")  # in USD -> usa cambio DEMO
    r = client.post("/api/risk/check", json={"instrument_id": iid, "entry_price": "100", "stop_price": "95",
                                             "take_profit_1": "110"}, headers=analyst)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["approved"] and body["inputs"]["currency"] == "USD"
    assert "DEMO" in body["inputs"]["fx_source"]
    assert D(body["result"]["est_loss_at_stop_base"]) <= D("50")


def test_market_overview_marks_missing_data(client, analyst):
    r = client.get("/api/market/overview", headers=analyst).json()
    assert all(i["status"] == "NO_DATA" for i in r["items"])
    assert r["regime"]["status"] == "NOT_AVAILABLE"
    client.post("/api/market/sync-benchmarks", headers=analyst)
    r = client.get("/api/market/overview", headers=analyst).json()
    assert all(i["status"] == "OK" and i["is_demo"] for i in r["items"])


def test_registry_default_is_demo_in_tests():
    assert get_market_data_adapter().is_demo
