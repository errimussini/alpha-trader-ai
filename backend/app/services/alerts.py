"""Alert del sistema: creazione con deduplica e notifica opzionale via webhook.

Un alert è una NOTIFICA: non equivale mai a un ordine eseguito. Quando un alert accompagna un'esecuzione
simulata (stop o target eseguiti dal PaperBroker) il testo lo dice esplicitamente.

Deduplica: se esiste un alert non ancora letto con la stessa `dedup_key` creato nelle ultime
`alert_dedup_hours` ore, non ne viene creato un altro (il monitoraggio gira ogni pochi minuti).
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.entities import Alert

log = logging.getLogger("alerts")
NOTIFY_SEVERITIES = {"WARNING", "CRITICAL"}


def raise_alert(db: Session, kind: str, severity: str, message: str, *, portfolio_id: int | None = None,
                instrument_id: int | None = None, data: dict | None = None,
                dedup_key: str | None = None) -> Alert | None:
    s = get_settings()
    if dedup_key:
        since = datetime.now(UTC) - timedelta(hours=s.alert_dedup_hours)
        dup = db.execute(select(Alert.id).where(Alert.dedup_key == dedup_key, Alert.acknowledged_at.is_(None),
                                                Alert.created_at >= since).limit(1)).scalar_one_or_none()
        if dup is not None:
            return None
    a = Alert(kind=kind, severity=severity, message=message, portfolio_id=portfolio_id, instrument_id=instrument_id,
              data=data or {}, dedup_key=dedup_key)
    db.add(a)
    db.flush()
    return a


def notify_pending(db: Session, client: httpx.Client | None = None) -> int:
    """Invia al webhook configurato gli alert WARNING/CRITICAL non ancora notificati. Restituisce quanti."""
    s = get_settings()
    url = s.alert_webhook_url.get_secret_value().strip() if s.alert_webhook_url else ""
    if not url:
        return 0
    rows = list(db.execute(select(Alert).where(Alert.notified_at.is_(None), Alert.severity.in_(NOTIFY_SEVERITIES))
                           .order_by(Alert.id).limit(50)).scalars())
    if not rows:
        return 0
    own = client is None
    client = client or httpx.Client(timeout=5.0)
    sent = 0
    try:
        for a in rows:
            body = {"id": a.id, "kind": a.kind, "severity": a.severity, "message": a.message,
                    "created_at": a.created_at.isoformat(), "data": a.data,
                    "note": "Notifica del sistema di simulazione: non è un ordine eseguito su un conto reale."}
            try:
                r = client.post(url, json=body)
                r.raise_for_status()
            except httpx.HTTPError as e:
                log.warning("webhook alert #%s non inviato: %s", a.id, type(e).__name__)
                break  # riprova al prossimo ciclo, nell'ordine
            a.notified_at = datetime.now(UTC)
            sent += 1
    finally:
        if own:
            client.close()
    db.flush()
    return sent
