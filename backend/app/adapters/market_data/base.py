"""Interfaccia astratta per i provider di dati di mercato.

Ogni oggetto restituito porta con sé `source`, l'istante del dato e `is_demo`.
Un adapter non deve MAI inventare un valore mancante: se il provider non lo fornisce,
il campo resta None oppure viene sollevata DataProviderError.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal


class DataProviderError(Exception):
    def __init__(self, provider: str, message: str, *, status: int | None = None):
        super().__init__(f"[{provider}] {message}")
        self.provider = provider
        self.status = status


@dataclass(frozen=True)
class InstrumentInfo:
    symbol: str
    exchange: str
    name: str | None
    currency: str
    instrument_type: str  # STOCK / ETF
    isin: str | None = None
    country: str | None = None
    sector: str | None = None
    industry: str | None = None
    market_cap: Decimal | None = None
    source: str = ""
    is_demo: bool = False


@dataclass(frozen=True)
class Bar:
    bar_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    adj_close: Decimal | None
    volume: int | None


@dataclass(frozen=True)
class BarSeries:
    symbol: str
    exchange: str
    bars: list[Bar]
    source: str
    retrieved_at: datetime
    is_demo: bool = False


@dataclass(frozen=True)
class Quote:
    symbol: str
    exchange: str
    price: Decimal
    quote_time: datetime
    source: str
    retrieved_at: datetime
    currency: str | None = None
    bid: Decimal | None = None
    ask: Decimal | None = None
    previous_close: Decimal | None = None
    volume: int | None = None
    delay_minutes: int | None = None
    is_demo: bool = False


@dataclass(frozen=True)
class EventInfo:
    symbol: str
    exchange: str
    event_type: str
    event_date: date
    details: dict = field(default_factory=dict)
    source: str = ""


@dataclass(frozen=True)
class NewsItem:
    title: str
    published_at: datetime
    url: str | None
    symbols: list[str]
    sentiment: Decimal | None
    source: str


@dataclass(frozen=True)
class ProviderInfo:
    name: str
    is_demo: bool
    markets: list[str]
    capabilities: list[str]
    license_note: str
    configured: bool


class MarketDataAdapter(ABC):
    name: str = "abstract"
    is_demo: bool = False

    @abstractmethod
    def info(self) -> ProviderInfo: ...

    @abstractmethod
    def get_instrument(self, symbol: str, exchange: str) -> InstrumentInfo: ...

    @abstractmethod
    def get_daily_bars(self, symbol: str, exchange: str, start: date, end: date) -> BarSeries: ...

    @abstractmethod
    def get_quote(self, symbol: str, exchange: str) -> Quote: ...

    def list_instruments(self, exchange: str) -> list[InstrumentInfo]:
        raise DataProviderError(self.name, "list_instruments non supportato da questo provider")

    def get_fundamentals(self, symbol: str, exchange: str) -> dict:
        raise DataProviderError(self.name, "fondamentali non supportati da questo provider")

    def get_earnings_calendar(self, symbols: list[str], start: date, end: date) -> list[EventInfo]:
        raise DataProviderError(self.name, "calendario eventi non supportato da questo provider")

    def get_news(self, symbol: str, limit: int = 20) -> list[NewsItem]:
        raise DataProviderError(self.name, "notizie non supportate da questo provider")

    def get_fx_rate(self, base: str, quote: str) -> Quote:
        """Tasso di cambio: quante unità di `quote` per 1 unità di `base` (es. EUR->USD)."""
        raise DataProviderError(self.name, "tassi di cambio non supportati da questo provider")


def split_ticker(ticker: str) -> tuple[str, str]:
    """'AAPL.US' -> ('AAPL', 'US'). L'exchange è obbligatorio per evitare ambiguità."""
    if "." not in ticker:
        raise ValueError(f"Ticker '{ticker}' senza exchange: usa il formato SIMBOLO.EXCHANGE (es. AAPL.US)")
    symbol, exchange = ticker.rsplit(".", 1)
    if not symbol or not exchange:
        raise ValueError(f"Ticker non valido: {ticker}")
    return symbol.upper(), exchange.upper()
