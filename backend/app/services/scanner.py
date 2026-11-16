"""Market Scanner: aggiornamento dati dell'universo, filtri, regime di mercato e ranking.

Pipeline di una scansione (point-in-time alla data `as_of`):
1. (opzionale) aggiornamento incrementale dei dati dal provider, con retry per titolo
2. regime di mercato dai benchmark (Agent 1)
3. per ogni titolo: indicatori, filtri di eleggibilità, setup (solo dati fino ad `as_of`)
4. punteggi cross-sectional e composito, salvati come TradingSignal collegati alla scansione
"""
from __future__ import annotations

import csv
import logging
import time
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from importlib import resources

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.adapters.market_data.base import DataProviderError, MarketDataAdapter, split_ticker
from app.adapters.market_data.demo import DemoAdapter
from app.analytics import indicators, regime as regime_mod, setups
from app.analytics.scoring import (
    MODEL_VERSION, WEIGHTS, Candidate, FilterConfig, apply_filters, candidate_payload, score_universe,
)
from app.core.config import get_settings
from app.core.errors import AppError
from app.models.entities import (
    CorporateEvent, FundamentalSnapshot, HistoricalBar, Instrument, MarketQuote, ScanRun, Strategy,
    StrategyVersion, TradingSignal, UniverseMember,
)
from app.services import audit
from app.services.market_data import find_instrument, store_quote, sync_bars, upsert_instrument

log = logging.getLogger("scanner")


# ------------------------------------------------------------------ universo
def default_universe(adapter: MarketDataAdapter) -> list[tuple[str, str]]:
    if adapter.is_demo:
        return [(t, "demo") for t in DemoAdapter.symbols()]
    with resources.files("app.data").joinpath("universe_default.csv").open() as fh:
        return [(r["ticker"].upper(), r["list_name"]) for r in csv.DictReader(fh)]


def add_to_universe(db: Session, tickers: list[tuple[str, str]], actor: str) -> int:
    existing = {t for (t,) in db.execute(select(UniverseMember.ticker))}
    added = 0
    for t, ln in tickers:
        split_ticker(t)
        if t in existing:
            m = db.execute(select(UniverseMember).where(UniverseMember.ticker == t)).scalar_one()
            m.is_active = True
            continue
        db.add(UniverseMember(ticker=t, list_name=ln, added_by=actor))
        existing.add(t)
        added += 1
    audit.record(db, actor=actor, action="universe.add", payload={"tickers": [t for t, _ in tickers], "added": added})
    return added


def active_universe(db: Session) -> list[str]:
    return list(db.execute(select(UniverseMember.ticker).where(UniverseMember.is_active).order_by(UniverseMember.id)).scalars())


def regime_tickers(adapter: MarketDataAdapter) -> tuple[list[str], str | None, str | None]:
    s = get_settings()
    if adapter.is_demo:
        return ["DEMO5.DEMO", "DEMO4.DEMO"], None, None
    return s.regime_benchmarks, s.regime_vix, s.regime_rates


def rs_benchmark(adapter: MarketDataAdapter, exchange: str) -> str:
    if adapter.is_demo:
        return "DEMO5.DEMO"
    m = get_settings().rs_benchmark_by_region
    return m["US"] if exchange == "US" else m["EU"]


# ------------------------------------------------------------------ dati
def _retry(fn, retries: int, pause: float):
    for attempt in range(retries + 1):
        try:
            return fn()
        except DataProviderError as e:
            if e.status == 404 or attempt == retries:
                raise
            time.sleep(pause * (2 ** attempt))
    return None


def _last_bar_date(db: Session, instrument_id: int) -> date | None:
    return db.execute(select(func.max(HistoricalBar.bar_date)).where(HistoricalBar.instrument_id == instrument_id)).scalar()


