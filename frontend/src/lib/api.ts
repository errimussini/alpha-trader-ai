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
  take_profit_2: string | null; trade_plan_id: number | null; tp1_done: boolean; trailing_active: boolean;
  trailing_distance: string | null; high_water: string | null; last_checked_at: string | null;
};

export type EquitySnapshot = {
  snap_date: string; equity: string; cash: string; market_value: string; realized_pnl: string; unrealized_pnl: string;
  drawdown_pct: string; aggregate_risk_pct: string; open_positions: number; stale: boolean; is_demo: boolean; taken_at: string;
};

export type MonitorReport = {
  portfolio_id: number; started_at: string; can_execute: boolean; blocked_reason: string | null; instruments: number;
  data_issues: string[]; orders_checked: number; orders_filled: number[]; orders_cancelled: number[];
  exits: { position_id: number; order_id: number; purpose: string; status: string; quantity: string }[];
  stops_raised: { position_id: number; from: string; to: string; why: string }[]; plans_invalidated: number[];
  alerts: number; reconciliation_ok: boolean; snapshot: { date: string; equity: string; stale: boolean } | null;
};

export type Reconciliation = {
  ok: boolean; checked_at: string; cash_from_movements: string; cash: string;
  issues: { check: string; message: string; expected?: string; actual?: string }[];
};

export type AlertSummary = { active: number; by_kind: Record<string, number>; by_severity: Record<string, number> };

export type OverviewItem = {
  ticker: string; name: string | null; price: string | null; change_pct: string | null; quote_time: string | null;
  source: string | null; is_demo: boolean; stale: boolean; status: "OK" | "STALE" | "NO_DATA";
};
export type MarketOverview = {
  provider: ProviderInfo; items: OverviewItem[]; regime: { status: string; reason?: string }; generated_at: string;
};

export type Alert = {
  id: number; kind: string; severity: "INFO" | "WARNING" | "CRITICAL"; message: string; created_at: string;
  instrument_id: number | null; portfolio_id: number | null; data: Record<string, unknown>; acknowledged_at: string | null;
};

export const ALERT_KINDS: Record<string, string> = {
  ENTRY_OPPORTUNITY: "Opportunità di ingresso", SETUP_INVALIDATED: "Setup invalidato", STOP_HIT: "Stop raggiunto",
  TARGET_HIT: "Target raggiunto", REVIEW_POSITION: "Rivaluta posizione", AGGREGATE_RISK: "Rischio aggregato",
  DATA_ANOMALY: "Anomalia dati", BROKER_ANOMALY: "Anomalia broker", SYSTEM: "Sistema",
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
  is_demo: boolean; created_at: string; stop_loss: string | null; take_profit_1: string | null;
  valid_until: string | null; purpose: string; trade_plan_id: number | null; last_checked_at: string | null;
  ticker: string | null;
};

export type ScanRun = {
  id: number; status: "QUEUED" | "RUNNING" | "DONE" | "FAILED"; trigger: string; requested_by: string;
  as_of: string | null; model_version: string | null; data_source: string | null;
  regime: { regime: string; score: number; notes: string[]; benchmarks: { ticker: string; trend: string; as_of: string; vol20: number; vol20_percentile_1y: number; source: string }[]; vix: { value: number; as_of: string; source: string } | null; rates: { us10y: number; change_3m: number; as_of: string } | null } | null;
  stats: { universe?: number; analysed?: number; eligible?: number; scored?: number; excluded?: number; errors?: number; top?: string[] };
  errors: { ticker?: string; stage: string; error: string }[]; progress: number; total: number;
  created_at: string; started_at: string | null; finished_at: string | null;
};

export type RankItem = {
  id: number; scan_run_id: number; instrument_id: number; ticker: string; name: string | null; isin: string | null;
  exchange: string; sector: string | null; instrument_type: string; currency: string; price: string | null;
  price_date: string | null; price_source: string; score: string | null;
  factor_scores: Record<string, number | null>; coverage: string | null; setup: string | null;
  strategy_style: string | null; horizon: string | null; risk_level: string | null; eligible: boolean;
  decision: string; market_regime: string; is_demo: boolean; analysis_time: string;
  potential_return_gross: null; potential_return_net: null; reward_risk: null; calibrated_confidence: null;
  rationale: { for: string[]; against: string[]; could_be_wrong_if: string[] };
  exclusions: string[]; flags: string[]; details?: { features: Record<string, number | null> };
};

