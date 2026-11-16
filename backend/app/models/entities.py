"""Schema del database di ALPHA TRADER AI.

Principi:
- Ogni dato di mercato registra fonte (`source`), istante del dato e istante di acquisizione.
- `is_demo=True` marca qualunque dato dimostrativo/sintetico: non può generare piani READY.
- AuditLog è append-only e concatenato via hash (in PostgreSQL un trigger vieta UPDATE/DELETE).
"""
from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base

PRICE = Numeric(20, 8)
MONEY = Numeric(20, 4)
QTY = Numeric(20, 6)


def utcnow() -> datetime:
    return datetime.now(UTC)


def _ts(**kw):
    return mapped_column(DateTime(timezone=True), **kw)


# ---------------------------------------------------------------- enums
class InstrumentType(StrEnum):
    STOCK = "STOCK"
    ETF = "ETF"


class TradingMode(StrEnum):
    RESEARCH = "RESEARCH"
    PAPER = "PAPER"
    LIVE_ASSISTED = "LIVE_ASSISTED"  # non attivabile nella prima release


class Decision(StrEnum):
    BUY = "BUY"
    WAIT = "WAIT"
    HOLD = "HOLD"
    REDUCE = "REDUCE"
    EXIT = "EXIT"
    REJECT = "REJECT"


class PlanStatus(StrEnum):
    READY = "READY"
    WAIT = "WAIT"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class OrderStatus(StrEnum):
    NEW = "NEW"
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


class AlertKind(StrEnum):
    ENTRY_OPPORTUNITY = "ENTRY_OPPORTUNITY"
    SETUP_INVALIDATED = "SETUP_INVALIDATED"
    STOP_HIT = "STOP_HIT"
    TARGET_HIT = "TARGET_HIT"
    REVIEW_POSITION = "REVIEW_POSITION"
    AGGREGATE_RISK = "AGGREGATE_RISK"
    DATA_ANOMALY = "DATA_ANOMALY"
    BROKER_ANOMALY = "BROKER_ANOMALY"
    SYSTEM = "SYSTEM"


class Severity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


# ---------------------------------------------------------------- users / system
class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    hashed_password: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20), default="viewer")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = _ts(default=utcnow)


class SystemSetting(Base):
    """Stato operativo persistente (modalità, kill switch)."""
    __tablename__ = "system_settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = _ts(default=utcnow, onupdate=utcnow)
    updated_by: Mapped[str | None] = mapped_column(String(255))


# ---------------------------------------------------------------- market data
class Instrument(Base):
    __tablename__ = "instruments"
    __table_args__ = (UniqueConstraint("symbol", "exchange", name="uq_instrument_symbol_exchange"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    exchange: Mapped[str] = mapped_column(String(16))
    isin: Mapped[str | None] = mapped_column(String(12), index=True)
    name: Mapped[str | None] = mapped_column(String(255))
    instrument_type: Mapped[str] = mapped_column(String(10), default=InstrumentType.STOCK)
    currency: Mapped[str] = mapped_column(String(3))
    country: Mapped[str | None] = mapped_column(String(64))
    sector: Mapped[str | None] = mapped_column(String(128))
    industry: Mapped[str | None] = mapped_column(String(128))
    market_cap: Mapped[Decimal | None] = mapped_column(Numeric(24, 2))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)
    data_source: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = _ts(default=utcnow)
    updated_at: Mapped[datetime] = _ts(default=utcnow, onupdate=utcnow)


class MarketQuote(Base):
    __tablename__ = "market_quotes"
    __table_args__ = (Index("ix_quote_instrument_time", "instrument_id", "quote_time"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id", ondelete="CASCADE"))
    price: Mapped[Decimal] = mapped_column(PRICE)
    bid: Mapped[Decimal | None] = mapped_column(PRICE)
    ask: Mapped[Decimal | None] = mapped_column(PRICE)
    previous_close: Mapped[Decimal | None] = mapped_column(PRICE)
    volume: Mapped[int | None] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3))
    quote_time: Mapped[datetime] = _ts()
    retrieved_at: Mapped[datetime] = _ts(default=utcnow)
    source: Mapped[str] = mapped_column(String(64))
    delay_minutes: Mapped[int | None] = mapped_column(Integer)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)
    instrument: Mapped[Instrument] = relationship()


