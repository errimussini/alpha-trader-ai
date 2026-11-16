from datetime import date, datetime
from decimal import Decimal
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, EmailStr, Field

T = TypeVar("T")


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int
    page_size: int


# ---------------------------------------------------------------- auth
class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class UserOut(ORM):
    id: int
    email: str
    role: str
    is_active: bool


class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=10)
    role: str = Field(pattern="^(admin|analyst|viewer)$", default="viewer")


# ---------------------------------------------------------------- system
class SystemStatus(BaseModel):
    app: str
    mode: str
    available_modes: list[str]
    live_trading_enabled: bool
    kill_switch: dict
    market_data: dict
    database: str
    redis: str
    max_quote_age_minutes: int


class ModeIn(BaseModel):
    mode: str


class KillSwitchIn(BaseModel):
    active: bool
    reason: str | None = Field(default=None, max_length=500)


# ---------------------------------------------------------------- market
class InstrumentOut(ORM):
    id: int
    symbol: str
    exchange: str
    isin: str | None
    name: str | None
    instrument_type: str
    currency: str
    country: str | None
    sector: str | None
    industry: str | None
    market_cap: Decimal | None
    is_demo: bool
    data_source: str


class QuoteOut(ORM):
    id: int
    instrument_id: int
    price: Decimal
    bid: Decimal | None
    ask: Decimal | None
    previous_close: Decimal | None
    volume: int | None
    currency: str
    quote_time: datetime
    retrieved_at: datetime
    source: str
    delay_minutes: int | None
    is_demo: bool


class BarOut(ORM):
    bar_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    adj_close: Decimal | None
    volume: int | None
    source: str
    is_demo: bool


class OverviewItem(BaseModel):
    ticker: str
    name: str | None
    price: Decimal | None
    change_pct: Decimal | None
    quote_time: datetime | None
    source: str | None
    is_demo: bool
    stale: bool
    status: str  # OK / STALE / NO_DATA


class MarketOverview(BaseModel):
    provider: dict
    items: list[OverviewItem]
    regime: dict
    generated_at: datetime


class SyncIn(BaseModel):
    tickers: list[str] = Field(min_length=1, max_length=50)
    years: int = Field(default=3, ge=1, le=20)


class SyncResult(BaseModel):
    ticker: str
    ok: bool
    instrument_id: int | None = None
    bars_added: int | None = None
    quote_time: datetime | None = None
    source: str | None = None
    is_demo: bool | None = None
    error: str | None = None


# ---------------------------------------------------------------- risk
class RiskCheckIn(BaseModel):
    instrument_id: int | None = None
    entry_price: Decimal = Field(gt=0)
    stop_price: Decimal = Field(gt=0)
    take_profit_1: Decimal | None = Field(default=None, gt=0)
    take_profit_2: Decimal | None = Field(default=None, gt=0)
    tp1_close_fraction: Decimal = Field(default=Decimal("0.5"), gt=0, le=1)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    fx_rate: Decimal | None = Field(default=None, gt=0, description="Unità di valuta strumento per 1 EUR; se assente viene letto dal provider")
    lot_size: Decimal = Field(default=Decimal("1"), gt=0)
    sector: str | None = None
    available_cash_override: Decimal | None = Field(default=None, gt=0, description="Liquidità da considerare, es. 'con 2.000 € disponibili'")
    gap_stress_pct: Decimal = Field(default=Decimal("3"), ge=0, le=50)


class RiskCheckOut(BaseModel):
    assessment_id: int
    approved: bool
    result: dict
    inputs: dict


# ---------------------------------------------------------------- portfolio
class PositionOut(BaseModel):
    id: int
    instrument_id: int
    ticker: str
    name: str | None
    sector: str | None
    quantity: Decimal
    avg_price: Decimal
    currency: str
    last_price: Decimal | None
    price_time: datetime | None
    price_source: str | None
    market_value_base: Decimal
    unrealized_pnl_base: Decimal
    stop_loss: Decimal | None
    take_profit_1: Decimal | None
    risk_to_stop_base: Decimal | None
    stale: bool
    is_demo: bool
    opened_at: datetime


