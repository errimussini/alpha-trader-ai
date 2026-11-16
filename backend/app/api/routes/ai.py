"""AI Investment Committee e AI Trading Assistant (Fase 4)."""
from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.ai.assistant import answer
from app.ai.llm import ai_status, get_llm
from app.api.deps import Pagination, current_user, require_role
from app.core.db import get_db
from app.core.errors import NotFound
from app.core.security import Role
from app.models.entities import AiRun, Job, TradePlan, User
from app.services import audit
from app.services.committee import request_committee
from app.services.portfolio import get_or_create_paper_portfolio

router = APIRouter(prefix="/api", tags=["ai"])


@router.get("/ai/status")
def status(_: User = Depends(current_user)):
    return ai_status()


def _own_plan(db: Session, user: User, plan_id: int) -> TradePlan:
    p = db.get(TradePlan, plan_id)
    pf = get_or_create_paper_portfolio(db, user)
    if not p or (p.portfolio_id is not None and p.portfolio_id != pf.id):
        raise NotFound("Opportunità non trovata")
    return p


def _committee_view(db: Session, p: TradePlan) -> dict:
    c = dict(p.committee or {})
    if c.get("job_id"):
        j = db.get(Job, c["job_id"])
        c["job"] = {"id": j.id, "status": j.status, "error": j.error} if j else None
    return {"plan_id": p.id, "committee": c}


@router.get("/opportunities/{plan_id}/committee")
def get_committee(plan_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return _committee_view(db, _own_plan(db, user, plan_id))


@router.post("/opportunities/{plan_id}/committee")
def convene(plan_id: int, user: User = Depends(require_role(Role.ANALYST)), db: Session = Depends(get_db)):
    p = _own_plan(db, user, plan_id)
    request_committee(db, p, user.email, user.id)
    audit.record(db, actor=user.email, action="committee.requested", entity="trade_plan", entity_id=p.id,
                 payload={"mode": ai_status()["mode"]})
    db.commit()
    return _committee_view(db, p)


@router.get("/ai/runs")
def runs(pg: Pagination = Depends(), trade_plan_id: int | None = None, kind: str | None = None,
         user: User = Depends(require_role(Role.ANALYST)), db: Session = Depends(get_db)):
    stmt = select(AiRun)
    if trade_plan_id:
        _own_plan(db, user, trade_plan_id)
        stmt = stmt.where(AiRun.trade_plan_id == trade_plan_id)
    if kind:
        stmt = stmt.where(AiRun.kind == kind)
    if user.role != Role.ADMIN:
        stmt = stmt.where((AiRun.user_id == user.id) | (AiRun.trade_plan_id.is_not(None)))
    total = db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    rows = db.execute(stmt.order_by(AiRun.id.desc()).offset(pg.offset).limit(pg.page_size)).scalars()
    cols = ("id", "kind", "agent", "trade_plan_id", "mode", "model", "prompt_version", "prompt_hash", "input_hash",
            "output", "validation", "usage", "request_id", "status", "error", "latency_ms", "created_at")
    return {"items": [{c: getattr(r, c) for c in cols} for r in rows], "total": total, "page": pg.page, "page_size": pg.page_size}


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=4000)


class ChatIn(BaseModel):
    messages: list[ChatMessage] = Field(min_length=1, max_length=20)

    @field_validator("messages")
    @classmethod
    def _last_is_user(cls, v):
        if v[-1].role != "user":
            raise ValueError("L'ultimo messaggio deve essere dell'utente")
        return v


@router.post("/assistant/chat")
def chat(body: ChatIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    msgs = [m.model_dump() for m in body.messages]
    while msgs and msgs[0]["role"] != "user":  # la conversazione inviata al modello inizia sempre dall'utente
        msgs.pop(0)
    res = answer(db, user, msgs, get_llm())
    db.commit()
    return res


@router.get("/assistant/history")
def history(limit: int = Query(20, ge=1, le=100), user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = db.execute(select(AiRun).where(AiRun.kind == "assistant", AiRun.user_id == user.id)
                      .order_by(AiRun.id.desc()).limit(limit)).scalars()
    return [{"id": r.id, "question": (r.inputs.get("messages") or [{}])[-1].get("content"), "answer": r.output.get("answer"),
             "mode": r.mode, "model": r.model, "created_at": r.created_at} for r in rows]
