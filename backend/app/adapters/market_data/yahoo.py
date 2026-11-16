"""Adapter gratuito basato su Yahoo Finance tramite la libreria open source `yfinance`.

ATTENZIONE — fonte NON ufficiale:
- Yahoo non offre un'API pubblica documentata; yfinance usa endpoint interni che possono cambiare
  o essere limitati senza preavviso.
- I termini di Yahoo consentono l'uso personale, non commerciale né la redistribuzione.
- Le quotazioni europee sono tipicamente ritardate di ~15-20 minuti.
Per questo ogni dato porta la fonte "Yahoo Finance (non ufficiale)" e le anomalie sospendono
le raccomandazioni. L'adapter è intercambiabile con provider professionali (vedi eodhd.py).

Convenzione ticker interna: SIMBOLO.EXCHANGE (es. AAPL.US, ENI.MI, SAP.DE, EXSA.DE).
"""
from __future__ import annotations

import math
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation

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

SOURCE = "Yahoo Finance (non ufficiale)"

# exchange interno -> suffisso Yahoo
EXCHANGE_SUFFIX = {
    "US": "", "MI": ".MI", "DE": ".DE", "XETRA": ".DE", "F": ".F", "PA": ".PA", "AS": ".AS", "BR": ".BR",
    "MC": ".MC", "LS": ".LS", "L": ".L", "LSE": ".L", "SW": ".SW", "VI": ".VI", "ST": ".ST", "HE": ".HE",
    "CO": ".CO", "OL": ".OL", "IR": ".IR",
}
# ritardo indicativo delle quotazioni (minuti)
EXCHANGE_DELAY = {"US": 0}
_DEFAULT_DELAY = 15


def yahoo_symbol(symbol: str, exchange: str) -> str:
    if exchange == "FOREX":
        return f"{symbol}=X"
    if exchange not in EXCHANGE_SUFFIX:
        raise DataProviderError("yahoo", f"exchange '{exchange}' non mappato (supportati: {', '.join(EXCHANGE_SUFFIX)})")
    return f"{symbol}{EXCHANGE_SUFFIX[exchange]}"


def _dec(v) -> Decimal | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    try:
        return Decimal(str(v))
    except InvalidOperation:
        return None


def _default_factory(sym: str):
    import yfinance as yf  # import pigro: la libreria serve solo se il provider è attivo
    return yf.Ticker(sym)


