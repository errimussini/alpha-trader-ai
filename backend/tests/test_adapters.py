import json
from datetime import date
from decimal import Decimal as D

import httpx
import pandas as pd
import pytest

from app.adapters.broker.base import MarketSnapshot, OrderRequest
from app.adapters.broker.paper import PaperBroker
from app.adapters.market_data.base import DataProviderError, split_ticker
from app.adapters.market_data.csv_adapter import CSVAdapter
from app.adapters.market_data.demo import DemoAdapter
from app.adapters.market_data.eodhd import EODHDAdapter
from app.adapters.market_data.yahoo import YahooAdapter, yahoo_symbol


# ---------------------------------------------------------------- helpers
def test_split_ticker():
    assert split_ticker("eni.mi") == ("ENI", "MI")
    with pytest.raises(ValueError):
        split_ticker("AAPL")


# ---------------------------------------------------------------- demo
def test_demo_is_labelled_and_deterministic():
    a = DemoAdapter()
    s1 = a.get_daily_bars("DEMO1", "DEMO", date(2024, 1, 1), date(2024, 3, 1))
    s2 = a.get_daily_bars("DEMO1", "DEMO", date(2024, 1, 1), date(2024, 3, 1))
    assert s1.bars == s2.bars and s1.is_demo and "DEMO" in s1.source
    assert a.get_quote("DEMO1", "DEMO").is_demo
    with pytest.raises(DataProviderError):
        a.get_quote("AAPL", "US")  # nessun ticker reale nell'universo demo


# ---------------------------------------------------------------- yahoo (mock della libreria)
class FakeTicker:
    def __init__(self, sym, info=None, df=None):
        self.sym, self.info, self._df, self.isin = sym, info or {}, df, "US0378331005"

    def history(self, **_):
        return self._df


def _yahoo(info=None, df=None):
    calls = []

    def factory(sym):
        calls.append(sym)
        return FakeTicker(sym, info, df)
    return YahooAdapter(factory), calls


def test_yahoo_symbol_mapping():
    assert yahoo_symbol("AAPL", "US") == "AAPL"
    assert yahoo_symbol("ENI", "MI") == "ENI.MI"
    assert yahoo_symbol("EURUSD", "FOREX") == "EURUSD=X"
    with pytest.raises(DataProviderError):
        yahoo_symbol("X", "ZZ")


def test_yahoo_quote_parsing_and_zero_bid():
    a, calls = _yahoo(info={"regularMarketPrice": 190.5, "regularMarketTime": 1763000000, "bid": 0, "ask": 0,
                            "currency": "USD", "regularMarketPreviousClose": 189.0, "regularMarketVolume": 1000})
    q = a.get_quote("AAPL", "US")
    assert q.price == D("190.5") and q.bid is None and q.ask is None
    assert q.source.startswith("Yahoo") and not q.is_demo
    assert calls == ["AAPL"]


def test_yahoo_missing_price_never_invented():
    a, _ = _yahoo(info={"currency": "USD"})
    with pytest.raises(DataProviderError):
        a.get_quote("AAPL", "US")


def test_yahoo_bars_skip_nan():
    idx = pd.to_datetime(["2026-11-12", "2026-11-13"]).tz_localize("America/New_York")
    df = pd.DataFrame({"Open": [1.0, float("nan")], "High": [2.0, 2.0], "Low": [0.5, 0.5], "Close": [1.5, 1.5],
                       "Adj Close": [1.4, 1.4], "Volume": [100, 100]}, index=idx)
    a, _ = _yahoo(df=df)
    s = a.get_daily_bars("AAPL", "US", date(2026, 11, 1), date(2026, 11, 13))
    assert len(s.bars) == 1 and s.bars[0].bar_date == date(2026, 11, 12)


def test_yahoo_rejects_non_equity():
    a, _ = _yahoo(info={"currency": "USD", "quoteType": "CRYPTOCURRENCY"})
    with pytest.raises(DataProviderError):
        a.get_instrument("BTC", "US")


# ---------------------------------------------------------------- eodhd (mock HTTP)
def test_eodhd_parsing_and_key_handling():
    seen = []

    def handler(req: httpx.Request):
        seen.append(req.url)
        if req.url.path.endswith("/real-time/AAPL.US"):
            return httpx.Response(200, json={"code": "AAPL.US", "timestamp": 1763000000, "close": 190.5,
                                             "previousClose": 189, "volume": 10})
        if req.url.path.endswith("/eod/AAPL.US"):
            return httpx.Response(200, json=[{"date": "2026-11-12", "open": 1, "high": 2, "low": 0.5, "close": 1.5,
                                              "adjusted_close": 1.4, "volume": 100}])
        return httpx.Response(401, json={})

    a = EODHDAdapter("secret-key", transport=httpx.MockTransport(handler))
    assert a.get_quote("AAPL", "US").price == D("190.5")
    assert len(a.get_daily_bars("AAPL", "US", date(2026, 11, 1), date(2026, 11, 13)).bars) == 1
    with pytest.raises(DataProviderError) as e:
        a.get_quote("MSFT", "US")
    assert "secret-key" not in str(e.value)
    with pytest.raises(DataProviderError):
        EODHDAdapter(None).get_quote("AAPL", "US")


# ---------------------------------------------------------------- csv
def test_csv_adapter(tmp_path):
    (tmp_path / "instruments.csv").write_text("symbol,exchange,name,currency,type\nTEST,MI,Test Spa,EUR,STOCK\n")
    (tmp_path / "TEST.MI.csv").write_text("date,open,high,low,close,adjusted_close,volume\n"
                                          "2024-01-03,10,11,9,10.5,10.5,1000\n2024-01-02,9,10,8,9.5,9.5,900\n")
    a = CSVAdapter(str(tmp_path))
    s = a.get_daily_bars("TEST", "MI", date(2024, 1, 1), date(2024, 12, 31))
    assert [b.bar_date for b in s.bars] == [date(2024, 1, 2), date(2024, 1, 3)]
    assert a.get_instrument("TEST", "MI").currency == "EUR"
    assert a.get_quote("TEST", "MI").price == D("10.5")


# ---------------------------------------------------------------- paper broker
def _snap(price="100", bid="99.9", ask="100.1"):
    from datetime import UTC, datetime
    return MarketSnapshot(D(price), D(bid), D(ask), datetime.now(UTC), "test")


def test_paper_broker_market_and_limit():
    b = PaperBroker(D("2"), D("0"), D("10"))
    r = b.submit_order(OrderRequest("id-00001", "X", "US", "BUY", "MARKET", D("10")), _snap())
    assert r.status == "FILLED" and r.fills[0].price == D("100.2001") and r.fills[0].fee == D("2.0000")
    r = b.submit_order(OrderRequest("id-00002", "X", "US", "BUY", "LIMIT", D("10"), D("99")), _snap())
    assert r.status == "OPEN" and not r.fills
    r = b.submit_order(OrderRequest("id-00003", "X", "US", "BUY", "LIMIT", D("10")), _snap())
    assert r.status == "REJECTED"
