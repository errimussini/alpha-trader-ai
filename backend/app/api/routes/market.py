from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.market_data.base import MarketDataAdapter, split_ticker
from app.adapters.market_data.demo import DemoAdapter
from app.api.deps import Pagination, current_user, market_data, require_role
from app.core.config import get_settings
from app.core.db import get_db
from app.core.errors import AppError
from app.core.security import Role
from app.models.entities import HistoricalBar, Instrument, User
from app.schemas.common import BarOut, InstrumentOut, MarketOverview, OverviewItem, Page, QuoteOut, SyncIn, SyncResult
from app.services import audit
from app.services.market_data import find_instrument, get_instrument_or_404, latest_quote, sync_ticker

router = APIRouter(prefix="/api", tags=["market"])


def _benchmarks(adapter: MarketDataAdapter) -> list[str]:
    return DemoAdapter.symbols() if adapter.is_demo else get_settings().benchmark_symbols


def _regime(db: Session) -> dict:
    from app.services.scanner import latest_done_scan
    sr = latest_done_scan(db)
    if not sr or not sr.regime:
        return {"status": "NOT_AVAILABLE", "reason": "Regime calcolato alla prima scansione completata."}
    return {"status": "OK", "regime": sr.regime["regime"], "as_of": str(sr.as_of), "scan_id": sr.id,
            "notes": sr.regime.get("notes", [])}


@router.get("/market/overview", response_model=MarketOverview)
def overview(db: Session = Depends(get_db), adapter: MarketDataAdapter = Depends(market_data),
             _: User = Depends(current_user)):
    s = get_settings()
    items = []
    for t in _benchmarks(adapter):
        inst = find_instrument(db, t)
        q = latest_quote(db, inst.id) if inst else None
        if not q:
            items.append(OverviewItem(ticker=t, name=inst.name if inst else None, price=None, change_pct=None,
                                      quote_time=None, source=None, is_demo=adapter.is_demo, stale=True, status="NO_DATA"))
            continue
        qt = q.quote_time if q.quote_time.tzinfo else q.quote_time.replace(tzinfo=UTC)
        stale = datetime.now(UTC) - qt > timedelta(minutes=s.max_quote_age_minutes)
        chg = ((q.price - q.previous_close) / q.previous_close * 100).quantize(Decimal("0.01")) if q.previous_close else None
        items.append(OverviewItem(ticker=t, name=inst.name, price=q.price, change_pct=chg, quote_time=q.quote_time,
                                  source=q.source, is_demo=q.is_demo, stale=stale, status="STALE" if stale else "OK"))
    return MarketOverview(
        provider=asdict(adapter.info()), items=items, generated_at=datetime.now(UTC),
        regime=_regime(db),
    )


@router.post("/market/sync", response_model=list[SyncResult])
def sync(body: SyncIn, user: User = Depends(require_role(Role.ANALYST)), db: Session = Depends(get_db),
         adapter: MarketDataAdapter = Depends(market_data)):
    """Scarica anagrafica, storico giornaliero e quotazione dal provider configurato."""
    out = []
    for t in body.tickers:
        try:
            split_ticker(t)
            r = sync_ticker(db, adapter, t.upper(), body.years)
            db.commit()
            out.append(SyncResult(ok=True, **r))
        except (AppError, ValueError) as e:
            db.rollback()
            out.append(SyncResult(ticker=t, ok=False, error=getattr(e, "message", str(e))))
    audit.record(db, actor=user.email, action="market.sync", payload={
        "provider": adapter.name, "results": [r.model_dump(mode="json") for r in out]})
    db.commit()
    return out


@router.post("/market/sync-benchmarks", response_model=list[SyncResult])
def sync_benchmarks(user: User = Depends(require_role(Role.ANALYST)), db: Session = Depends(get_db),
                    adapter: MarketDataAdapter = Depends(market_data)):
    return sync(SyncIn(tickers=_benchmarks(adapter)), user, db, adapter)


@router.get("/instruments", response_model=Page[InstrumentOut])
def instruments(pg: Pagination = Depends(), q: str | None = Query(None, max_length=64),
                include_fx: bool = False, db: Session = Depends(get_db), _: User = Depends(current_user)):
    stmt = select(Instrument)
    if not include_fx:
        stmt = stmt.where(Instrument.instrument_type != "FX")
    if q:
        like = f"%{q.upper()}%"
        stmt = stmt.where((Instrument.symbol.ilike(like)) | (Instrument.name.ilike(like)) | (Instrument.isin.ilike(like)))
    total = db.execute(stmt.with_only_columns(Instrument.id)).all()
    rows = db.execute(stmt.order_by(Instrument.symbol).offset(pg.offset).limit(pg.page_size)).scalars()
    return Page(items=list(rows), total=len(total), page=pg.page, page_size=pg.page_size)


@router.get("/instruments/{instrument_id}", response_model=InstrumentOut)
def instrument(instrument_id: int, db: Session = Depends(get_db), _: User = Depends(current_user)):
    return get_instrument_or_404(db, instrument_id)


@router.get("/instruments/{instrument_id}/bars", response_model=list[BarOut])
def bars(instrument_id: int, start: date | None = None, end: date | None = None,
         db: Session = Depends(get_db), _: User = Depends(current_user)):
    get_instrument_or_404(db, instrument_id)
    end = end or datetime.now(UTC).date()
    start = start or end - timedelta(days=365)
    rows = db.execute(select(HistoricalBar).where(HistoricalBar.instrument_id == instrument_id,
                                                  HistoricalBar.bar_date >= start, HistoricalBar.bar_date <= end)
                      .order_by(HistoricalBar.bar_date)).scalars()
    return list(rows)


@router.get("/instruments/{instrument_id}/quote", response_model=QuoteOut | None)
def quote(instrument_id: int, db: Session = Depends(get_db), _: User = Depends(current_user)):
    get_instrument_or_404(db, instrument_id)
    return latest_quote(db, instrument_id)
