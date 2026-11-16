"""PaperBroker: esecuzione simulata deterministica.

Regole (conservative, documentate):
- MARKET BUY  : eseguito all'ask (o al prezzo se l'ask manca) + slippage in bps.
- MARKET SELL : eseguito al bid (o al prezzo) - slippage.
- LIMIT BUY   : eseguito solo se ask+slippage <= limite, al prezzo min(limite, ask+slippage);
                altrimenti l'ordine resta OPEN e il monitoraggio (Fase 6) lo rivaluta a ogni ciclo.
- LIMIT SELL  : simmetrico.
- Partecipazione al volume (opzionale, max_participation_pct > 0): a ogni invio è eseguita al massimo
                quella percentuale del volume della seduta riportato dalla quotazione; il residuo resta
                aperto (PARTIALLY_FILLED). Se il volume non è noto il limite non si applica.
- Commissione : fissa per ordine + percentuale sul controvalore (applicata a ogni tranche eseguita).
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

from app.adapters.broker.base import BrokerAdapter, BrokerOrderResult, Fill, MarketSnapshot, OrderRequest

_PX = Decimal("0.00000001")
_MONEY = Decimal("0.0001")


class PaperBroker(BrokerAdapter):
    name = "paper"
    is_live = False

    def __init__(self, commission_per_order: Decimal, commission_pct: Decimal, slippage_bps: Decimal,
                 max_participation_pct: Decimal = Decimal("0")):
        self.fixed_fee = commission_per_order
        self.pct_fee = commission_pct
        self.slip = slippage_bps / Decimal("10000")
        self.participation = max_participation_pct / Decimal("100")

    def is_tradable(self, symbol: str, exchange: str) -> bool:
        return True  # il broker simulato accetta ogni strumento con quotazione verificata

    def fee(self, notional: Decimal) -> Decimal:
        return (self.fixed_fee + notional * self.pct_fee / Decimal("100")).quantize(_MONEY, ROUND_HALF_UP)

    def submit_order(self, req: OrderRequest, market: MarketSnapshot) -> BrokerOrderResult:
        if req.quantity <= 0:
            return BrokerOrderResult("REJECTED", reject_reason="quantità non positiva")
        if req.order_type == "LIMIT" and (req.limit_price is None or req.limit_price <= 0):
            return BrokerOrderResult("REJECTED", reject_reason="prezzo limite mancante o non valido")

        if req.side == "BUY":
            ref = market.ask or market.price
            exec_px = ref * (1 + self.slip)
            if req.order_type == "LIMIT":
                if exec_px > req.limit_price:
                    return BrokerOrderResult("OPEN")
                exec_px = min(exec_px, req.limit_price)
        else:
            ref = market.bid or market.price
            exec_px = ref * (1 - self.slip)
            if req.order_type == "LIMIT":
                if exec_px < req.limit_price:
                    return BrokerOrderResult("OPEN")
                exec_px = max(exec_px, req.limit_price)

        qty = req.quantity
        if self.participation > 0 and market.volume:
            cap = (Decimal(market.volume) * self.participation).to_integral_value(ROUND_DOWN)
            if cap <= 0:
                return BrokerOrderResult("OPEN")
            qty = min(qty, cap)
        exec_px = exec_px.quantize(_PX, ROUND_HALF_UP)
        fill = Fill(
            quantity=qty, price=exec_px, fee=self.fee(exec_px * qty),
            slippage=abs(exec_px - ref).quantize(_PX), executed_at=datetime.now(UTC),
        )
        return BrokerOrderResult("FILLED" if qty == req.quantity else "PARTIALLY_FILLED", fills=[fill])
