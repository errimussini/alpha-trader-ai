"""Configurazione centrale. Tutti i valori sono letti da variabili d'ambiente / file .env.

I parametri di rischio sono valori INIZIALI da sottoporre a test, non parametri ottimizzati.
"""
from decimal import Decimal
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "ALPHA TRADER AI"
    environment: Literal["development", "test", "production"] = "development"

    # --- Database / cache ---
    database_url: str = "postgresql+psycopg://alpha:alpha@localhost:5432/alpha_trader"
    redis_url: str = "redis://localhost:6379/0"

    # --- Sicurezza ---
    jwt_secret: SecretStr = SecretStr("change-me-in-env")
    jwt_algorithm: str = "HS256"
    access_token_minutes: int = 60
    cors_origins: list[str] = ["http://localhost:3000"]
    bootstrap_admin_email: str | None = None
    bootstrap_admin_password: SecretStr | None = None

    # --- Modalità operativa ---
    # Solo RESEARCH e PAPER sono supportate nella prima release. LIVE non è attivabile.
    default_mode: Literal["RESEARCH", "PAPER"] = "RESEARCH"
    live_trading_enabled: bool = False  # bloccato a livello di codice, vedi services/safety.py

    # --- Dati di mercato ---
    # yahoo = gratuito non ufficiale (default); eodhd = professionale a pagamento; csv = file utente; demo = sintetico
    market_data_provider: Literal["yahoo", "eodhd", "csv", "demo"] = "yahoo"
    eodhd_api_key: SecretStr | None = None
    eodhd_base_url: str = "https://eodhd.com/api"
    eodhd_timeout_seconds: float = 20.0
    csv_data_dir: str = "data/csv"
    # Una quotazione più vecchia di questa soglia blocca ordini e raccomandazioni.
    max_quote_age_minutes: int = 60
    allow_demo_paper_trading: bool = True  # ordini su dati DEMO sono marcati is_demo=True
    benchmark_symbols: list[str] = ["SPY.US", "QQQ.US", "VGK.US", "EXSA.DE", "CSSPX.MI"]

    # --- Scanner (Fase 2) ---
    regime_benchmarks: list[str] = ["SPY.US", "EXSA.DE"]
    regime_vix: str | None = "VIX.INDEX"
    regime_rates: str | None = "TNX.INDEX"
    rs_benchmark_by_region: dict[str, str] = {"US": "SPY.US", "EU": "EXSA.DE"}
    scan_schedule_utc: str | None = "22:30"  # giorni feriali, dopo la chiusura USA; None = disattivato
    scan_history_years: int = 2
    scan_request_pause_seconds: float = 0.25
    scan_max_retries: int = 2
    scan_min_price: float = 5.0
    scan_min_market_cap: float = 1e9
    scan_min_traded_value: float = 5e6
    scan_max_spread_pct: float = 0.5
    plan_top_n: int = 10  # piani operativi generati automaticamente dopo ogni scansione

    # --- AI (Fase 4): Anthropic Claude API ---
    # La chiave resta lato server (.env). Senza chiave il comitato e l'assistente funzionano in modalità
    # deterministica: stesse regole e stessi dati, senza sintesi qualitativa del modello.
    anthropic_api_key: SecretStr | None = None
    ai_model: str = "claude-opus-5-5"
    ai_effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    ai_max_tokens: int = 16000
    ai_timeout_seconds: float = 300.0
    ai_refusal_fallback: bool = True  # fallback lato server in caso di rifiuto per sicurezza
    ai_auto_committee_top_n: int = 0  # comitato AI automatico sui primi N piani READY/WAIT dopo la scansione
    ai_assistant_max_steps: int = 8

    # --- Monitoraggio paper trading (Fase 6) ---
    monitor_interval_seconds: int = 300  # ciclo del worker su ordini aperti, posizioni e piani attivi
    paper_auto_exits: bool = True  # stop/TP autorizzati all'ingresso eseguiti in simulazione; False = solo alert
    paper_limit_order_days: int = 5  # validità di un ordine LIMIT senza piano
    paper_max_participation_pct: Decimal = Decimal("0")  # >0: eseguito max questa % del volume (fill parziali)
    alert_webhook_url: SecretStr | None = None  # notifiche WARNING/CRITICAL in POST JSON (opzionale)
    alert_dedup_hours: int = 24

    # --- Profilo operativo / rischio (parametri iniziali, da validare) ---
    base_currency: str = "EUR"
    initial_paper_capital: Decimal = Decimal("10000")
    risk_per_trade_pct: Decimal = Decimal("0.5")
    max_aggregate_risk_pct: Decimal = Decimal("3.0")
    max_position_exposure_pct: Decimal = Decimal("15.0")
    max_sector_exposure_pct: Decimal = Decimal("30.0")
    min_cash_reserve_pct: Decimal = Decimal("5.0")
    max_daily_loss_pct: Decimal = Decimal("2.0")
    max_total_drawdown_pct: Decimal = Decimal("10.0")
    commission_per_order: Decimal = Decimal("2.00")
    commission_pct: Decimal = Decimal("0.0")
    slippage_bps: Decimal = Decimal("5")
    leverage_enabled: bool = Field(default=False)
    short_selling_enabled: bool = Field(default=False)


@lru_cache
def get_settings() -> Settings:
    return Settings()
