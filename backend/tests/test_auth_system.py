def test_health_is_public(client):
    assert client.get("/api/system/health").json() == {"status": "ok"}


def test_protected_requires_token(client):
    r = client.get("/api/portfolio")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthorized"


def test_bad_credentials(client, admin):
    r = client.post("/api/auth/login", data={"username": "admin@example.com", "password": "wrong-password"})
    assert r.status_code == 401


def test_me_and_roles(client, admin, viewer):
    assert client.get("/api/auth/me", headers=viewer).json()["role"] == "viewer"
    body = {"email": "new@example.com", "password": "long-password-1", "role": "analyst"}
    assert client.post("/api/auth/users", json=body, headers=viewer).status_code == 403
    assert client.post("/api/auth/users", json=body, headers=admin).status_code == 201
    assert client.post("/api/auth/users", json=body, headers=admin).status_code == 409


def test_validation_errors_are_structured(client, admin):
    r = client.post("/api/auth/users", json={"email": "x", "password": "short"}, headers=admin)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"


def test_status_defaults_research_and_live_off(client, viewer):
    s = client.get("/api/system/status", headers=viewer).json()
    assert s["mode"] == "RESEARCH"
    assert s["live_trading_enabled"] is False
    assert "LIVE_ASSISTED" not in s["available_modes"]
    assert s["market_data"]["is_demo"] is True


def test_live_mode_cannot_be_enabled(client, admin):
    r = client.put("/api/system/mode", json={"mode": "LIVE_ASSISTED"}, headers=admin)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "mode_not_allowed"
    assert client.put("/api/system/mode", json={"mode": "PAPER"}, headers=admin).json() == {"mode": "PAPER"}


def test_only_admin_changes_mode_and_kill_switch(client, analyst):
    assert client.put("/api/system/mode", json={"mode": "PAPER"}, headers=analyst).status_code == 403
    assert client.post("/api/system/kill-switch", json={"active": True}, headers=analyst).status_code == 403


def test_empty_state_is_honest(client, analyst):
    # nessuna opportunità né backtest inventati su un database vuoto
    assert client.get("/api/opportunities", headers=analyst).json()["total"] == 0
    assert client.get("/api/backtests", headers=analyst).json()["total"] == 0