class HistoricalBar(Base):
    __tablename__ = "historical_bars"
    __table_args__ = (UniqueConstraint("instrument_id", "timeframe", "bar_date", name="uq_bar"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id", ondelete="CASCADE"), index=True)
    timeframe: Mapped[str] = mapped_column(String(4), default="1d")
    bar_date: Mapped[date] = mapped_column(Date)
    open: Mapped[Decimal] = mapped_column(PRICE)
    high: Mapped[Decimal] = mapped_column(PRICE)
    low: Mapped[Decimal] = mapped_column(PRICE)
    close: Mapped[Decimal] = mapped_column(PRICE)
    adj_close: Mapped[Decimal | None] = mapped_column(PRICE)
    volume: Mapped[int | None] = mapped_column(BigInteger)
    source: Mapped[str] = mapped_column(String(64))
    retrieved_at: Mapped[datetime] = _ts(default=utcnow)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)


class FundamentalSnapshot(Base):
    """Snapshot point-in-time: `as_of` è l'istante in cui il dato era noto, non la data del periodo."""
    __tablename__ = "fundamental_snapshots"
    id: Mapped[int] = mapped_column(primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id", ondelete="CASCADE"), index=True)
    as_of: Mapped[datetime] = _ts()
    period_end: Mapped[date | None] = mapped_column(Date)
    period_type: Mapped[str | None] = mapped_column(String(8))  # Q / FY / TTM
    revenue: Mapped[Decimal | None] = mapped_column(Numeric(24, 2))
    eps: Mapped[Decimal | None] = mapped_column(Numeric(16, 4))
    operating_margin: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    free_cash_flow: Mapped[Decimal | None] = mapped_column(Numeric(24, 2))
    net_debt: Mapped[Decimal | None] = mapped_column(Numeric(24, 2))
    roe: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    roic: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    pe_ratio: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    ev_ebitda: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    source: Mapped[str] = mapped_column(String(64))
    retrieved_at: Mapped[datetime] = _ts(default=utcnow)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)


class CorporateEvent(Base):
    __tablename__ = "corporate_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id", ondelete="CASCADE"), index=True)
    event_type: Mapped[str] = mapped_column(String(32))  # EARNINGS, DIVIDEND, SPLIT, SUSPENSION, OTHER
    event_date: Mapped[date] = mapped_column(Date, index=True)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    source: Mapped[str] = mapped_column(String(64))
    retrieved_at: Mapped[datetime] = _ts(default=utcnow)


class NewsEvent(Base):
    __tablename__ = "news_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    instrument_id: Mapped[int | None] = mapped_column(ForeignKey("instruments.id", ondelete="SET NULL"), index=True)
    title: Mapped[str] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime] = _ts(index=True)
    sentiment: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))  # come fornito dalla fonte
    reliability: Mapped[str | None] = mapped_column(String(16))      # HIGH / MEDIUM / LOW / UNKNOWN
    source: Mapped[str] = mapped_column(String(64))
    retrieved_at: Mapped[datetime] = _ts(default=utcnow)


# ---------------------------------------------------------------- strategy / signals
class Strategy(Base):
    __tablename__ = "strategies"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    style: Mapped[str] = mapped_column(String(32))  # SWING / POSITION
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _ts(default=utcnow)


