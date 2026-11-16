from datetime import UTC, datetime

from app.adapters.market_data.demo import DemoAdapter
from app.models.entities import ScanRun
from app.services.scanner import run_scan


def _run(db, scan_id):
    sr = db.get(ScanRun, scan_id)
    return run_scan(db, DemoAdapter(), sr)


def test_scan_end_to_end(client, analyst, db):
    assert client.post("/api/universe/seed", headers=analyst).json()["added"] == 5
    r = client.post("/api/scans", json={"refresh_data": True}, headers=analyst)
    assert r.status_code == 202
    sid = r.json()["scan"]["id"]
    # una seconda richiesta non crea una scansione duplicata
    r2 = client.post("/api/scans", headers=analyst)
    assert r2.status_code == 200 and r2.json()["created"] is False and r2.json()["scan"]["id"] == sid

    sr = _run(db, sid)
    assert sr.status == "DONE" and sr.stats["analysed"] == 5
    assert sr.regime["regime"] in ("BULL", "BEAR", "SIDEWAYS", "HIGH_VOLATILITY")

    rk = client.get("/api/rankings/latest?include_excluded=true", headers=analyst).json()
    assert rk["total"] == 5 and rk["scan"]["id"] == sid
    for it in rk["items"]:
        assert it["is_demo"] is True and it["price_date"] and it["price_source"].startswith("DEMO")
        assert it["calibrated_confidence"] is None and it["potential_return_net"] is None
        assert it["factor_scores"]["sentiment"] is None  # mai stimato
        if it["score"] is not None:
            assert 0 <= float(it["score"]) <= 100
        else:
            assert it["exclusions"] or it["flags"]
    scores = [float(i["score"]) for i in rk["items"] if i["score"] is not None]
    assert scores == sorted(scores, reverse=True)

    detail = client.get(f"/api/rankings/signals/{rk['items'][0]['id']}", headers=analyst).json()
    assert "features" in detail["details"] and "could_be_wrong_if" in detail["rationale"]
    assert client.get("/api/market/regime", headers=analyst).json()["status"] == "OK"
    assert client.get("/api/market/overview", headers=analyst).json()["regime"]["status"] == "OK"


def test_scan_without_universe_fails_cleanly(client, analyst, db):
    sid = client.post("/api/scans", headers=analyst).json()["scan"]["id"]
    import pytest
    from app.core.errors import AppError
    with pytest.raises(AppError):
        _run(db, sid)


def test_universe_validation(client, analyst, viewer):
    assert client.post("/api/universe", json={"tickers": ["AAPL"]}, headers=analyst).status_code == 400
    assert client.post("/api/universe", json={"tickers": ["AAPL.US"]}, headers=viewer).status_code == 403
    assert client.post("/api/universe", json={"tickers": ["AAPL.US", "eni.mi"]}, headers=analyst).json()["added"] == 2
    items = client.get("/api/universe", headers=analyst).json()["items"]
    assert {i["ticker"] for i in items} == {"AAPL.US", "ENI.MI"}


def test_scheduled_scan_idempotent(db):
    from app.services.scanner import enqueue_scan
    a, ca = enqueue_scan(db, actor="scheduler", trigger="SCHEDULED", idempotency_key="scheduled:2026-01-05")
    db.commit()
    b, cb = enqueue_scan(db, actor="scheduler", trigger="SCHEDULED", idempotency_key="scheduled:2026-01-05")
    assert ca and not cb and a.id == b.id


def test_default_universe_file():
    from app.services.scanner import default_universe

    class Real:  # adapter non demo
        is_demo = False
    rows = default_universe(Real())
    assert len(rows) > 100 and all("." in t for t, _ in rows)
    assert datetime.now(UTC)
