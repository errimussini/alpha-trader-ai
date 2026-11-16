"""Adapter per EOD Historical Data (https://eodhd.com).

Endpoint usati (documentazione pubblica EODHD):
- GET /eod/{SYMBOL}.{EXCHANGE}?from=&to=&period=d&fmt=json          prezzi giornalieri (con adjusted_close)
- GET /real-time/{SYMBOL}.{EXCHANGE}?fmt=json                       quotazione live/ritardata
- GET /fundamentals/{SYMBOL}.{EXCHANGE}                             fondamentali (piano Fundamentals)
- GET /exchange-symbol-list/{EXCHANGE}?fmt=json                     universo di un mercato
- GET /calendar/earnings?symbols=&from=&to=&fmt=json                calendario utili
- GET /news?s=&limit=&fmt=json                                      notizie con sentiment

La chiave API è letta solo lato server (EODHD_API_KEY) e non viene mai loggata.
Copertura effettiva, ritardo delle quotazioni e diritti d'uso dipendono dal piano sottoscritto.
"""
from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation

import httpx

from app.adapters.market_data.base import (
    Bar,
    BarSeries,
    DataProviderError,
    EventInfo,
    InstrumentInfo,
    MarketDataAdapter,
    NewsItem,
    ProviderInfo,
    Quote,
)

SOURCE = "EODHD"

# Ritardo tipico delle quotazioni /real-time per le borse non USA (minuti). Per gli USA EODHD
# dichiara dati in tempo reale/ritardo minimo a seconda del piano; registriamo il valore come stima.
_DELAY_HINT = {"US": 0, "FOREX": 0}
_DEFAULT_DELAY = 15


def _dec(v) -> Decimal | None:
    if v is None or v in ("NA", "", "N/A"):
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None


def _int(v) -> int | None:
    d = _dec(v)
    return int(d) if d is not None else None


def _type(raw: str | None) -> str:
    return "ETF" if raw and raw.upper() == "ETF" else "STOCK"


