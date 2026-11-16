"""Adapter per dati storici forniti dall'utente in CSV (es. export dal proprio broker/provider).

File attesi: {csv_data_dir}/{SYMBOL}.{EXCHANGE}.csv con colonne
    date,open,high,low,close,adjusted_close,volume
e, opzionale, {csv_data_dir}/instruments.csv con colonne
    symbol,exchange,name,currency,type,isin,sector,industry

La "quotazione" è l'ultima chiusura disponibile, con timestamp pari alla data della barra:
è quindi tipicamente troppo vecchia per gli ordini e serve solo in Research Mode.
"""
from __future__ import annotations

import csv
from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path

from app.adapters.market_data.base import (
    Bar,
    BarSeries,
    DataProviderError,
    InstrumentInfo,
    MarketDataAdapter,
    ProviderInfo,
    Quote,
)


class CSVAdapter(MarketDataAdapter):
    name = "csv"
    is_demo = False

    def __init__(self, data_dir: str):
        self.dir = Path(data_dir)

    def info(self) -> ProviderInfo:
        return ProviderInfo(
            name="CSV", is_demo=False, markets=["user-provided"], capabilities=["instruments", "daily_bars"],
            license_note="Dati importati dall'utente: fonte e licenza sono responsabilità di chi li fornisce.",
            configured=self.dir.is_dir(),
        )

    def _file(self, symbol: str, exchange: str) -> Path:
        p = self.dir / f"{symbol}.{exchange}.csv"
        if not p.is_file():
            raise DataProviderError(self.name, f"file {p.name} non trovato in {self.dir}")
        return p

    def get_instrument(self, symbol: str, exchange: str) -> InstrumentInfo:
        meta = self.dir / "instruments.csv"
        if meta.is_file():
            with meta.open(newline="") as fh:
                for r in csv.DictReader(fh):
                    if r["symbol"].upper() == symbol and r["exchange"].upper() == exchange:
                        return InstrumentInfo(
                            symbol=symbol, exchange=exchange, name=r.get("name") or None,
                            currency=r["currency"].upper(), instrument_type=(r.get("type") or "STOCK").upper(),
                            isin=r.get("isin") or None, sector=r.get("sector") or None,
                            industry=r.get("industry") or None, source=f"CSV:{meta.name}",
                        )
        raise DataProviderError(self.name, f"{symbol}.{exchange} assente in instruments.csv (valuta obbligatoria)")

    def get_daily_bars(self, symbol: str, exchange: str, start: date, end: date) -> BarSeries:
        p = self._file(symbol, exchange)
        bars = []
        with p.open(newline="") as fh:
            for r in csv.DictReader(fh):
                d = date.fromisoformat(r["date"])
                if not (start <= d <= end):
                    continue
                adj = r.get("adjusted_close") or r.get("adj_close")
                vol = r.get("volume")
                bars.append(Bar(d, Decimal(r["open"]), Decimal(r["high"]), Decimal(r["low"]), Decimal(r["close"]),
                                Decimal(adj) if adj else None, int(float(vol)) if vol else None))
        bars.sort(key=lambda b: b.bar_date)
        return BarSeries(symbol, exchange, bars, f"CSV:{p.name}", datetime.now(UTC))

    def get_quote(self, symbol: str, exchange: str) -> Quote:
        series = self.get_daily_bars(symbol, exchange, date.min, date.max)
        if not series.bars:
            raise DataProviderError(self.name, f"nessuna barra per {symbol}.{exchange}")
        last = series.bars[-1]
        return Quote(symbol=symbol, exchange=exchange, price=last.close,
                     quote_time=datetime.combine(last.bar_date, time(0), UTC), source=series.source,
                     retrieved_at=datetime.now(UTC), volume=last.volume)
