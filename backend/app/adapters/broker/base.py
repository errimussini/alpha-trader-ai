"""Interfaccia astratta verso un broker. Nessuna dipendenza obbligatoria da un singolo broker."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True)
class OrderRequest:
    client_order_id: str
    symbol: str
    exchange: str
    side: str          # BUY / SELL
    order_type: str    # MARKET / LIMIT
    quantity: Decimal
    limit_price: Decimal | None = None


@dataclass(frozen=True)
class MarketSnapshot:
    """Quotazione verificata usata dal broker simulato per decidere l'eseguito."""
    price: Decimal
    bid: Decimal | None
    ask: Decimal | None
    quote_time: datetime
    source: str


@dataclass(frozen=True)
class Fill:
    quantity: Decimal
    price: Decimal
    fee: Decimal
    slippage: Decimal
    executed_at: datetime


@dataclass
class BrokerOrderResult:
    status: str  # OPEN / FILLED / PARTIALLY_FILLED / REJECTED
    fills: list[Fill] = field(default_factory=list)
    reject_reason: str | None = None


class BrokerAdapter(ABC):
    name: str = "abstract"
    is_live: bool = False

    @abstractmethod
    def submit_order(self, req: OrderRequest, market: MarketSnapshot) -> BrokerOrderResult: ...

    @abstractmethod
    def is_tradable(self, symbol: str, exchange: str) -> bool: ...