class EODHDAdapter(MarketDataAdapter):
    name = "eodhd"
    is_demo = False

    def __init__(self, api_key: str | None, base_url: str = "https://eodhd.com/api",
                 timeout: float = 20.0, transport: httpx.BaseTransport | None = None):
        self._key = api_key
        self._client = httpx.Client(base_url=base_url, timeout=timeout, transport=transport)

    # ------------------------------------------------------------ http
    def _get(self, path: str, **params):
        if not self._key:
            raise DataProviderError(self.name, "EODHD_API_KEY non configurata")
        params = {k: v for k, v in params.items() if v is not None}
        params["api_token"] = self._key
        params.setdefault("fmt", "json")
        try:
            r = self._client.get(path, params=params)
        except httpx.HTTPError as e:
            raise DataProviderError(self.name, f"connessione fallita: {type(e).__name__}") from e
        if r.status_code in (401, 403):
            raise DataProviderError(self.name, "chiave non valida o piano non abilitato per questo dato", status=r.status_code)
        if r.status_code == 402:
            raise DataProviderError(self.name, "limite del piano superato (402)", status=402)
        if r.status_code == 429:
            raise DataProviderError(self.name, "rate limit raggiunto (429)", status=429)
        if r.status_code == 404:
            raise DataProviderError(self.name, "strumento non trovato", status=404)
        if r.status_code >= 400:
            raise DataProviderError(self.name, f"errore HTTP {r.status_code}", status=r.status_code)
        try:
            return r.json()
        except ValueError as e:
            raise DataProviderError(self.name, "risposta non JSON") from e

    # ------------------------------------------------------------ api
    def info(self) -> ProviderInfo:
        return ProviderInfo(
            name=SOURCE, is_demo=False, markets=["US", "EU"],
            capabilities=["instruments", "daily_bars", "quotes", "fundamentals", "earnings_calendar", "news"],
            license_note="Uso soggetto ai termini del piano EODHD sottoscritto; ritardo quotazioni variabile per borsa.",
            configured=bool(self._key),
        )

    def get_daily_bars(self, symbol: str, exchange: str, start: date, end: date) -> BarSeries:
        data = self._get(f"/eod/{symbol}.{exchange}", **{"from": start.isoformat(), "to": end.isoformat(), "period": "d"})
        if not isinstance(data, list):
            raise DataProviderError(self.name, "formato inatteso per /eod")
        bars: list[Bar] = []
        for row in data:
            o, h, l, c = (_dec(row.get(k)) for k in ("open", "high", "low", "close"))
            if None in (o, h, l, c):
                continue  # barra incompleta: scartata, mai ricostruita
            bars.append(Bar(
                bar_date=date.fromisoformat(row["date"]), open=o, high=h, low=l, close=c,
                adj_close=_dec(row.get("adjusted_close")), volume=_int(row.get("volume")),
            ))
        return BarSeries(symbol, exchange, bars, SOURCE, datetime.now(UTC))

    def get_quote(self, symbol: str, exchange: str) -> Quote:
        d = self._get(f"/real-time/{symbol}.{exchange}")
        if not isinstance(d, dict):
            raise DataProviderError(self.name, "formato inatteso per /real-time")
        price = _dec(d.get("close"))
        ts = d.get("timestamp")
        if price is None or ts in (None, "NA"):
            raise DataProviderError(self.name, f"quotazione non disponibile per {symbol}.{exchange}")
        return Quote(
            symbol=symbol, exchange=exchange, price=price,
            quote_time=datetime.fromtimestamp(int(ts), UTC), source=SOURCE, retrieved_at=datetime.now(UTC),
            previous_close=_dec(d.get("previousClose")), volume=_int(d.get("volume")),
            delay_minutes=_DELAY_HINT.get(exchange, _DEFAULT_DELAY),
        )

    def get_fundamentals(self, symbol: str, exchange: str) -> dict:
        d = self._get(f"/fundamentals/{symbol}.{exchange}")
        if not isinstance(d, dict):
            raise DataProviderError(self.name, "formato inatteso per /fundamentals")
        return d

    def get_instrument(self, symbol: str, exchange: str) -> InstrumentInfo:
        f = self.get_fundamentals(symbol, exchange)
        g = f.get("General") or {}
        h = f.get("Highlights") or {}
        if not g.get("CurrencyCode"):
            raise DataProviderError(self.name, f"anagrafica incompleta per {symbol}.{exchange}")
        return InstrumentInfo(
            symbol=symbol, exchange=exchange, name=g.get("Name"), currency=g["CurrencyCode"],
            instrument_type=_type(g.get("Type")), isin=g.get("ISIN"), country=g.get("CountryName"),
            sector=g.get("Sector"), industry=g.get("Industry"),
            market_cap=_dec(h.get("MarketCapitalization")), source=SOURCE,
        )

    def list_instruments(self, exchange: str) -> list[InstrumentInfo]:
        data = self._get(f"/exchange-symbol-list/{exchange}")
        out = []
        for r in data or []:
            t = (r.get("Type") or "").lower()
            if t not in ("common stock", "etf"):
                continue
            if not r.get("Currency"):
                continue
            out.append(InstrumentInfo(
                symbol=r["Code"], exchange=exchange, name=r.get("Name"), currency=r["Currency"],
                instrument_type="ETF" if t == "etf" else "STOCK", isin=r.get("Isin"),
                country=r.get("Country"), source=SOURCE,
            ))
        return out

    def get_earnings_calendar(self, symbols: list[str], start: date, end: date) -> list[EventInfo]:
        d = self._get("/calendar/earnings", symbols=",".join(symbols), **{"from": start.isoformat(), "to": end.isoformat()})
        out = []
        for e in (d or {}).get("earnings", []):
            code = e.get("code", "")
            if "." not in code or not e.get("report_date"):
                continue
            sym, ex = code.rsplit(".", 1)
            out.append(EventInfo(
                symbol=sym, exchange=ex, event_type="EARNINGS", event_date=date.fromisoformat(e["report_date"]),
                details={k: e.get(k) for k in ("date", "before_after_market", "currency", "actual", "estimate")},
                source=SOURCE,
            ))
        return out

    def get_news(self, symbol: str, limit: int = 20) -> list[NewsItem]:
        d = self._get("/news", s=symbol, limit=limit)
        out = []
        for n in d or []:
            if not n.get("title") or not n.get("date"):
                continue
            s = n.get("sentiment") or {}
            out.append(NewsItem(
                title=n["title"], published_at=datetime.fromisoformat(n["date"].replace("Z", "+00:00")),
                url=n.get("link"), symbols=n.get("symbols") or [], sentiment=_dec(s.get("polarity")),
                source=SOURCE,
            ))
        return out

    def get_fx_rate(self, base: str, quote: str) -> Quote:
        return self.get_quote(f"{base.upper()}{quote.upper()}", "FOREX")