def ensure_index_instrument(db: Session, ticker: str, source: str) -> Instrument:
    inst = find_instrument(db, ticker)
    if inst is None:
        sym, ex = split_ticker(ticker)
        inst = Instrument(symbol=sym, exchange=ex, name=ticker, currency="USD", instrument_type="INDEX",
                          data_source=source, is_demo=False)
        db.add(inst)
        db.flush()
    return inst


def refresh_ticker(db: Session, adapter: MarketDataAdapter, ticker: str, as_of: date, *, index: bool = False) -> dict:
    """Aggiornamento incrementale: anagrafica (settimanale), barre mancanti, quotazione, fondamentali, utili."""
    s = get_settings()
    retries, pause = s.scan_max_retries, max(s.scan_request_pause_seconds, 0.01)
    info: dict = {"ticker": ticker}
    sym, ex = split_ticker(ticker)

    def bars(inst: Instrument) -> None:
        last = _last_bar_date(db, inst.id)
        start = (last + timedelta(days=1)) if last else as_of - timedelta(days=365 * s.scan_history_years + 30)
        if start <= as_of:
            info["bars_added"] = _retry(lambda: sync_bars(db, adapter, inst, start, as_of), retries, pause)

    if index:
        bars(ensure_index_instrument(db, ticker, adapter.info().name))
        return info
    # la quotazione per prima: con Yahoo popola la cache usata da anagrafica e fondamentali
    try:
        q = _retry(lambda: adapter.get_quote(sym, ex), retries, pause)
    except DataProviderError as e:
        q, info["quote_error"] = None, str(e)
    inst = find_instrument(db, ticker)
    if inst is None or datetime.now(UTC) - (inst.updated_at if inst.updated_at.tzinfo
                                            else inst.updated_at.replace(tzinfo=UTC)) > timedelta(days=7):
        inst = _retry(lambda: upsert_instrument(db, adapter, ticker), retries, pause)
    if q is not None:
        store_quote(db, inst, q)
    bars(inst)
    caps = set(adapter.info().capabilities)
    if inst.instrument_type == "STOCK" and "fundamentals" in caps:
        try:
            f = adapter.get_fundamentals(inst.symbol, inst.exchange)
            data = f.get("data", f)
            dec = lambda k: Decimal(str(data[k])) if data.get(k) is not None else None  # noqa: E731
            debt, cash = data.get("totalDebt"), data.get("totalCash")
            db.add(FundamentalSnapshot(
                instrument_id=inst.id, as_of=datetime.now(UTC), period_type="TTM", eps=dec("trailingEps"),
                operating_margin=dec("operatingMargins"), free_cash_flow=dec("freeCashflow"),
                net_debt=Decimal(str(debt - cash)) if debt is not None and cash is not None else None,
                roe=dec("returnOnEquity"), pe_ratio=dec("forwardPE") or dec("trailingPE"),
                ev_ebitda=dec("enterpriseToEbitda"), data=data, source=f.get("source", adapter.info().name),
            ))
        except DataProviderError as e:
            info["fundamentals_error"] = str(e)
    if inst.instrument_type == "STOCK" and "earnings_calendar" in caps:
        try:
            events = adapter.get_earnings_calendar([ticker], as_of, as_of + timedelta(days=45))
            known = {(e.event_date) for e in db.execute(select(CorporateEvent).where(
                CorporateEvent.instrument_id == inst.id, CorporateEvent.event_type == "EARNINGS")).scalars()}
            for e in events:
                if e.event_date not in known:
                    db.add(CorporateEvent(instrument_id=inst.id, event_type="EARNINGS", event_date=e.event_date,
                                          details=e.details, source=e.source))
            info["earnings_checked"] = True
        except DataProviderError as e:
            info["earnings_error"] = str(e)
    db.flush()
    return info


