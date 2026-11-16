"""Validazione prospettica, confronto con i benchmark, affidabilità di dati e segnali (Fase 7)."""
from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import Pagination, current_user, require_role
from app.core.db import get_db
from app.core.security import Role
from app.models.entities import Job, ModelEvaluation, PlanOutcome, User
from app.services.portfolio import get_or_create_paper_portfolio
from app.validation import service as V

router = APIRouter(prefix="/api/validation", tags=["validation"])


@router.get("/summary")
def summary(db: Session = Depends(get_db), _: User = Depends(current_user)):
    """Metriche calcolate ora sugli esiti registrati (l'aggiornamento degli esiti avviene con /run o dal worker)."""
    out = V.summary(db)
    last = db.execute(select(ModelEvaluation).where(ModelEvaluation.evaluation_type == "PROSPECTIVE")
                      .order_by(ModelEvaluation.created_at.desc()).limit(1)).scalar_one_or_none()
    out["last_run"] = {"model_evaluation_id": last.id, "at": last.created_at.isoformat(),
                       "update": (last.metrics or {}).get("update")} if last else None
    out["gate"] = V.validation_gate(db)
    job = db.execute(select(Job).where(Job.kind == "validation").order_by(Job.id.desc()).limit(1)).scalar_one_or_none()
    out["job"] = {"id": job.id, "status": job.status, "error": job.error} if job else None
    if out["last_run"] and job and job.status == "DONE" and job.finished_at:
        out["last_run"]["at"] = job.finished_at.isoformat()  # la valutazione del giorno viene aggiornata, non duplicata
    return out


@router.post("/run", status_code=202)
def run(user: User = Depends(require_role(Role.ANALYST)), db: Session = Depends(get_db)):
    job, created = V.enqueue_validation(db, user.email) if not V.jobs.active_job(db, "validation") else \
        (V.jobs.active_job(db, "validation"), False)
    db.commit()
    return {"job_id": job.id, "status": job.status, "created": created}


@router.get("/outcomes")
def outcomes(pg: Pagination = Depends(), outcome: str | None = Query(None, max_length=14),
             db: Session = Depends(get_db), _: User = Depends(current_user)):
    stmt = select(PlanOutcome)
    if outcome:
        stmt = stmt.where(PlanOutcome.outcome == outcome)
    total = db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    rows = db.execute(stmt.order_by(PlanOutcome.created_for.desc(), PlanOutcome.id.desc())
                      .offset(pg.offset).limit(pg.page_size)).scalars()
    items = [{"id": o.id, "trade_plan_id": o.trade_plan_id, "ticker": f"{o.instrument.symbol}.{o.instrument.exchange}",
              "setup": o.setup, "score": o.score, "status_at_creation": o.status_at_creation,
              "committee_decision": o.committee_decision, "created_for": o.created_for, "outcome": o.outcome,
              "entry_date": o.entry_date, "entry_price": o.entry_price, "exit_date": o.exit_date,
              "exit_price": o.exit_price, "r_gross": o.r_gross, "r_net": o.r_net, "mfe_r": o.mfe_r, "mae_r": o.mae_r,
              "bars_evaluated": o.bars_evaluated, "last_bar_date": o.last_bar_date, "data_source": o.data_source,
              "is_demo": o.is_demo, "notes": o.notes,
              "plan": {"entry": o.plan.entry_price, "stop": o.plan.stop_loss, "tp1": o.plan.take_profit_1,
                       "valid_until": o.plan.valid_until, "horizon_days": o.plan.horizon_days}} for o in rows]
    return {"items": items, "total": total, "page": pg.page, "page_size": pg.page_size}


@router.get("/benchmark")
def benchmark(user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = get_or_create_paper_portfolio(db, user)
    db.commit()
    return V.benchmark_comparison(db, p)


@router.get("/data-health")
def data_health(days: int = Query(60, ge=10, le=365), db: Session = Depends(get_db), _: User = Depends(current_user)):
    return V.data_health(db, days)


@router.get("/signals")
def signals(last: int = Query(10, ge=2, le=50), db: Session = Depends(get_db), _: User = Depends(current_user)):
    return V.signal_stability(db, last)
