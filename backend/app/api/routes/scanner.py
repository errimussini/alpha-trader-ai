from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.adapters.market_data.base import MarketDataAdapter, split_ticker
from app.analytics.scoring import MODEL_VERSION, WEIGHTS
from app.api.deps import Pagination, current_user, market_data, require_role
from app.core.db import get_db
from app.core.errors import AppError, NotFound
from app.core.security import Role
from app.models.entities import Instrument, ScanRun, TradingSignal, UniverseMember, User
from app.services import audit
from app.services.scanner import add_to_universe, default_universe, enqueue_scan, latest_done_scan

router = APIRouter(prefix="/api", tags=["scanner"])


class ScanIn(BaseModel):
    refresh_data: bool = True


class UniverseIn(BaseModel):
    tickers: list[str] = Field(min_length=1, max_length=500)
    list_name: str = Field(default="custom", max_length=64)


def scan_out(sr: ScanRun) -> dict:
    return {"id": sr.id, "status": sr.status, "trigger": sr.trigger, "requested_by": sr.requested_by,
            "params": sr.params, "as_of": sr.as_of, "model_version": sr.model_version, "data_source": sr.data_source,
            "regime": sr.regime, "stats": sr.stats, "errors": sr.errors, "progress": sr.progress, "total": sr.total,
            "created_at": sr.created_at, "started_at": sr.started_at, "finished_at": sr.finished_at}


def signal_out(sig: TradingSignal, inst: Instrument, detail: bool = False) -> dict:
    d = {
        "id": sig.id, "scan_run_id": sig.scan_run_id, "instrument_id": inst.id,
        "ticker": f"{inst.symbol}.{inst.exchange}", "name": inst.name, "isin": inst.isin,
        "exchange": inst.exchange, "sector": inst.sector, "instrument_type": inst.instrument_type,
        "currency": inst.currency, "price": sig.price, "price_date": sig.price_date, "price_source": sig.data_source,
        "score": sig.score, "factor_scores": sig.factor_scores, "coverage": sig.coverage,
        "setup": sig.setup, "strategy_style": sig.strategy_style, "horizon": sig.horizon,
        "risk_level": sig.risk_level, "eligible": sig.eligible, "decision": sig.decision,
        "market_regime": sig.market_regime, "is_demo": sig.is_demo, "analysis_time": sig.analysis_time,
        # disponibili dalla Fase 3 (piano operativo) e dopo validazione statistica (Fase 5)
        "potential_return_gross": None, "potential_return_net": None, "reward_risk": None,
        "calibrated_confidence": None,
        "rationale": sig.rationale, "exclusions": (sig.details or {}).get("exclusions", []),
        "flags": (sig.details or {}).get("flags", []),
    }
    if detail:
        d["details"] = sig.details
    return d


@router.post("/scans", status_code=202)
def create_scan(body: ScanIn | None = None, user: User = Depends(require_role(Role.ANALYST)),
                db: Session = Depends(get_db)):
    sr, created = enqueue_scan(db, actor=user.email, refresh_data=(body or ScanIn()).refresh_data)
    db.commit()
    return JSONResponse(status_code=202 if created else 200,
                        content={"created": created, "scan": _json(scan_out(sr))})


def _json(d: dict) -> dict:
    from fastapi.encoders import jsonable_encoder
    return jsonable_encoder(d)


@router.get("/scans")
def list_scans(pg: Pagination = Depends(), db: Session = Depends(get_db), _: User = Depends(current_user)):
    total = db.execute(select(func.count(ScanRun.id))).scalar_one()
    rows = db.execute(select(ScanRun).order_by(ScanRun.id.desc()).offset(pg.offset).limit(pg.page_size)).scalars()
    return {"items": [scan_out(r) for r in rows], "total": total, "page": pg.page, "page_size": pg.page_size}


@router.get("/scans/{scan_id}")
def get_scan(scan_id: int, db: Session = Depends(get_db), _: User = Depends(current_user)):
    sr = db.get(ScanRun, scan_id)
    if not sr:
        raise NotFound("Scansione non trovata")
    return scan_out(sr)