def bars_frame(db: Session, instrument_id: int, as_of: date, lookback_days: int = 900) -> pd.DataFrame:
    rows = db.execute(select(HistoricalBar).where(
        HistoricalBar.instrument_id == instrument_id, HistoricalBar.bar_date <= as_of,
        HistoricalBar.bar_date >= as_of - timedelta(days=lookback_days)).order_by(HistoricalBar.bar_date)).scalars().all()
    if not rows:
        return pd.DataFrame(columns=["open", "high", "low", "close", "adj_close", "volume", "source"])
    df = pd.DataFrame([{
        "date": pd.Timestamp(r.bar_date), "open": float(r.open), "high": float(r.high), "low": float(r.low),
        "close": float(r.close), "adj_close": float(r.adj_close) if r.adj_close is not None else None,
        "volume": float(r.volume) if r.volume is not None else float("nan"), "source": r.source,
        "is_demo": r.is_demo,
    } for r in rows]).set_index("date")
    df["adj_close"] = df["adj_close"].astype(float)
    return df


# ------------------------------------------------------------------ esecuzione
def get_strategy_version(db: Session, cfg: FilterConfig) -> StrategyVersion:
    st = db.execute(select(Strategy).where(Strategy.name == "Composite Ranking")).scalar_one_or_none()
    if st is None:
        st = Strategy(name="Composite Ranking", style="MULTI",
                      description="Ranking composito multi-fattore (Fase 2). Pesi iniziali non validati.")
        db.add(st)
        db.flush()
    sv = db.execute(select(StrategyVersion).where(StrategyVersion.strategy_id == st.id,
                                                  StrategyVersion.version == MODEL_VERSION)).scalar_one_or_none()
    if sv is None:
        sv = StrategyVersion(strategy_id=st.id, version=MODEL_VERSION, is_validated=False,
                             parameters={"weights": WEIGHTS, "filters": cfg.__dict__})
        db.add(sv)
        db.flush()
    return sv


def enqueue_scan(db: Session, *, actor: str, trigger: str = "MANUAL", refresh_data: bool = True,
                 idempotency_key: str | None = None) -> tuple[ScanRun, bool]:
    """Accoda una scansione. Restituisce (scan, creata). Una sola scansione attiva alla volta."""
    if idempotency_key:
        ex = db.execute(select(ScanRun).where(ScanRun.idempotency_key == idempotency_key)).scalar_one_or_none()
        if ex:
            return ex, False
    active = db.execute(select(ScanRun).where(ScanRun.status.in_(("QUEUED", "RUNNING")))).scalars().first()
    if active:
        return active, False
    sr = ScanRun(trigger=trigger, status="QUEUED", requested_by=actor, idempotency_key=idempotency_key,
                 params={"refresh_data": refresh_data})
    db.add(sr)
    db.flush()
    audit.record(db, actor=actor, action="scan.enqueued", entity="scan_run", entity_id=sr.id, payload=sr.params)
    return sr, True


def _filter_config() -> FilterConfig:
    s = get_settings()
    return FilterConfig(min_price=s.scan_min_price, min_market_cap=s.scan_min_market_cap,
                        min_traded_value=s.scan_min_traded_value, max_spread_pct=s.scan_max_spread_pct)


def _latest_fundamentals(db: Session, instrument_id: int, as_of: date) -> dict | None:
    f = db.execute(select(FundamentalSnapshot).where(
        FundamentalSnapshot.instrument_id == instrument_id,
        func.date(FundamentalSnapshot.as_of) <= as_of).order_by(FundamentalSnapshot.as_of.desc()).limit(1)).scalar_one_or_none()
    return f.data if f else None


def _spread(db: Session, instrument_id: int, as_of: date) -> float | None:
    q = db.execute(select(MarketQuote).where(MarketQuote.instrument_id == instrument_id)
                   .order_by(MarketQuote.quote_time.desc()).limit(1)).scalar_one_or_none()
    if not q or q.bid is None or q.ask is None or q.ask <= 0 or q.bid <= 0:
        return None
    if (as_of - q.quote_time.date()).days > 3:
        return None
    mid = (q.ask + q.bid) / 2
    return float((q.ask - q.bid) / mid * 100)