class PortfolioOut(BaseModel):
    id: int
    name: str
    mode: str
    base_currency: str
    initial_capital: Decimal
    cash: Decimal
    invested_cost: Decimal
    market_value: Decimal
    equity: Decimal
    unrealized_pnl: Decimal
    realized_pnl: Decimal
    cumulative_return_pct: Decimal
    daily_return_pct: Decimal | None
    drawdown_pct: Decimal
    aggregate_risk_base: Decimal
    aggregate_risk_pct: Decimal
    risk_params: dict
    open_positions: int
    warnings: list[str]
    valued_at: datetime


class PaperOrderIn(BaseModel):
    instrument_id: int
    side: str = Field(pattern="^(BUY|SELL)$")
    order_type: str = Field(pattern="^(MARKET|LIMIT)$", default="MARKET")
    quantity: Decimal = Field(gt=0)
    limit_price: Decimal | None = Field(default=None, gt=0)
    stop_loss: Decimal | None = Field(default=None, gt=0)
    take_profit_1: Decimal | None = Field(default=None, gt=0)
    client_order_id: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_\-]+$")
    trade_plan_id: int | None = None


class OrderOut(ORM):
    id: int
    client_order_id: str
    instrument_id: int
    side: str
    order_type: str
    quantity: Decimal
    filled_quantity: Decimal
    limit_price: Decimal | None
    status: str
    reject_reason: str | None
    mode: str
    broker: str
    is_demo: bool
    created_at: datetime


class PaperOrderOut(BaseModel):
    order: OrderOut
    duplicate: bool


class AlertOut(ORM):
    id: int
    kind: str
    severity: str
    message: str
    instrument_id: int | None
    portfolio_id: int | None
    data: dict
    created_at: datetime
    acknowledged_at: datetime | None


class TradePlanOut(ORM):
    id: int
    instrument_id: int
    model_version: str
    direction: str
    strategy_style: str
    price_reference: Decimal
    price_source: str
    price_time: datetime
    entry_price: Decimal
    entry_low: Decimal | None
    entry_high: Decimal | None
    stop_loss: Decimal
    take_profit_1: Decimal
    take_profit_2: Decimal | None
    quantity: Decimal
    notional_base: Decimal
    est_loss_at_stop_base: Decimal
    est_gain_tp1_base: Decimal | None
    est_gain_tp2_base: Decimal | None
    reward_risk_net: Decimal | None
    calibrated_probability: Decimal | None
    horizon_days: int
    rationale_for: list
    rationale_against: list
    invalidation_conditions: list
    status: str
    valid_until: datetime
    is_demo: bool
    created_at: datetime
    portfolio_id: int | None = None
    signal_id: int | None = None
    setup: str | None = None
    score: Decimal | None = None
    risk_level: str | None = None
    market_regime: str | None = None
    status_reason: str | None = None
    tp1_close_fraction: Decimal | None = None
    trailing_stop: dict | None = None
    entry_conditions: list = []
    avoid_conditions: list = []
    early_exit_conditions: list = []
    stop_rationale: dict = {}
    scenarios: list = []
    correlations: dict = {}
    gross_return_tp1_pct: Decimal | None = None
    net_return_tp1_pct: Decimal | None = None
    expected_value_base: Decimal | None = None
    currency: str | None = None
    fx_rate: Decimal | None = None
    committee: dict = {}
    calibration: dict = {}
    ticker: str | None = None
    name: str | None = None
    isin: str | None = None
    sector: str | None = None


class TradePlanCreate(BaseModel):
    signal_id: int | None = None
    scan_id: int | None = None
    top_n: int = Field(default=10, ge=1, le=50)


class AuditOut(ORM):
    id: int
    created_at: datetime
    actor: str
    action: str
    entity: str | None
    entity_id: str | None
    payload: dict
    hash: str