class StrategyVersion(Base):
    __tablename__ = "strategy_versions"
    __table_args__ = (UniqueConstraint("strategy_id", "version", name="uq_strategy_version"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    strategy_id: Mapped[int] = mapped_column(ForeignKey("strategies.id", ondelete="CASCADE"))
    version: Mapped[str] = mapped_column(String(32))
    parameters: Mapped[dict] = mapped_column(JSON, default=dict)
    code_hash: Mapped[str | None] = mapped_column(String(64))
    is_validated: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = _ts(default=utcnow)


class ModelEvaluation(Base):
    __tablename__ = "model_evaluations"
    id: Mapped[int] = mapped_column(primary_key=True)
    strategy_version_id: Mapped[int] = mapped_column(ForeignKey("strategy_versions.id", ondelete="CASCADE"))
    evaluation_type: Mapped[str] = mapped_column(String(32))  # BACKTEST / WALK_FORWARD / PROSPECTIVE
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    is_out_of_sample: Mapped[bool] = mapped_column(Boolean)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = _ts(default=utcnow)


class TradingSignal(Base):
    __tablename__ = "trading_signals"
    id: Mapped[int] = mapped_column(primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id"), index=True)
    strategy_version_id: Mapped[int] = mapped_column(ForeignKey("strategy_versions.id"))
    analysis_time: Mapped[datetime] = _ts(index=True)
    data_source: Mapped[str] = mapped_column(String(64))
    data_as_of: Mapped[datetime] = _ts()
    market_regime: Mapped[str] = mapped_column(String(32))
    decision: Mapped[str] = mapped_column(String(10))
    score: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    factor_scores: Mapped[dict] = mapped_column(JSON, default=dict)
    rationale: Mapped[dict] = mapped_column(JSON, default=dict)  # pro, contro, condizioni di errore
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = _ts(default=utcnow)


class TradePlan(Base):
    __tablename__ = "trade_plans"
    id: Mapped[int] = mapped_column(primary_key=True)
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("trading_signals.id"))
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id"), index=True)
    strategy_version_id: Mapped[int | None] = mapped_column(ForeignKey("strategy_versions.id"))
    model_version: Mapped[str] = mapped_column(String(64))
    direction: Mapped[str] = mapped_column(String(5), default="LONG")
    strategy_style: Mapped[str] = mapped_column(String(32))
    price_reference: Mapped[Decimal] = mapped_column(PRICE)
    price_source: Mapped[str] = mapped_column(String(64))
    price_time: Mapped[datetime] = _ts()
    entry_price: Mapped[Decimal] = mapped_column(PRICE)
    entry_low: Mapped[Decimal | None] = mapped_column(PRICE)
    entry_high: Mapped[Decimal | None] = mapped_column(PRICE)
    stop_loss: Mapped[Decimal] = mapped_column(PRICE)
    take_profit_1: Mapped[Decimal] = mapped_column(PRICE)
    take_profit_2: Mapped[Decimal | None] = mapped_column(PRICE)
    tp1_close_fraction: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    trailing_stop: Mapped[dict | None] = mapped_column(JSON)
    quantity: Mapped[Decimal] = mapped_column(QTY)
    notional_base: Mapped[Decimal] = mapped_column(MONEY)
    est_loss_at_stop_base: Mapped[Decimal] = mapped_column(MONEY)
    est_gain_tp1_base: Mapped[Decimal | None] = mapped_column(MONEY)
    est_gain_tp2_base: Mapped[Decimal | None] = mapped_column(MONEY)
    reward_risk_net: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    calibrated_probability: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))  # NULL = non disponibile
    horizon_days: Mapped[int] = mapped_column(Integer)
    rationale_for: Mapped[list] = mapped_column(JSON, default=list)
    rationale_against: Mapped[list] = mapped_column(JSON, default=list)
    invalidation_conditions: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(10), index=True)
    valid_until: Mapped[datetime] = _ts()
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = _ts(default=utcnow)
    instrument: Mapped[Instrument] = relationship()


class RiskAssessment(Base):
    __tablename__ = "risk_assessments"
    id: Mapped[int] = mapped_column(primary_key=True)
    trade_plan_id: Mapped[int | None] = mapped_column(ForeignKey("trade_plans.id"))
    portfolio_id: Mapped[int | None] = mapped_column(ForeignKey("portfolios.id"))
    approved: Mapped[bool] = mapped_column(Boolean)
    inputs: Mapped[dict] = mapped_column(JSON)
    result: Mapped[dict] = mapped_column(JSON)
    blocking_reasons: Mapped[list] = mapped_column(JSON, default=list)
    engine_version: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = _ts(default=utcnow)


# ---------------------------------------------------------------- portfolio / orders
class Portfolio(Base):
    """Portafoglio operativo (simulato). Distinto dal patrimonio complessivo dell'utente."""
    __tablename__ = "portfolios"
    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(String(128))
    mode: Mapped[str] = mapped_column(String(16), default=TradingMode.PAPER)
    base_currency: Mapped[str] = mapped_column(String(3), default="EUR")
    initial_capital: Mapped[Decimal] = mapped_column(MONEY)
    cash: Mapped[Decimal] = mapped_column(MONEY)
    realized_pnl: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    peak_equity: Mapped[Decimal] = mapped_column(MONEY)
    risk_params: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = _ts(default=utcnow)


