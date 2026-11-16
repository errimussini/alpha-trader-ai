"""Acquisizione e persistenza dei dati di mercato dal provider configurato."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.market_data.base import DataProviderError, MarketDataAdapter, Quote, split_ticker
from app.core.errors import DataUnavailable, NotFound
from app.models.entities import Alert, AlertKind, HistoricalBar, Instrument, MarketQuote, Severity


def find_instrument(db: Session, ticker: str) -> Instrument | None:
    sym, ex = split_ticker(ticker)
    return db.execute(select(Instrument).where(Instrument.symbol == sym, Instrument.exchange == ex)).scalar_one_or_none()


def _data_alert(db: Session, message: str, instrument_id: int | None = None, data: dict | None = None) -> None:
    db.add(Alert(kind=AlertKind.DATA_ANOMALY, severity=Severity.WARNING, message=message,
                 instrument_id=instrument_id, data=data or {}))


def upsert_instrument(db: Session, adapter: MarketDataAdapter, ticker: str) -> Instrument:
    sym, ex = split_ticker(ticker)
    try:
        info = adapter.get_instrument(sym, ex)
    except DataProviderError as e:
        raise DataUnavailable(f"Anagrafica non disponibile per {ticker}: {e}") from e
    inst = find_instrument(db, ticker)
    if inst is None:
        inst = Instrument(symbol=sym, exchange=ex)
        db.add(inst)
    inst.name = info.name
    inst.currency = info.currency
    inst.instrument_type = info.instrument_type
    inst.isin = info.isin
    inst.country = info.country
    inst.sector = info.sector
    inst.industry = info.industry
    inst.market_cap = info.market_cap
    inst.data_source = info.source
    inst.is_demo = info.is_demo
    inst.updated_at = datetime.now(UTC)
    db.flush()
    return inst


def ensure_fx_instrument(db: Session, base: str, quote: str, source: str, is_demo: bool) -> Instrument:
    ticker = f"{base}{quote}.FOREX"
    inst = find_instrument(db, ticker)
    if inst is None:
        inst = Instrument(symbol=f"{base}{quote}", exchange="FOREX", name=f"{base}/{quote}", currency=quote,
                          instrument_type="FX", data_source=source, is_demo=is_demo)
        db.add(inst)
        db.flush()
    return inst


def sync_bars(db: Session, adapter: MarketDataAdapter, inst: Instrument, start: date, end: date) -> int:
    try:
        series = adapter.get_daily_bars(inst.symbol, inst.exchange, start, end)
    except DataProviderError as e:
        _data_alert(db, f"Storico non disponibile per {inst.symbol}.{inst.exchange}: {e}", inst.id)
        raise DataUnavailable(f"Storico non disponibile: {e}") from e
    existing = set(db.execute(
        select(HistoricalBar.bar_date).where(HistoricalBar.instrument_id == inst.id,
                                             HistoricalBar.bar_date >= start, HistoricalBar.bar_date <= end)
    ).scalars())
    added = 0
    for b in series.bars:
        if b.high < b.low or b.close <= 0:
            _data_alert(db, f"Barra incoerente scartata {inst.symbol} {b.bar_date}", inst.id)
            continue
        if b.bar_date in existing:
            continue
        db.add(HistoricalBar(instrument_id=inst.id, timeframe="1d", bar_date=b.bar_date, open=b.open, high=b.high,
                             low=b.low, close=b.close, adj_close=b.adj_close, volume=b.volume, source=series.source,
                             retrieved_at=series.retrieved_at, is_demo=series.is_demo))
        added += 1
    db.flush()
    return added


def store_quote(db: Session, inst: Instrument, q: Quote) -> MarketQuote:
    mq = MarketQuote(instrument_id=inst.id, price=q.price, bid=q.bid, ask=q.ask, previous_close=q.previous_close,
                     volume=q.volume, currency=q.currency or inst.currency, quote_time=q.quote_time,
                     retrieved_at=q.retrieved_at, source=q.source, delay_minutes=q.delay_minutes, is_demo=q.is_demo)
    db.add(mq)
    db.flush()
    return mq


def fetch_quote(db: Session, adapter: MarketDataAdapter, inst: Instrument) -> MarketQuote:
    try:
        q = adapter.get_quote(inst.symbol, inst.exchange)
    except DataProviderError as e:
        _data_alert(db, f"Quotazione non disponibile per {inst.symbol}.{inst.exchange}: {e}", inst.id)
        raise DataUnavailable(f"Quotazione non disponibile per {inst.symbol}.{inst.exchange}: {e}") from e
    return store_quote(db, inst, q)


def fetch_fx(db: Session, adapter: MarketDataAdapter, base: str, quote: str) -> MarketQuote | None:
    """Restituisce None se base == quote. Il prezzo è: unità di `quote` per 1 `base`."""
    if base == quote:
        return None
    try:
        q = adapter.get_fx_rate(base, quote)
    except DataProviderError as e:
        _data_alert(db, f"Cambio {base}/{quote} non disponibile: {e}")
        raise DataUnavailable(f"Cambio {base}/{quote} non disponibile: {e}") from e
    inst = ensure_fx_instrument(db, base, quote, q.source, q.is_demo)
    return store_quote(db, inst, q)


def latest_quote(db: Session, instrument_id: int) -> MarketQuote | None:
    return db.execute(select(MarketQuote).where(MarketQuote.instrument_id == instrument_id)
                      .order_by(MarketQuote.quote_time.desc(), MarketQuote.id.desc()).limit(1)).scalar_one_or_none()


def latest_fx(db: Session, base: str, quote: str) -> MarketQuote | None:
    if base == quote:
        return None
    inst = find_instrument(db, f"{base}{quote}.FOREX")
    return latest_quote(db, inst.id) if inst else None


def sync_ticker(db: Session, adapter: MarketDataAdapter, ticker: str, years: int = 3) -> dict:
    inst = upsert_instrument(db, adapter, ticker)
    end = datetime.now(UTC).date()
    added = sync_bars(db, adapter, inst, end - timedelta(days=365 * years), end)
    quote = fetch_quote(db, adapter, inst)
    return {"instrument_id": inst.id, "ticker": ticker, "bars_added": added, "quote_time": quote.quote_time,
            "source": quote.source, "is_demo": quote.is_demo}


def get_instrument_or_404(db: Session, instrument_id: int) -> Instrument:
    inst = db.get(Instrument, instrument_id)
    if not inst:
        raise NotFound(f"Strumento {instrument_id} non trovato")
    return inst
