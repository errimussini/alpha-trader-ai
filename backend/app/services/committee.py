"""Orchestrazione del comitato: esecuzione immediata (deterministica) o in coda (AI, chiamate lente e a pagamento)."""
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.ai.committee import run_committee
from app.ai.llm import get_llm
from app.core.errors import NotFound
from app.models.entities import Job, TradePlan
from app.services import jobs


def _previous(plan: TradePlan) -> dict | None:
    c = plan.committee or {}
    return c if c.get("status") == "DONE" else c.get("previous")


def request_committee(db: Session, plan: TradePlan, actor: str, user_id: int | None) -> dict:
    llm = get_llm()
    if llm is None:
        return run_committee(db, plan, None, actor, user_id)
    job = jobs.active_job(db, "committee", plan_id=plan.id)
    if job is None:
        job, _ = jobs.enqueue(db, "committee", {"plan_id": plan.id, "user_id": user_id}, actor)
        plan.committee = {"status": "QUEUED", "job_id": job.id, "requested_at": datetime.now(UTC).isoformat(),
                          "model": llm.model, "previous": _previous(plan)}
    return plan.committee


@jobs.handler("committee")
def _committee_job(db: Session, job: Job) -> dict:
    plan = db.get(TradePlan, job.payload["plan_id"])
    if plan is None:
        raise NotFound("Piano non trovato")
    prev = _previous(plan)
    plan.committee = (plan.committee or {}) | {"status": "RUNNING", "started_at": datetime.now(UTC).isoformat()}
    db.commit()
    try:
        res = run_committee(db, plan, get_llm(), job.requested_by, job.payload.get("user_id"))
        db.commit()
    except Exception as e:
        db.rollback()
        plan = db.get(TradePlan, job.payload["plan_id"])
        plan.committee = {"status": "FAILED", "error": f"{type(e).__name__}: {str(e)[:300]}", "previous": prev}
        db.commit()
        raise
    return {"plan_id": plan.id, "decision": res["decision"]["decision"], "mode": res["mode"]}
