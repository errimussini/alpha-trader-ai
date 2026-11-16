"""Interfaccia per broker reali (Fase 8): SOLO architettura, spenta di default.

Un adapter live non va mai chiamato direttamente: l'unico punto d'ingresso è `services/live.py`, che verifica
modalità LIVE_ASSISTED, kill switch, dati freschi, limiti di rischio e la conferma umana con codice monouso.
Un test (`tests/test_live.py`) verifica che nessun altro modulo importi `get_live_broker`.

Per collegare un broker reale (es. Interactive Brokers, Alpaca, Saxo, Directa):
1. implementare `LiveBrokerAdapter` in un nuovo modulo, con credenziali lette SOLO da variabili d'ambiente
   lato server e mai registrate nei log o nell'audit;
2. ordini sempre LIMIT, `client_order_id` passato al broker per l'idempotenza;
3. aggiungere il nome a `LIVE_BROKER` in `core/config.py` e a `get_live_broker()`;
4. test di contratto con il broker in modalità sandbox/paper del broker stesso prima di qualsiasi uso reale.
"""
from __future__ import annotations

import uuid
from abc import abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from app.adapters.broker.base import BrokerAdapter, BrokerOrderResult, MarketSnapshot, OrderRequest
from app.core.config import get_settings


@dataclass(frozen=True)
class AccountSummary:
    equity_base: Decimal
    cash_base: Decimal
    currency: str
    as_of: datetime
    source: str
    is_simulated: bool


@dataclass(frozen=True)
class BrokerPosition:
    symbol: str
    exchange: str
    quantity: Decimal
    avg_price: Decimal
    currency: str


class LiveBrokerAdapter(BrokerAdapter):
    is_live = True
    sends_real_orders = True  # False solo per il broker di collaudo

    @abstractmethod
    def account_summary(self) -> AccountSummary: ...

    @abstractmethod
    def positions(self) -> list[BrokerPosition]: ...

    @abstractmethod
    def cancel_order(self, broker_order_id: str) -> BrokerOrderResult: ...

    @abstractmethod
    def order_status(self, broker_order_id: str) -> BrokerOrderResult: ...


class DryRunBroker(LiveBrokerAdapter):
    """Broker di COLLAUDO del flusso live: accetta formalmente gli ordini LIMIT e non li invia a nessuno.

    Serve a verificare end-to-end ticket, conferma, controlli e audit senza denaro reale. Ogni risposta è
    marcata `dry_run` e l'interfaccia lo dichiara.
    """
    name = "dry_run"
    sends_real_orders = False

    def __init__(self) -> None:
        self._orders: dict[str, BrokerOrderResult] = {}

    def is_tradable(self, symbol: str, exchange: str) -> bool:
        return True

    def account_summary(self) -> AccountSummary:
        s = get_settings()
        return AccountSummary(s.live_dry_run_equity, s.live_dry_run_equity, s.base_currency, datetime.now(UTC),
                              "dry_run (capitale fittizio di collaudo)", True)

    def positions(self) -> list[BrokerPosition]:
        return []

    def submit_order(self, req: OrderRequest, market: MarketSnapshot) -> BrokerOrderResult:
        if req.order_type != "LIMIT" or req.limit_price is None or req.quantity <= 0:
            return BrokerOrderResult("REJECTED", reject_reason="solo ordini LIMIT con quantità positiva")
        oid = f"DRY-{uuid.uuid4().hex[:12]}"
        res = BrokerOrderResult("OPEN", broker_order_id=oid, raw={
            "dry_run": True, "client_order_id": req.client_order_id, "symbol": req.symbol, "exchange": req.exchange,
            "side": req.side, "quantity": str(req.quantity), "limit_price": str(req.limit_price),
            "note": "Collaudo: nessun ordine è stato inviato a un mercato reale."})
        self._orders[oid] = res
        return res

    def cancel_order(self, broker_order_id: str) -> BrokerOrderResult:
        return BrokerOrderResult("CANCELLED", broker_order_id=broker_order_id, raw={"dry_run": True})

    def order_status(self, broker_order_id: str) -> BrokerOrderResult:
        return self._orders.get(broker_order_id) or BrokerOrderResult("UNKNOWN", broker_order_id=broker_order_id)


def get_live_broker() -> LiveBrokerAdapter | None:
    """Restituisce l'adapter live configurato o None. Da usare SOLO in services/live.py."""
    name = get_settings().live_broker
    if name == "none":
        return None
    if name == "dry_run":
        return DryRunBroker()
    raise RuntimeError(f"Broker live '{name}' non implementato")
