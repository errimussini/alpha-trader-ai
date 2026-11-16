// Client tipizzato verso il backend FastAPI. Il token JWT è tenuto in sessionStorage.
export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string, public details?: unknown) {
    super(message);
  }
}

const TOKEN_KEY = "alpha.token";

export function getToken(): string | null {
  try { return sessionStorage.getItem(TOKEN_KEY); } catch { return null; }
}
export function setToken(t: string | null) {
  try { t ? sessionStorage.setItem(TOKEN_KEY, t) : sessionStorage.removeItem(TOKEN_KEY); } catch { /* noop */ }
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  const token = getToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (init.body && typeof init.body === "string") headers.set("Content-Type", "application/json");
  const res = await fetch(`${API_URL}${path}`, { ...init, headers });
  if (!res.ok) {
    let code = "http_error", message = `Errore ${res.status}`, details: unknown;
    try {
      const b = await res.json();
      code = b.error?.code ?? code; message = b.error?.message ?? message; details = b.error?.details;
    } catch { /* corpo non JSON */ }
    if (res.status === 401) setToken(null);
    throw new ApiError(res.status, code, message, details);
  }
  return res.json() as Promise<T>;
}

export async function login(email: string, password: string) {
  const body = new URLSearchParams({ username: email, password });
  const res = await fetch(`${API_URL}/api/auth/login`, {
    method: "POST", body, headers: { "Content-Type": "application/x-www-form-urlencoded" },
  });
  if (!res.ok) throw new ApiError(res.status, "unauthorized", "Credenziali non valide");
  const data = (await res.json()) as { access_token: string; role: string };
  setToken(data.access_token);
  return data;
}

// ------------------------------------------------------------------ tipi
export type Page<T> = { items: T[]; total: number; page: number; page_size: number };

export type ProviderInfo = {
  name: string; is_demo: boolean; markets: string[]; capabilities: string[]; license_note: string; configured: boolean;
};

export type SystemStatus = {
  app: string; mode: "RESEARCH" | "PAPER"; available_modes: string[]; live_trading_enabled: boolean;
  kill_switch: { active: boolean; reason: string | null; since: string | null };
  market_data: ProviderInfo; database: string; redis: string; max_quote_age_minutes: number;
};

export type User = { id: number; email: string; role: "admin" | "analyst" | "viewer"; is_active: boolean };

export type Portfolio = {
  id: number; name: string; mode: string; base_currency: string; initial_capital: string; cash: string;
  invested_cost: string; market_value: string; equity: string; unrealized_pnl: string; realized_pnl: string;
  cumulative_return_pct: string; daily_return_pct: string | null; drawdown_pct: string;
  aggregate_risk_base: string; aggregate_risk_pct: string; risk_params: Record<string, string>;
  open_positions: number; warnings: string[]; valued_at: string;
};

export type Position = {
  id: number; instrument_id: number; ticker: string; name: string | null; sector: string | null; quantity: string;
  avg_price: string; currency: string; last_price: string | null; price_time: string | null;
  price_source: string | null; market_value_base: string; unrealized_pnl_base: string; stop_loss: string | null;
  take_profit_1: string | null; risk_to_stop_base: string | null; stale: boolean; is_demo: boolean; opened_at: string;
};

export type OverviewItem = {
  ticker: string; name: string | null; price: string | null; change_pct: string | null; quote_time: string | null;
  source: string | null; is_demo: boolean; stale: boolean; status: "OK" | "STALE" | "NO_DATA";
};
export type MarketOverview = {
  provider: ProviderInfo; items: OverviewItem[]; regime: { status: string; reason?: string }; generated_at: string;
};

export type Alert = {
  id: number; kind: string; severity: "INFO" | "WARNING" | "CRITICAL"; message: string; created_at: string;
};

export type Instrument = {
  id: number; symbol: string; exchange: string; isin: string | null; name: string | null; instrument_type: string;
  currency: string; sector: string | null; industry: string | null; market_cap: string | null; is_demo: boolean;
  data_source: string;
};

export type Bar = {
  bar_date: string; open: string; high: string; low: string; close: string; adj_close: string | null;
  volume: number | null; source: string; is_demo: boolean;
};

export type Quote = {
  id: number; price: string; bid: string | null; ask: string | null; previous_close: string | null;
  currency: string; quote_time: string; source: string; delay_minutes: number | null; is_demo: boolean;
};

export type SyncResult = {
  ticker: string; ok: boolean; instrument_id?: number; bars_added?: number; source?: string; is_demo?: boolean;
  error?: string;
};

export type RiskCheck = {
  assessment_id: number; approved: boolean; result: Record<string, string | string[] | boolean | null>;
  inputs: Record<string, unknown>;
};

export type Order = {
  id: number; client_order_id: string; instrument_id: number; side: string; order_type: string; quantity: string;
  filled_quantity: string; limit_price: string | null; status: string; reject_reason: string | null; mode: string;
  is_demo: boolean; created_at: string;
};
