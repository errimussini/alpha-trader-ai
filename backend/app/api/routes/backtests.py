"""Backtest Engine e Strategy Laboratory (Fase 5)."""
from datetime import date

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import Pagination, current_user, require_role
from app.backtest.runner import enqueue_backtest
from app.core.db import get_db
from app.core.errors import NotFound
from app.core.security import Role
from app.models.entities import BacktestRun, Job, User

router = APIRouter(prefix="/api/backtests", tags=["backtest"])


class ParamsIn(BaseModel):
    initial_capital: float = Field(10000, ge=1000, le=10_000_000)
    min_score: float = Field(60, ge=0, le=100)
    max_positions: int = Field(8, ge=1, le=50)
    risk_per_trade_pct: float = Field(0.5, gt=0, le=5)
    max_aggregate_risk_pct: float = Field(3, gt=0, le=20)
    max_position_exposure_pct: float = Field(15, gt=0, le=100)
    max_sector_exposure_pct: float = Field(30, gt=0, le=100)
    min_cash_reserve_pct: float = Field(5, ge=0, le=50)
    commission_per_order: float = Field(2, ge=0, le=100)
    commission_pct: float = Field(0, ge=0, le=2)
    slippage_bps: float = Field(5, ge=0, le=200)
    fx_cost_bps: float = Field(10, ge=0, le=200)
    min_price: float = Field(5, ge=0)
    min_traded_value: float = Field(5e6, ge=0)
    setups: list[str] = ["BREAKOUT", "PULLBACK", "TREND_FOLLOWING"]
    trailing: bool = True
    time_stop: bool = True


class BacktestIn(BaseModel):
    name: str | None = Field(None, max_length=128)
    tickers: list[str] | None = Field(None, max_length=400, description="null = universo attivo")
    start: date | None = None
    end: date | None = None
    params: ParamsIn = ParamsIn()
    train_days: int = Field(252, ge=60, le=1260)
    test_days: int = Field(63, ge=20, le=252)
    download_years: int = Field(0, ge=0, le=15, description="scarica prima N anni di storico (0 = usa i dati presenti)")

    @model_validator(mode="after")
    def _check(self):
        if self.start and self.end and self.start >= self.end:
            raise ValueError("start deve precedere end")
        bad = set(self.params.setups) - {"BREAKOUT", "PULLBACK", "TREND_FOLLOWING"}
        if bad or not self.params.setups:
            raise ValueError(f"Setup non validi: {sorted(bad) or 'nessuno'}")
        return self


def _out(r: BacktestRun, full: bool = False) -> dict:
    d = {"id": r.id, "name": r.name, "status": r.status, "engine_version": r.engine_version, "params": r.params,
         "universe_size": len(r.universe or []), "period_start": r.period_start, "period_end": r.period_end,
         "summary": r.summary, "error": r.error, "is_demo": r.is_demo, "requested_by": r.requested_by,
         "strategy_version_id": r.strategy_version_id, "created_at": r.created_at, "finished_at": r.finished_at}
    if full:
        d |= {"universe": r.universe, "results": r.results}
    return d


@router.get("")
def list_runs(pg: Pagination = Depends(), db: Session = Depends(get_db), _: User = Depends(current_user)):
    total = db.execute(select(func.count(BacktestRun.id))).scalar_one()
    rows = db.execute(select(BacktestRun).order_by(BacktestRun.id.desc()).offset(pg.offset).limit(pg.page_size)).scalars()
    return {"items": [_out(r) for r in rows], "total": total, "page": pg.page, "page_size": pg.page_size}


@router.post("", status_code=202)
def create_run(body: BacktestIn, user: User = Depends(require_role(Role.ANALYST)), db: Session = Depends(get_db)):
    run = enqueue_backtest(db, body.model_dump(mode="json"), user.email)
    db.commit()
    return _out(run)


@router.get("/{run_id}")
def get_run(run_id: int, db: Session = Depends(get_db), _: User = Depends(current_user)):
    r = db.get(BacktestRun, run_id)
    if r is None:
        raise NotFound("Backtest non trovato")
    out = _out(r, full=True)
    if r.job_id:
        j = db.get(Job, r.job_id)
        out["job"] = {"id": j.id, "status": j.status, "error": j.error} if j else None
    return out
