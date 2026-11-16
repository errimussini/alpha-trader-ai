"""Adapter DIMOSTRATIVO con serie sintetiche deterministiche.

ATTENZIONE: questi dati NON sono quotazioni reali. Gli strumenti hanno ticker fittizi
(DEMO1..DEMO5 sull'exchange "DEMO") per non poter essere confusi con titoli reali,
ogni record è marcato is_demo=True e la fonte è "DEMO (sintetico)".
Servono solo a far funzionare interfaccia e pipeline senza una chiave del provider.
"""
from __future__ import annotations

import hashlib
import math
import random
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from app.adapters.market_data.base import (
    Bar,
    BarSeries,
    DataProviderError,
    InstrumentInfo,
    MarketDataAdapter,
    ProviderInfo,
    Quote,
)

SOURCE = "DEMO (sintetico)"
EXCHANGE = "DEMO"

_UNIVERSE = {
    "DEMO1": ("Demo Instrument 1 (sintetico)", "EUR", "STOCK", "Demo Sector A", 40.0, 0.18),
    "DEMO2": ("Demo Instrument 2 (sintetico)", "USD", "STOCK", "Demo Sector B", 120.0, 0.28),
    "DEMO3": ("Demo Instrument 3 (sintetico)", "USD", "STOCK", "Demo Sector A", 75.0, 0.35),
    "DEMO4": ("Demo ETF 4 (sintetico)", "EUR", "ETF", "Demo Broad Market", 90.0, 0.14),
    "DEMO5": ("Demo ETF 5 (sintetico)", "USD", "ETF", "Demo Broad Market", 400.0, 0.16),
}

_Q = Decimal("0.0001")


def _seed(symbol: str) -> int:
    return int(hashlib.sha256(symbol.encode()).hexdigest()[:12], 16)


class DemoAdapter(MarketDataAdapter):
    name = "demo"
    is_demo = True

    def info(self) -> ProviderInfo:
        return ProviderInfo(
            name=SOURCE, is_demo=True, markets=[EXCHANGE], capabilities=["instruments", "daily_bars", "quotes"],
            license_note="Dati sintetici generati localmente. NON rappresentano alcun mercato reale.",
            configured=True,
        )

    @staticmethod
    def symbols() -> list[str]:
        return [f"{s}.{EXCHANGE}" for s in _UNIVERSE]

    def _check(self, symbol: str, exchange: str):
        if exchange != EXCHANGE or symbol not in _UNIVERSE:
            raise DataProviderError(self.name, f"{symbol}.{exchange} non esiste nell'universo DEMO")
        return _UNIVERSE[symbol]

    def get_instrument(self, symbol: str, exchange: str) -> InstrumentInfo:
        name, ccy, typ, sector, _, _ = self._check(symbol, exchange)
        return InstrumentInfo(symbol=symbol, exchange=exchange, name=name, currency=ccy, instrument_type=typ,
                              sector=sector, source=SOURCE, is_demo=True)

    def list_instruments(self, exchange: str) -> list[InstrumentInfo]:
        return [self.get_instrument(s, EXCHANGE) for s in _UNIVERSE] if exchange == EXCHANGE else []

    def _full_series(self, symbol: str, end: date) -> list[Bar]:
        _, _, _, _, p0, vol = _UNIVERSE[symbol]
        rng = random.Random(_seed(symbol))
        start = date(2018, 1, 1)
        d, price, bars = start, p0, []
        daily_sigma = vol / math.sqrt(252)
        while d <= end:
            if d.weekday() < 5:
                ret = rng.gauss(0.0002, daily_sigma)
                o = price * (1 + rng.gauss(0, daily_sigma / 3))
                c = price * math.exp(ret)
                h = max(o, c) * (1 + abs(rng.gauss(0, daily_sigma / 2)))
                low = min(o, c) * (1 - abs(rng.gauss(0, daily_sigma / 2)))
                q = lambda x: Decimal(str(x)).quantize(_Q, ROUND_HALF_UP)  # noqa: E731
                bars.append(Bar(d, q(o), q(h), q(low), q(c), q(c), int(rng.uniform(2e5, 3e6))))
                price = c
            d += timedelta(days=1)
        return bars

    def get_daily_bars(self, symbol: str, exchange: str, start: date, end: date) -> BarSeries:
        self._check(symbol, exchange)
        today = datetime.now(UTC).date()
        bars = [b for b in self._full_series(symbol, min(end, today)) if b.bar_date >= start]
        return BarSeries(symbol, exchange, bars, SOURCE, datetime.now(UTC), is_demo=True)

    def get_quote(self, symbol: str, exchange: str) -> Quote:
        _, ccy, *_ = self._check(symbol, exchange)
        now = datetime.now(UTC)
        series = self._full_series(symbol, now.date())
        last, prev = series[-1], series[-2]
        spread = (last.close * Decimal("0.0005")).quantize(_Q)
        return Quote(symbol=symbol, exchange=exchange, price=last.close, quote_time=now, source=SOURCE,
                     retrieved_at=now, currency=ccy, bid=last.close - spread, ask=last.close + spread,
                     previous_close=prev.close, volume=last.volume, delay_minutes=0, is_demo=True)

    def get_fx_rate(self, base: str, quote: str) -> Quote:
        """Cambio SINTETICO per far funzionare la demo multi-valuta. Non è un tasso reale."""
        now = datetime.now(UTC)
        rates = {("EUR", "USD"): Decimal("1.10")}
        if base == quote:
            r = Decimal("1")
        elif (base, quote) in rates:
            r = rates[(base, quote)]
        elif (quote, base) in rates:
            r = (Decimal("1") / rates[(quote, base)]).quantize(Decimal("0.00000001"))
        else:
            raise DataProviderError(self.name, f"cambio DEMO {base}/{quote} non disponibile")
        return Quote(symbol=f"{base}{quote}", exchange="FOREX", price=r, quote_time=now, source=SOURCE,
                     retrieved_at=now, currency=quote, is_demo=True, delay_minutes=0)