class Position(Base):
    __tablename__ = "positions"
    id: Mapped[int] = mapped_column(primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("portfolios.id", ondelete="CASCADE"), index=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id"))
    trade_plan_id: Mapped[int | None] = mapped_column(ForeignKey("trade_plans.id"))
    quantity: Mapped[Decimal] = mapped_column(QTY)
    avg_price: Mapped[Decimal] = mapped_column(PRICE)
    currency: Mapped[str] = mapped_column(String(3))
    fx_rate_at_open: Mapped[Decimal] = mapped_column(Numeric(20, 10), default=Decimal("1"))
    stop_loss: Mapped[Decimal | None] = mapped_column(PRICE)
    take_profit_1: Mapped[Decimal | None] = mapped_column(PRICE)
    take_profit_2: Mapped[Decimal | None] = mapped_column(PRICE)
    status: Mapped[str] = mapped_column(String(10), default="OPEN", index=True)
    realized_pnl_base: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    opened_at: Mapped[datetime] = _ts(default=utcnow)
    closed_at: Mapped[datetime | None] = _ts()
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)
    instrument: Mapped[Instrument] = relationship()


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("portfolios.id", ondelete="CASCADE"), index=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id"))
    trade_plan_id: Mapped[int | None] = mapped_column(ForeignKey("trade_plans.id"))
    client_order_id: Mapped[str] = mapped_column(String(64), unique=True)  # idempotenza / anti-duplicati
    broker: Mapped[str] = mapped_column(String(32))
    mode: Mapped[str] = mapped_column(String(16))
    side: Mapped[str] = mapped_column(String(4))
    order_type: Mapped[str] = mapped_column(String(8))
    quantity: Mapped[Decimal] = mapped_column(QTY)
    filled_quantity: Mapped[Decimal] = mapped_column(QTY, default=Decimal("0"))
    limit_price: Mapped[Decimal | None] = mapped_column(PRICE)
    status: Mapped[str] = mapped_column(String(20), index=True)
    reject_reason: Mapped[str | None] = mapped_column(Text)
    reference_quote_id: Mapped[int | None] = mapped_column(ForeignKey("market_quotes.id"))
    created_by: Mapped[str] = mapped_column(String(255))
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = _ts(default=utcnow)
    updated_at: Mapped[datetime] = _ts(default=utcnow, onupdate=utcnow)
    instrument: Mapped[Instrument] = relationship()


class Execution(Base):
    __tablename__ = "executions"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    quantity: Mapped[Decimal] = mapped_column(QTY)
    price: Mapped[Decimal] = mapped_column(PRICE)
    fee: Mapped[Decimal] = mapped_column(MONEY)
    slippage: Mapped[Decimal] = mapped_column(PRICE, default=Decimal("0"))
    fx_rate: Mapped[Decimal] = mapped_column(Numeric(20, 10), default=Decimal("1"))
    realized_pnl_base: Mapped[Decimal | None] = mapped_column(MONEY)
    executed_at: Mapped[datetime] = _ts(default=utcnow)
    venue: Mapped[str] = mapped_column(String(32))


class CashMovement(Base):
    __tablename__ = "cash_movements"
    id: Mapped[int] = mapped_column(primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("portfolios.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(16))  # DEPOSIT, WITHDRAWAL, TRADE, FEE
    amount_base: Mapped[Decimal] = mapped_column(MONEY)
    currency: Mapped[str] = mapped_column(String(3))
    reference: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = _ts(default=utcnow)


class Alert(Base):
    __tablename__ = "alerts"
    id: Mapped[int] = mapped_column(primary_key=True)
    portfolio_id: Mapped[int | None] = mapped_column(ForeignKey("portfolios.id", ondelete="CASCADE"))
    instrument_id: Mapped[int | None] = mapped_column(ForeignKey("instruments.id"))
    kind: Mapped[str] = mapped_column(String(32))
    severity: Mapped[str] = mapped_column(String(10))
    message: Mapped[str] = mapped_column(Text)
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = _ts(default=utcnow, index=True)
    acknowledged_at: Mapped[datetime | None] = _ts()


class AuditLog(Base):
    """Registro immutabile: ogni riga contiene l'hash della precedente (catena verificabile)."""
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = _ts(default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(255))
    action: Mapped[str] = mapped_column(String(64), index=True)
    entity: Mapped[str | None] = mapped_column(String(64))
    entity_id: Mapped[str | None] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    prev_hash: Mapped[str | None] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64), unique=True)