def run_scan(db: Session, adapter: MarketDataAdapter, sr: ScanRun, as_of: date | None = None) -> ScanRun:
    s = get_settings()
    cfg = _filter_config()
    as_of = as_of or datetime.now(UTC).date()
    sr.status, sr.started_at, sr.heartbeat_at = "RUNNING", datetime.now(UTC), datetime.now(UTC)
    sr.as_of, sr.model_version, sr.data_source = as_of, MODEL_VERSION, adapter.info().name
    universe = active_universe(db)
    if not universe:
        raise AppError("Universo vuoto: aggiungi titoli o carica la lista iniziale.", code="empty_universe")
    bench, vix_t, rates_t = regime_tickers(adapter)
    sr.total = len(universe)
    db.commit()
    errors: list[dict] = []
    refresh = bool((sr.params or {}).get("refresh_data", True))
    refresh_info: dict[str, dict] = {}

    # 1) dati
    if refresh:
        for t in dict.fromkeys(bench + [x for x in (vix_t, rates_t) if x]):
            try:
                refresh_ticker(db, adapter, t, as_of, index=t.endswith(".INDEX"))
                db.commit()
            except (DataProviderError, AppError) as e:
                db.rollback()
                errors.append({"ticker": t, "stage": "benchmark", "error": getattr(e, "message", str(e))})
        for i, t in enumerate(universe, 1):
            try:
                r = refresh_ticker(db, adapter, t, as_of)
                refresh_info[t] = r
                for k in ("quote_error", "fundamentals_error", "earnings_error"):
                    if k in r:
                        errors.append({"ticker": t, "stage": k.split("_")[0], "error": r[k][:300]})
                sr.progress = i
                sr.heartbeat_at = datetime.now(UTC)
                db.commit()
            except (DataProviderError, AppError, ValueError) as e:
                db.rollback()
                errors.append({"ticker": t, "stage": "data", "error": getattr(e, "message", str(e))[:300]})
            if not adapter.is_demo:
                time.sleep(s.scan_request_pause_seconds)

    # 2) regime
    states = []
    closes: dict[str, pd.Series] = {}
    for t in bench:
        inst = find_instrument(db, t)
        if inst:
            df = bars_frame(db, inst.id, as_of)
            if len(df):
                closes[t] = df["adj_close"].fillna(df["close"])
                st = regime_mod.benchmark_state(t, closes[t], df["source"].iloc[-1])
                if st:
                    states.append(st)
    aux = {}
    for t in (vix_t, rates_t):
        inst = find_instrument(db, t) if t else None
        df = bars_frame(db, inst.id, as_of) if inst else None
        aux[t] = (df["close"], df["source"].iloc[-1]) if df is not None and len(df) else (None, None)
    reg = regime_mod.classify(states, aux.get(vix_t, (None, None))[0], aux.get(rates_t, (None, None))[0],
                              aux.get(vix_t, (None, None))[1], aux.get(rates_t, (None, None))[1])
    sr.regime = reg.to_dict()

    # 3) candidati
    cands: list[Candidate] = []
    lows: dict[int, pd.Series] = {}
    for t in universe:
        inst = find_instrument(db, t)
        if inst is None:
            continue
        df = bars_frame(db, inst.id, as_of)
        c = Candidate(instrument_id=inst.id, ticker=t, instrument_type=inst.instrument_type, sector=inst.sector,
                      exchange=inst.exchange, price=float(df["close"].iloc[-1]) if len(df) else None,
                      price_date=df.index[-1].date() if len(df) else None,
                      source=df["source"].iloc[-1] if len(df) else inst.data_source,
                      is_demo=inst.is_demo or (bool(df["is_demo"].iloc[-1]) if len(df) else False),
                      market_cap=float(inst.market_cap) if inst.market_cap is not None else None)
        if len(df) >= 30:
            bt = rs_benchmark(adapter, inst.exchange)
            bench_close = closes.get(bt) if bt != t else None
            ind = indicators.compute_all(df, bench_close)
            c.features = {k: ind[k].iloc[-1] for k in ind.columns}
            sup, res = indicators.support_resistance(df["high"], df["low"], df["close"])
            c.features.update({"support": sup, "resistance": res})
            c.setup = setups.detect(ind, df["low"])
        c.spread_pct = _spread(db, inst.id, as_of)
        if inst.instrument_type == "STOCK":
            c.fundamentals = _latest_fundamentals(db, inst.id, as_of)
            ev = db.execute(select(CorporateEvent).where(
                CorporateEvent.instrument_id == inst.id, CorporateEvent.event_type == "EARNINGS",
                CorporateEvent.event_date >= as_of).order_by(CorporateEvent.event_date).limit(1)).scalar_one_or_none()
            recent_check = db.execute(select(func.max(CorporateEvent.retrieved_at)).where(
                CorporateEvent.instrument_id == inst.id, CorporateEvent.event_type == "EARNINGS")).scalar()
            recent = recent_check is not None and (datetime.now(UTC) - (
                recent_check if recent_check.tzinfo else recent_check.replace(tzinfo=UTC))) < timedelta(days=3)
            c.earnings_known = bool(ev) or bool(refresh_info.get(t, {}).get("earnings_checked")) or recent
            c.days_to_earnings = (ev.event_date - as_of).days if ev else None
        apply_filters(c, df, as_of, cfg)
        cands.append(c)
        lows[inst.id] = df["low"] if len(df) else pd.Series(dtype=float)

    score_universe(cands, reg.score if reg.regime != "UNKNOWN" else None)

    # 4) persistenza
    sv = get_strategy_version(db, cfg)
    now = datetime.now(UTC)
    for c in cands:
        db.add(TradingSignal(
            scan_run_id=sr.id, instrument_id=c.instrument_id, strategy_version_id=sv.id, analysis_time=now,
            data_source=c.source, data_as_of=datetime.combine(c.price_date or as_of, datetime.min.time(), UTC),
            market_regime=reg.regime, decision="WAIT" if c.eligible and c.score is not None else "REJECT",
            score=Decimal(str(c.score)) if c.score is not None else None,
            factor_scores={k: (round(v, 2) if v is not None else None) for k, v in c.factors.items()},
            rationale={"for": c.rationale_for, "against": c.rationale_against,
                       "could_be_wrong_if": [
                           "Il regime di mercato cambia (es. rottura dei minimi del benchmark)",
                           "Il setup viene invalidato (chiusura sotto il supporto o l'EMA di riferimento)",
                           "I pesi del punteggio non reggono la validazione statistica (Fase 5)",
                       ]},
            is_demo=c.is_demo, eligible=c.eligible, setup=c.setup.setup if c.setup else None,
            strategy_style=c.style, horizon=c.horizon, risk_level=c.risk_level, coverage=Decimal(str(c.coverage)),
            price=Decimal(str(round(c.price, 6))) if c.price is not None else None, price_date=c.price_date,
            details=candidate_payload(c),
        ))
    ranked = [c for c in cands if c.score is not None]
    sr.stats = {"universe": len(universe), "analysed": len(cands), "eligible": sum(c.eligible for c in cands),
                "scored": len(ranked), "excluded": sum(not c.eligible for c in cands), "errors": len(errors),
                "top": [c.ticker for c in sorted(ranked, key=lambda x: -x.score)[:5]]}
    sr.errors = errors[:500]
    sr.status, sr.finished_at, sr.progress = "DONE", datetime.now(UTC), len(universe)
    audit.record(db, actor=sr.requested_by, action="scan.completed", entity="scan_run", entity_id=sr.id,
                 payload={"as_of": as_of, "regime": reg.regime, "stats": sr.stats, "model_version": MODEL_VERSION})
    db.commit()
    return sr


def latest_done_scan(db: Session) -> ScanRun | None:
    return db.execute(select(ScanRun).where(ScanRun.status == "DONE").order_by(ScanRun.finished_at.desc()).limit(1)).scalar_one_or_none()