class YahooAdapter(MarketDataAdapter):
    name = "yahoo"
    is_demo = False

    def __init__(self, ticker_factory: Callable[[str], object] | None = None):
        self._factory = ticker_factory or _default_factory

    def _t(self, symbol: str, exchange: str):
        return self._factory(yahoo_symbol(symbol, exchange))

    @staticmethod
    def _wrap(fn, what: str):
        try:
            return fn()
        except DataProviderError:
            raise
        except Exception as e:  # noqa: BLE001  (yfinance solleva eccezioni eterogenee)
            raise DataProviderError("yahoo", f"{what} non disponibile: {type(e).__name__}: {str(e)[:200]}") from e

    def info(self) -> ProviderInfo:
        return ProviderInfo(
            name=SOURCE, is_demo=False, markets=["US", "EU"],
            capabilities=["instruments", "daily_bars", "quotes", "fundamentals", "earnings_calendar", "news", "fx"],
            license_note=("Fonte gratuita NON ufficiale: solo uso personale, nessuna garanzia di continuità o "
                          "accuratezza; quotazioni EU ritardate ~15-20 min."),
            configured=True,
        )

    def _info_dict(self, symbol: str, exchange: str) -> dict:
        d = self._wrap(lambda: self._t(symbol, exchange).info, "anagrafica")
        if not isinstance(d, dict) or not d:
            raise DataProviderError(self.name, f"nessuna anagrafica per {symbol}.{exchange}")
        return d

    def get_instrument(self, symbol: str, exchange: str) -> InstrumentInfo:
        d = self._info_dict(symbol, exchange)
        ccy = d.get("currency")
        qt = (d.get("quoteType") or "").upper()
        if not ccy:
            raise DataProviderError(self.name, f"valuta non disponibile per {symbol}.{exchange}")
        if qt not in ("EQUITY", "ETF"):
            raise DataProviderError(self.name, f"{symbol}.{exchange} è di tipo '{qt or 'sconosciuto'}': solo azioni ed ETF")
        isin = None
        try:
            raw = self._t(symbol, exchange).isin
            isin = raw if isinstance(raw, str) and len(raw) == 12 else None
        except Exception:  # noqa: BLE001
            isin = None
        # Yahoo quota alcune borse in centesimi (es. GBp a Londra): normalizziamo il codice valuta
        if ccy in ("GBp", "GBX"):
            ccy = "GBX"
        return InstrumentInfo(
            symbol=symbol, exchange=exchange, name=d.get("longName") or d.get("shortName"), currency=ccy.upper(),
            instrument_type="ETF" if qt == "ETF" else "STOCK", isin=isin, country=d.get("country"),
            sector=d.get("sector") or d.get("category"), industry=d.get("industry"),
            market_cap=_dec(d.get("marketCap")), source=SOURCE,
        )

    def get_daily_bars(self, symbol: str, exchange: str, start: date, end: date) -> BarSeries:
        def fetch():
            return self._t(symbol, exchange).history(
                start=start.isoformat(), end=(end + timedelta(days=1)).isoformat(), interval="1d",
                auto_adjust=False, actions=False, raise_errors=True)
        df = self._wrap(fetch, "storico")
        if df is None or len(df) == 0:
            raise DataProviderError(self.name, f"nessuna barra per {symbol}.{exchange}")
        bars = []
        for idx, row in df.iterrows():
            o, h, l, c = (_dec(row.get(k)) for k in ("Open", "High", "Low", "Close"))
            if None in (o, h, l, c):
                continue
            vol = _dec(row.get("Volume"))
            d = idx.date() if hasattr(idx, "date") else date.fromisoformat(str(idx)[:10])
            bars.append(Bar(d, o, h, l, c, _dec(row.get("Adj Close")), int(vol) if vol is not None else None))
        return BarSeries(symbol, exchange, bars, SOURCE, datetime.now(UTC))

    def get_quote(self, symbol: str, exchange: str) -> Quote:
        d = self._info_dict(symbol, exchange)
        price = _dec(d.get("regularMarketPrice") or d.get("currentPrice"))
        ts = d.get("regularMarketTime")
        if price is None or not ts:
            raise DataProviderError(self.name, f"quotazione non disponibile per {symbol}.{exchange}")
        qtime = datetime.fromtimestamp(int(ts), UTC) if isinstance(ts, (int, float)) else datetime.fromisoformat(str(ts))
        bid, ask = _dec(d.get("bid")), _dec(d.get("ask"))
        # Yahoo restituisce bid/ask = 0 a mercato chiuso: non sono prezzi validi
        bid = bid if bid and bid > 0 else None
        ask = ask if ask and ask > 0 else None
        vol = _dec(d.get("regularMarketVolume"))
        ccy = d.get("currency")
        return Quote(
            symbol=symbol, exchange=exchange, price=price, quote_time=qtime, source=SOURCE,
            retrieved_at=datetime.now(UTC), currency=ccy.upper() if ccy else None, bid=bid, ask=ask,
            previous_close=_dec(d.get("regularMarketPreviousClose") or d.get("previousClose")),
            volume=int(vol) if vol is not None else None,
            delay_minutes=EXCHANGE_DELAY.get(exchange, _DEFAULT_DELAY),
        )

    def get_fx_rate(self, base: str, quote: str) -> Quote:
        return self.get_quote(f"{base.upper()}{quote.upper()}", "FOREX")

    def get_fundamentals(self, symbol: str, exchange: str) -> dict:
        d = self._info_dict(symbol, exchange)
        keys = ("revenueGrowth", "earningsGrowth", "trailingEps", "forwardEps", "operatingMargins", "freeCashflow",
                "totalDebt", "totalCash", "debtToEquity", "returnOnEquity", "returnOnAssets", "trailingPE",
                "forwardPE", "enterpriseToEbitda", "priceToBook", "marketCap", "sector", "industry")
        return {"source": SOURCE, "retrieved_at": datetime.now(UTC).isoformat(),
                "data": {k: d.get(k) for k in keys if d.get(k) is not None}}

    def get_earnings_calendar(self, symbols: list[str], start: date, end: date) -> list[EventInfo]:
        out = []
        for full in symbols:
            sym, ex = full.rsplit(".", 1)
            cal = self._wrap(lambda: self._t(sym, ex).calendar, "calendario")  # noqa: B023
            for d in (cal or {}).get("Earnings Date", []) or []:
                dd = d if isinstance(d, date) else date.fromisoformat(str(d)[:10])
                if start <= dd <= end:
                    out.append(EventInfo(sym, ex, "EARNINGS", dd, {}, SOURCE))
        return out

    def get_news(self, symbol: str, limit: int = 20) -> list[NewsItem]:
        sym, ex = symbol.rsplit(".", 1)
        raw = self._wrap(lambda: self._t(sym, ex).news, "notizie") or []
        out = []
        for n in raw[:limit]:
            c = n.get("content", n)
            title, pub = c.get("title"), c.get("pubDate") or c.get("providerPublishTime")
            if not title or not pub:
                continue
            when = (datetime.fromtimestamp(pub, UTC) if isinstance(pub, (int, float))
                    else datetime.fromisoformat(str(pub).replace("Z", "+00:00")))
            url = (c.get("canonicalUrl") or {}).get("url") if isinstance(c.get("canonicalUrl"), dict) else c.get("link")
            provider = (c.get("provider") or {}).get("displayName") if isinstance(c.get("provider"), dict) else c.get("publisher")
            out.append(NewsItem(title=title, published_at=when, url=url, symbols=[symbol], sentiment=None,
                                source=f"{provider or 'n/d'} via {SOURCE}"))
        return out