export type Ranking = {
  scan: ScanRun | null; items: RankItem[]; total: number; page: number; page_size: number;
  weights: Record<string, number>; model_version: string; facets?: { exchanges: string[]; sectors: string[] };
};

export const FACTOR_LABELS: Record<string, string> = {
  trend_momentum: "Trend e momentum", setup: "Qualità del setup", fundamentals: "Fondamentali",
  macro_sector: "Regime macro e settore", liquidity_vol: "Liquidità e volatilità", catalysts: "Catalizzatori",
  sentiment: "Sentiment",
};
export const REGIME_LABELS: Record<string, string> = {
  BULL: "Rialzista", BEAR: "Ribassista", SIDEWAYS: "Laterale", HIGH_VOLATILITY: "Alta volatilità", UNKNOWN: "Non determinabile",
};

export type TradePlan = {
  id: number; instrument_id: number; ticker: string | null; name: string | null; isin: string | null; sector: string | null;
  model_version: string; direction: string; strategy_style: string; setup: string | null; score: string | null;
  risk_level: string | null; market_regime: string | null; price_reference: string; price_source: string; price_time: string;
  entry_price: string; entry_low: string | null; entry_high: string | null; stop_loss: string; take_profit_1: string;
  take_profit_2: string | null; tp1_close_fraction: string | null; trailing_stop: Record<string, string> | null;
  quantity: string; notional_base: string; est_loss_at_stop_base: string; est_gain_tp1_base: string | null;
  est_gain_tp2_base: string | null; reward_risk_net: string | null; calibrated_probability: string | null;
  expected_value_base: string | null; gross_return_tp1_pct: string | null; net_return_tp1_pct: string | null;
  horizon_days: number; rationale_for: string[]; rationale_against: string[]; invalidation_conditions: string[];
  entry_conditions: string[]; avoid_conditions: string[]; early_exit_conditions: string[];
  stop_rationale: Record<string, unknown>; scenarios: { name: string; exit: number; pnl_base: string | null }[];
  correlations: { values?: Record<string, number | null>; max?: number | null; notes?: string[] };
  status: "READY" | "WAIT" | "REJECTED" | "EXPIRED"; status_reason: string | null; valid_until: string;
  currency: string | null; fx_rate: string | null; is_demo: boolean; created_at: string;
  committee: Record<string, unknown>;
};

// ---------------------------------------------------------------- Fase 4: comitato e assistente
export type Stance = "FAVOREVOLE" | "NEUTRALE" | "CONTRARIO";
export type Validation = { unverified_numbers: string[]; unknown_refs: string[] };
export type AgentOpinion = {
  agent: string; title: string; mode: "ai" | "deterministic"; run_id: number; stance: Stance; summary: string;
  points_for: string[]; points_against: string[]; risks: string[]; data_refs: string[]; validation: Validation; error: string | null;
};
export type CommitteeDecision = {
  agent: string; title: string; mode: "ai" | "deterministic"; run_id: number;
  decision: "BUY" | "WAIT" | "HOLD" | "REDUCE" | "EXIT" | "REJECT"; summary: string; reasons_for: string[];
  reasons_against: string[]; could_be_wrong_if: string[]; conditions_to_act: string[]; dissent: string[];
  data_refs: string[]; validation: Validation; error: string | null;
};
export type CommitteeResult = {
  status: "DONE"; version: string; prompt_version: string; mode: "ai" | "mixed" | "deterministic"; model: string | null;
  completed_at: string; allowed_decisions: string[]; constraints: string[]; inputs_hash: string;
  agents: AgentOpinion[]; decision: CommitteeDecision; disclaimer: string;
};
export type CommitteeState = CommitteeResult | {
  status: "QUEUED" | "RUNNING" | "FAILED"; job_id?: number; model?: string; error?: string;
  previous?: CommitteeResult | null; job?: { id: number; status: string; error: string | null } | null;
};
export type AiStatus = { enabled: boolean; model: string | null; configured_model: string; effort: string; mode: "ai" | "deterministic" };
export type ChatSource = { tool: string; input: Record<string, unknown>; output: unknown; error?: string | null };
export type ChatAnswer = { answer: string; mode: "ai" | "deterministic"; model: string | null; run_id: number; sources: ChatSource[] };

