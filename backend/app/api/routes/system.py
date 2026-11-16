from dataclasses import asdict

from fastapi import APIRouter, Depends
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.adapters.market_data.base import MarketDataAdapter
from app.api.deps import Pagination, current_user, market_data, require_role
from app.core.config import get_settings
from app.core.db import get_db
from app.core.security import Role
from app.models.entities import AuditLog, User
from app.schemas.common import AuditOut, KillSwitchIn, ModeIn, Page, SystemStatus
from app.services import audit, safety

router = APIRouter(prefix="/api/system", tags=["system"])


def _redis_status() -> str:
    try:
        import redis
        redis.Redis.from_url(get_settings().redis_url, socket_connect_timeout=1).ping()
        return "ok"
    except Exception:  # noqa: BLE001
        return "unavailable"


@router.get("/health")
def health(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    return {"status": "ok"}


@router.get("/status", response_model=SystemStatus)
def status(db: Session = Depends(get_db), adapter: MarketDataAdapter = Depends(market_data),
           _: User = Depends(current_user)):
    try:
        db.execute(text("SELECT 1"))
        dbs = "ok"
    except Exception:  # noqa: BLE001
        dbs = "error"
    s = get_settings()
    return SystemStatus(
        app=s.app_name, mode=safety.get_mode(db), available_modes=["RESEARCH", "PAPER"],
        live_trading_enabled=False, kill_switch=safety.kill_switch_state(db), market_data=asdict(adapter.info()),
        database=dbs, redis=_redis_status(), max_quote_age_minutes=s.max_quote_age_minutes,
    )


@router.put("/mode")
def set_mode(body: ModeIn, user: User = Depends(require_role(Role.ADMIN)), db: Session = Depends(get_db)):
    mode = safety.set_mode(db, body.mode.upper(), user.email)
    db.commit()
    return {"mode": mode}


@router.post("/kill-switch")
def kill_switch(body: KillSwitchIn, user: User = Depends(require_role(Role.ADMIN)), db: Session = Depends(get_db)):
    state = safety.set_kill_switch(db, body.active, body.reason, user.email)
    db.commit()
    return state


@router.get("/audit", response_model=Page[AuditOut])
def audit_log(pg: Pagination = Depends(), _: User = Depends(require_role(Role.ANALYST)), db: Session = Depends(get_db)):
    total = db.query(AuditLog).count()
    rows = db.execute(select(AuditLog).order_by(AuditLog.id.desc()).offset(pg.offset).limit(pg.page_size)).scalars()
    return Page(items=list(rows), total=total, page=pg.page, page_size=pg.page_size)


@router.get("/audit/verify")
def audit_verify(_: User = Depends(require_role(Role.ANALYST)), db: Session = Depends(get_db)):
    ok, bad = audit.verify_chain(db)
    return {"valid": ok, "first_invalid_id": bad}