@router.get("/rankings/latest")
def latest_ranking(pg: Pagination = Depends(), exchange: str | None = None, sector: str | None = None,
                   risk_level: str | None = Query(None, pattern="^(LOW|MEDIUM|HIGH)$"),
                   setup: str | None = Query(None, pattern="^(BREAKOUT|PULLBACK|TREND_FOLLOWING)$"),
                   style: str | None = Query(None, pattern="^(SWING|POSITION)$"),
                   instrument_type: str | None = Query(None, pattern="^(STOCK|ETF)$"),
                   include_excluded: bool = False, scan_id: int | None = None,
                   db: Session = Depends(get_db), _: User = Depends(current_user)):
    sr = db.get(ScanRun, scan_id) if scan_id else latest_done_scan(db)
    if sr is None:
        return {"scan": None, "items": [], "total": 0, "page": pg.page, "page_size": pg.page_size,
                "weights": WEIGHTS, "model_version": MODEL_VERSION}
    stmt = select(TradingSignal, Instrument).join(Instrument, TradingSignal.instrument_id == Instrument.id) \
        .where(TradingSignal.scan_run_id == sr.id)
    if not include_excluded:
        stmt = stmt.where(TradingSignal.eligible, TradingSignal.score.is_not(None))
    for col, val in ((Instrument.exchange, exchange.upper() if exchange else None), (Instrument.sector, sector),
                     (TradingSignal.risk_level, risk_level), (TradingSignal.setup, setup),
                     (TradingSignal.strategy_style, style), (Instrument.instrument_type, instrument_type)):
        if val:
            stmt = stmt.where(col == val)
    total = db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    rows = db.execute(stmt.order_by(TradingSignal.score.desc().nulls_last(), Instrument.symbol)
                      .offset(pg.offset).limit(pg.page_size)).all()
    facets = db.execute(select(Instrument.exchange, Instrument.sector).join(
        TradingSignal, TradingSignal.instrument_id == Instrument.id).where(TradingSignal.scan_run_id == sr.id)).all()
    return {"scan": scan_out(sr), "items": [signal_out(s, i) for s, i in rows], "total": total, "page": pg.page,
            "page_size": pg.page_size, "weights": WEIGHTS, "model_version": MODEL_VERSION,
            "facets": {"exchanges": sorted({e for e, _ in facets}), "sectors": sorted({s for _, s in facets if s})}}


@router.get("/rankings/signals/{signal_id}")
def signal_detail(signal_id: int, db: Session = Depends(get_db), _: User = Depends(current_user)):
    sig = db.get(TradingSignal, signal_id)
    if not sig:
        raise NotFound("Segnale non trovato")
    return signal_out(sig, sig.instrument, detail=True)


@router.get("/market/regime")
def market_regime(db: Session = Depends(get_db), _: User = Depends(current_user)):
    sr = latest_done_scan(db)
    if not sr or not sr.regime:
        return {"status": "NOT_AVAILABLE", "reason": "Nessuna scansione completata."}
    return {"status": "OK", "as_of": sr.as_of, "scan_id": sr.id, **sr.regime}


# ------------------------------------------------------------------ universo
@router.get("/universe")
def universe(pg: Pagination = Depends(), db: Session = Depends(get_db), _: User = Depends(current_user)):
    total = db.execute(select(func.count(UniverseMember.id)).where(UniverseMember.is_active)).scalar_one()
    rows = db.execute(select(UniverseMember).where(UniverseMember.is_active).order_by(UniverseMember.list_name,
                      UniverseMember.ticker).offset(pg.offset).limit(pg.page_size)).scalars()
    return {"items": [{"id": r.id, "ticker": r.ticker, "list_name": r.list_name, "created_at": r.created_at}
                      for r in rows], "total": total, "page": pg.page, "page_size": pg.page_size}


@router.post("/universe")
def add_universe(body: UniverseIn, user: User = Depends(require_role(Role.ANALYST)), db: Session = Depends(get_db)):
    try:
        tickers = [(t.strip().upper(), body.list_name) for t in body.tickers if t.strip()]
        for t, _ in tickers:
            split_ticker(t)
    except ValueError as e:
        raise AppError(str(e), code="invalid_ticker") from e
    added = add_to_universe(db, tickers, user.email)
    db.commit()
    return {"added": added}


@router.post("/universe/seed")
def seed_universe(user: User = Depends(require_role(Role.ANALYST)), db: Session = Depends(get_db),
                  adapter: MarketDataAdapter = Depends(market_data)):
    added = add_to_universe(db, default_universe(adapter), user.email)
    db.commit()
    return {"added": added}


@router.delete("/universe/{member_id}")
def remove_universe(member_id: int, user: User = Depends(require_role(Role.ANALYST)), db: Session = Depends(get_db)):
    m = db.get(UniverseMember, member_id)
    if not m:
        raise NotFound("Titolo non presente nell'universo")
    m.is_active = False
    audit.record(db, actor=user.email, action="universe.remove", payload={"ticker": m.ticker,
                 "at": datetime.now(UTC).isoformat()})
    db.commit()
    return {"removed": m.ticker}