export const DECISION_LABELS: Record<string, string> = {
  BUY: "Acquistare (simulato)", WAIT: "Attendere", HOLD: "Mantenere", REDUCE: "Ridurre", EXIT: "Uscire", REJECT: "Respingere",
};
export const TOOL_LABELS: Record<string, string> = {
  get_market_regime: "Regime di mercato", list_opportunities: "Opportunità", get_trade_plan: "Piano operativo",
  explain_selection: "Motivazioni del ranking", check_budget: "Motore di rischio", get_positions: "Posizioni aperte",
  get_performance: "Performance del portafoglio",
};

// ---------------------------------------------------------------- Fase 5: backtest
export type Metrics = Record<string, number | string | null | number[] | Record<string, number> | undefined> & {
  net_return_pct?: number | null; cagr_pct?: number | null; max_drawdown_pct?: number | null; sharpe?: number | null;
  sortino?: number | null; profit_factor?: number | null; win_rate_pct?: number | null; trades?: number;
  expectancy_base?: number | null; expectancy_r?: number | null; turnover_annual?: number | null; avg_exposure_pct?: number | null;
  volatility_pct?: number | null; days?: number; start?: string; end?: string; win_rate_ci95_pct?: number[];
};
export type CalibBlock = { trades: number; win_rate?: number; win_rate_ci95?: number[]; ev_r?: number; ev_r_ci95?: number[];
  avg_win_r?: number; avg_loss_r?: number; validated: boolean };
export type BTTrade = { ticker: string; setup: string; regime: string; score: number; entry_date: string; entry_price: number;
  stop: number; tp1: number; qty: number; exit_date: string; exit_reason: string; pnl_base: number; r_multiple: number | null;
  holding_days: number };
export type BacktestResults = {
  engine_version: string; period: [string, string]; split_bounds: Record<string, [string, string]>;
  segments: Record<"train" | "validation" | "test" | "full", Metrics>;
  walk_forward: { train_days: number; test_days: number; grid_min_score: number[]; metrics: Metrics; equity: [string, number][];
    windows: { train: [string, string]; test: [string, string]; chosen_min_score: number; result: Metrics }[] };
  calibration: { overall: CalibBlock; by_setup: Record<string, CalibBlock>; min_trades: number; is_demo: boolean; note: string };
  bootstrap: Record<"full" | "walk_forward", { available: boolean; note?: string; trades?: number;
    expectancy_base_p2_5_p50_p97_5?: number[]; total_pnl_base_p2_5_p50_p97_5?: number[]; profit_factor_p2_5_p50_p97_5?: number[];
    prob_expectancy_negative_pct?: number }>;
  stress: Record<string, Metrics>; by_regime_trades: Record<string, Metrics>; by_regime_days: Record<string, { days: number; annualized_return_pct: number }>;
  by_setup: Record<string, Metrics>; benchmarks: Record<string, Metrics>; benchmark_curve: { ticker?: string; series?: [string, number][] };
  equity: [string, number][]; drawdown: [string, number][]; trades: BTTrade[]; trades_total: number; universe_size: number;
  is_demo: boolean; bias_controls: Record<string, string>; notes: string[]; missing?: string[]; disclaimer: string;
};
export type BacktestRun = {
  id: number; name: string; status: "QUEUED" | "RUNNING" | "DONE" | "FAILED"; engine_version: string;
  params: { params: Record<string, unknown>; tickers?: string[] | null; train_days?: number; test_days?: number };
  universe_size: number; period_start: string | null; period_end: string | null; error: string | null; is_demo: boolean;
  summary: Metrics & { oos_walk_forward?: Metrics; test?: Metrics; calibration_validated?: boolean };
  requested_by: string; created_at: string; finished_at: string | null; results?: BacktestResults; universe?: string[];
};
