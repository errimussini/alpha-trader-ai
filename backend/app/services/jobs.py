"""Coda generica di job nel database (comitato AI; in seguito backtest e monitoraggio).

- enqueue è idempotente sulla chiave (stesso lavoro mai accodato due volte);
- claim usa SELECT ... FOR UPDATE SKIP LOCKED, quindi più worker non prendono lo stesso job;
- un job RUNNING senza heartbeat da oltre 30 minuti viene marcato FAILED (recover_stale_jobs).
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.entities import Job

STALE_AFTER = timedelta(minutes=30)
Handler = Callable[[Session, Job], dict]
HANDLERS: dict[str, Handler] = {}


def handler(kind: str):
    def deco(fn: Handler) -> Handler:
        HANDLERS[kind] = fn
        return fn
    return deco


def enqueue(db: Session, kind: str, payload: dict, requested_by: str, idempotency_key: str | None = None) -> tuple[Job, bool]:
    if idempotency_key:
        ex = db.execute(select(Job).where(Job.idempotency_key == idempotency_key)).scalar_one_or_none()
        if ex:
            return ex, False
    job = Job(kind=kind, status="QUEUED", payload=payload, requested_by=requested_by, idempotency_key=idempotency_key)
    try:
        with db.begin_nested():
            db.add(job)
            db.flush()
    except IntegrityError:
        return db.execute(select(Job).where(Job.idempotency_key == idempotency_key)).scalar_one(), False
    return job, True


def active_job(db: Session, kind: str, **match) -> Job | None:
    rows = db.execute(select(Job).where(Job.kind == kind, Job.status.in_(("QUEUED", "RUNNING")))).scalars()
    return next((j for j in rows if all(j.payload.get(k) == v for k, v in match.items())), None)


def run_one(db: Session) -> Job | None:
    job = db.execute(select(Job).where(Job.status == "QUEUED").order_by(Job.id)
                     .with_for_update(skip_locked=True).limit(1)).scalar_one_or_none()
    if job is None:
        return None
    job.status, job.started_at, job.heartbeat_at = "RUNNING", datetime.now(UTC), datetime.now(UTC)
    job.attempts += 1
    db.commit()
    fn = HANDLERS.get(job.kind)
    try:
        if fn is None:
            raise RuntimeError(f"Nessun handler per il job '{job.kind}'")
        result = fn(db, job)
        job = db.get(Job, job.id)
        job.status, job.result, job.finished_at = "DONE", result or {}, datetime.now(UTC)
        db.commit()
    except Exception as e:  # noqa: BLE001
        db.rollback()
        job = db.get(Job, job.id)
        job.status, job.error, job.finished_at = "FAILED", f"{type(e).__name__}: {str(e)[:500]}", datetime.now(UTC)
        db.commit()
    return job


def recover_stale_jobs(db: Session) -> int:
    limit = datetime.now(UTC) - STALE_AFTER
    n = 0
    for j in db.execute(select(Job).where(Job.status == "RUNNING")).scalars():
        hb = j.heartbeat_at or j.started_at
        if hb is None or hb < limit:
            j.status, j.error, j.finished_at = "FAILED", "heartbeat scaduto", datetime.now(UTC)
            n += 1
    return n
