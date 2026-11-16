import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.services import audit


def test_chain_is_valid_and_detects_tampering(db):
    for i in range(3):
        audit.record(db, actor="t", action="test", payload={"i": i})
    db.commit()
    assert audit.verify_chain(db) == (True, None)


def test_audit_rows_are_immutable(db):
    audit.record(db, actor="t", action="test")
    db.commit()
    with pytest.raises(DBAPIError, match="append-only"):
        db.execute(text("UPDATE audit_logs SET actor='x'"))
    db.rollback()
    with pytest.raises(DBAPIError, match="append-only"):
        db.execute(text("DELETE FROM audit_logs"))
    db.rollback()


def test_login_is_audited(client, admin):
    rows = client.get("/api/system/audit", headers=admin).json()["items"]
    assert any(r["action"] == "auth.login" for r in rows)
    assert client.get("/api/system/audit/verify", headers=admin).json()["valid"] is True
