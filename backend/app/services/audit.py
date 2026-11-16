"""Audit log append-only con catena di hash (tamper-evident)."""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models.entities import AuditLog

_LOCK_KEY = 815_001  # chiave advisory lock PostgreSQL per serializzare la catena


def _default(o):
    if isinstance(o, Decimal):
        return str(o)
    if isinstance(o, datetime):
        return o.isoformat()
    return str(o)


def _canonical(d: dict) -> str:
    return json.dumps(d, sort_keys=True, default=_default, separators=(",", ":"))


def compute_hash(prev_hash: str | None, created_at: datetime, actor: str, action: str,
                 entity: str | None, entity_id: str | None, payload: dict) -> str:
    body = _canonical({
        "prev": prev_hash, "ts": created_at.astimezone(UTC).isoformat(), "actor": actor, "action": action,
        "entity": entity, "entity_id": entity_id, "payload": payload,
    })
    return hashlib.sha256(body.encode()).hexdigest()


def record(db: Session, *, actor: str, action: str, entity: str | None = None,
           entity_id: str | int | None = None, payload: dict | None = None) -> AuditLog:
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _LOCK_KEY})
    prev = db.execute(select(AuditLog.hash).order_by(AuditLog.id.desc()).limit(1)).scalar_one_or_none()
    payload = json.loads(_canonical(payload or {}))
    now = datetime.now(UTC)
    eid = str(entity_id) if entity_id is not None else None
    entry = AuditLog(created_at=now, actor=actor, action=action, entity=entity, entity_id=eid, payload=payload,
                     prev_hash=prev, hash=compute_hash(prev, now, actor, action, entity, eid, payload))
    db.add(entry)
    db.flush()
    return entry


def verify_chain(db: Session) -> tuple[bool, int | None]:
    """Ricalcola la catena. Restituisce (ok, id della prima riga non coerente)."""
    prev = None
    for row in db.execute(select(AuditLog).order_by(AuditLog.id)).scalars():
        expected = compute_hash(prev, row.created_at, row.actor, row.action, row.entity, row.entity_id, row.payload)
        if row.prev_hash != prev or row.hash != expected:
            return False, row.id
        prev = row.hash
    return True, None
