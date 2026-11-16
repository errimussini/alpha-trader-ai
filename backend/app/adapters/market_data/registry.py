from functools import lru_cache

from app.adapters.market_data.base import MarketDataAdapter
from app.adapters.market_data.csv_adapter import CSVAdapter
from app.adapters.market_data.demo import DemoAdapter
from app.adapters.market_data.eodhd import EODHDAdapter
from app.adapters.market_data.yahoo import YahooAdapter
from app.core.config import get_settings


@lru_cache
def get_market_data_adapter() -> MarketDataAdapter:
    s = get_settings()
    if s.market_data_provider == "eodhd":
        key = s.eodhd_api_key.get_secret_value() if s.eodhd_api_key else None
        return EODHDAdapter(key, s.eodhd_base_url, s.eodhd_timeout_seconds)
    if s.market_data_provider == "yahoo":
        return YahooAdapter()
    if s.market_data_provider == "csv":
        return CSVAdapter(s.csv_data_dir)
    return DemoAdapter()
