"use client";
import { useMemo, useState } from "react";
import { api, type BacktestRun, type Metrics, type Page } from "@/lib/api";
import { useApi } from "@/lib/hooks";
import { cn, num, when } from "@/lib/format";
import { Badge, Button, Card, CardHeader, DemoBadge, Empty, ErrorNote, Input, Label } from "@/components/ui/primitives";
import { Lines } from "@/components/lines";
import { useSession } from "@/components/shell";

const SETUPS = [["BREAKOUT", "Breakout"], ["PULLBACK", "Pullback"], ["TREND_FOLLOWING", "Trend following"]] as const;
const STATUS_TONE = { QUEUED: "neutral", RUNNING: "accent", DONE: "up", FAILED: "down" } as const;
const KEYS: [keyof Metrics, string, number?][] = [
  ["net_return_pct", "Rendimento netto %"], ["cagr_pct", "CAGR %"], ["max_drawdown_pct", "Max drawdown %"],
  ["sharpe", "Sharpe"], ["sortino", "Sortino"], ["volatility_pct", "Volatilità %"], ["profit_factor", "Profit factor"],
  ["win_rate_pct", "Win rate %"], ["expectancy_base", "Expectancy €"], ["expectancy_r", "Expectancy R", 3],
  ["trades", "Operazioni", 0], ["turnover_annual", "Turnover annuo"], ["avg_exposure_pct", "Esposizione media %"],
];
const SEGMENTS = [["train", "Training"], ["validation", "Validazione"], ["test", "Test"], ["full", "Intero periodo"]] as const;
const n = (v: unknown, d = 2) => (typeof v === "number" ? num(v, d) : "—");
const signed = (v: unknown) => cn("num", typeof v === "number" ? (v >= 0 ? "text-up" : "text-down") : "text-muted");

function MetricsTable({ cols }: { cols: [string, Metrics | undefined][] }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs">
        <thead><tr className="border-b border-line text-left text-muted">
          <th className="px-3 py-1.5 font-medium">Metrica</th>
          {cols.map(([h]) => <th key={h} className="px-3 py-1.5 text-right font-medium">{h}</th>)}
        </tr></thead>
        <tbody className="divide-y divide-line">
          {KEYS.map(([k, label, d]) => (
            <tr key={String(k)}><td className="px-3 py-1 text-muted">{label}</td>
              {cols.map(([h, m]) => <td key={h} className={cn("px-3 py-1 text-right", k === "net_return_pct" || k === "cagr_pct" ? signed(m?.[k]) : "num")}>{n(m?.[k], d ?? 2)}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function NewBacktest({ onDone }: { onDone: (id: number) => void }) {
  const [f, setF] = useState({ name: "", tickers: "", start: "", end: "", min_score: "60", max_positions: "8", risk: "0.5",
    slippage: "5", commission: "2", train: "252", test: "63", download: "0" });
  const [setups, setSetups] = useState<string[]>(SETUPS.map(([k]) => k));
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<unknown>(null);
  const set = (k: keyof typeof f) => (e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) => setF({ ...f, [k]: e.target.value });

  async function submit(e: React.FormEvent) {
    e.preventDefault(); setBusy(true); setErr(null);
    try {
      const tickers = f.tickers.split(/[\s,;]+/).map((t) => t.trim().toUpperCase()).filter(Boolean);
      const r = await api<BacktestRun>("/api/backtests", { method: "POST", body: JSON.stringify({
        name: f.name || null, tickers: tickers.length ? tickers : null, start: f.start || null, end: f.end || null,
        train_days: Number(f.train), test_days: Number(f.test), download_years: Number(f.download),
        params: { min_score: Number(f.min_score), max_positions: Number(f.max_positions), risk_per_trade_pct: Number(f.risk),
          slippage_bps: Number(f.slippage), commission_per_order: Number(f.commission), setups },
      }) });
      onDone(r.id);
    } catch (x) { setErr(x); } finally { setBusy(false); }
  }

  return (
    <Card>
      <CardHeader title="Nuovo backtest" subtitle="Eseguito dal worker in background: può richiedere alcuni minuti" />
      <form onSubmit={submit} className="grid gap-3 p-4 md:grid-cols-4">
        <div className="md:col-span-2"><Label>Nome</Label><Input value={f.name} onChange={set("name")} placeholder="es. Pullback, score ≥ 60" /></div>
        <div><Label hint="vuoto = fino all'ultimo dato">Inizio</Label><Input type="date" value={f.start} onChange={set("start")} /></div>
        <div><Label>Fine</Label><Input type="date" value={f.end} onChange={set("end")} /></div>
        <div className="md:col-span-4"><Label hint="vuoto = universo attivo dell'Opportunity Radar">Ticker</Label>
          <textarea value={f.tickers} onChange={set("tickers")} rows={2} placeholder="AAPL.US MSFT.US ENI.MI SAP.DE"
            className="w-full rounded-md border border-line bg-bg px-3 py-2 text-sm outline-none focus:border-accent" /></div>
        <div><Label>Punteggio minimo</Label><Input type="number" min={0} max={100} value={f.min_score} onChange={set("min_score")} /></div>
        <div><Label>Posizioni massime</Label><Input type="number" min={1} max={50} value={f.max_positions} onChange={set("max_positions")} /></div>
        <div><Label>Rischio per trade %</Label><Input type="number" step="0.1" min={0.1} max={5} value={f.risk} onChange={set("risk")} /></div>
        <div><Label>Slippage (bps)</Label><Input type="number" min={0} max={200} value={f.slippage} onChange={set("slippage")} /></div>
        <div><Label>Commissione per ordine €</Label><Input type="number" step="0.5" min={0} value={f.commission} onChange={set("commission")} /></div>
        <div><Label>Walk-forward: training (giorni)</Label><Input type="number" min={60} value={f.train} onChange={set("train")} /></div>
        <div><Label>Walk-forward: test (giorni)</Label><Input type="number" min={20} value={f.test} onChange={set("test")} /></div>
        <div><Label hint="0 = usa i dati già scaricati">Scarica storico (anni)</Label><Input type="number" min={0} max={15} value={f.download} onChange={set("download")} /></div>
        <div className="flex flex-wrap items-center gap-4 md:col-span-3">
          {SETUPS.map(([k, l]) => (
            <label key={k} className="flex items-center gap-1.5 text-xs">
              <input type="checkbox" checked={setups.includes(k)} onChange={(e) => setSetups(e.target.checked ? [...setups, k] : setups.filter((x) => x !== k))} />{l}
            </label>
          ))}
        </div>
        <div className="flex items-end"><Button className="w-full" disabled={busy || !setups.length}>{busy ? "Invio…" : "Avvia backtest"}</Button></div>
        <div className="md:col-span-4"><ErrorNote error={err} /></div>
      </form>
    </Card>
  );
}

function Detail({ id }: { id: number }) {
  const q = useApi<BacktestRun>(`/api/backtests/${id}`, 5000);
  const r = q.data;
  const res = r?.results;
  const series = useMemo(() => !res ? [] : [
    { name: "Strategia", color: "#4f9dff", data: res.equity, area: true },
    { name: "Walk-forward OOS", color: "#26c485", data: res.walk_forward.equity },
    ...(res.benchmark_curve.series ? [{ name: `Benchmark ${res.benchmark_curve.ticker}`, color: "#8791a2", data: res.benchmark_curve.series }] : []),
  ], [res]);
  const dd = useMemo(() => res ? [{ name: "Drawdown %", color: "#f0535c", data: res.drawdown, area: true }] : [], [res]);
  if (q.error) return <ErrorNote error={q.error} />;
  if (!r) return null;
  if (r.status !== "DONE" || !res) {
    return <Card className="p-4 text-sm">{r.status === "FAILED" ? <span className="text-down">Backtest fallito: {r.error}</span>
      : <span className="text-muted">Backtest #{r.id} {r.status === "QUEUED" ? "in coda" : "in esecuzione"}…</span>}</Card>;
  }
  const cal = res.calibration;
  const bs = res.bootstrap.walk_forward.available ? res.bootstrap.walk_forward : res.bootstrap.full;
  return (
    <div className="space-y-4">
      <Card>
        <CardHeader title={`#${r.id} · ${r.name}`} subtitle={`${res.period[0]} → ${res.period[1]} · ${res.universe_size} strumenti · ${res.trades_total} operazioni · ${res.engine_version}`}
          right={r.is_demo ? <DemoBadge /> : null} />
        <div className="p-2"><Lines series={series} /></div>
        <div className="border-t border-line p-2"><Lines series={dd} height={140} /></div>
        <p className="px-4 pb-3 text-[11px] text-muted">{res.disclaimer}</p>
      </Card>

      <div className="grid gap-4 xl:grid-cols-[2fr_1fr]">
        <Card>
          <CardHeader title="Metriche per periodo" subtitle={`Training ${res.split_bounds.train.join(" → ")} · Validazione ${res.split_bounds.validation.join(" → ")} · Test ${res.split_bounds.test.join(" → ")}`} />
          <MetricsTable cols={[...SEGMENTS.map(([k, l]) => [l, res.segments[k]] as [string, Metrics]), ["Walk-forward OOS", res.walk_forward.metrics],
            ...Object.entries(res.benchmarks).map(([t, m]) => [`Benchmark ${t}`, m] as [string, Metrics])]} />
        </Card>
        <div className="space-y-4">
          <Card>
            <CardHeader title="Calibrazione fuori campione" subtitle="Base per probabilità ed expected value dei piani" />
            <div className="space-y-2 p-4 text-xs">
              <p>{cal.overall.validated
                ? <Badge tone="up">Validata</Badge>
                : <Badge tone="warn">Non validata</Badge>} <span className="text-muted">min. {cal.min_trades} operazioni OOS{cal.is_demo ? ", dati DEMO esclusi" : ""}</span></p>
              {[["Complessiva", cal.overall] as const, ...Object.entries(cal.by_setup)].map(([k, b]) => (
                <p key={k}><b>{k}</b>: {b.trades} op.{b.win_rate !== undefined && <> · win rate {num(b.win_rate * 100, 1)}% (IC95 {num((b.win_rate_ci95?.[0] ?? 0) * 100, 1)}–{num((b.win_rate_ci95?.[1] ?? 0) * 100, 1)}%) · EV {num(b.ev_r, 2)} R</>}</p>
              ))}
              <p className="text-[11px] text-muted">{cal.note}</p>
            </div>
          </Card>
          <Card>
            <CardHeader title="Bootstrap" subtitle={bs.available ? `${bs.trades} operazioni ricampionate` : bs.note} />
            {bs.available && <div className="space-y-1 p-4 text-xs">
              <p>Expectancy € (2,5% · 50% · 97,5%): <span className="num">{bs.expectancy_base_p2_5_p50_p97_5?.map((x) => num(x)).join(" · ")}</span></p>
              <p>P&L totale €: <span className="num">{bs.total_pnl_base_p2_5_p50_p97_5?.map((x) => num(x, 0)).join(" · ")}</span></p>
              <p>Profit factor: <span className="num">{bs.profit_factor_p2_5_p50_p97_5?.map((x) => num(x)).join(" · ")}</span></p>
              <p>Probabilità di expectancy ≤ 0: <b>{num(bs.prob_expectancy_negative_pct, 1)}%</b></p>
            </div>}
          </Card>
        </div>
      </div>

      <div className="grid gap-4 lg:grid-cols-3">
        <Card>
          <CardHeader title="Stress test" subtitle="Stesso periodo con condizioni peggiori" />
          <MetricsTable cols={Object.entries(res.stress).map(([k, m]) => [k.replace(/_/g, " "), m])} />
        </Card>
        <Card>
          <CardHeader title="Per regime di mercato" subtitle="Operazioni per regime all'ingresso; rendimento annualizzato dei giorni" />
          <table className="w-full text-xs"><tbody className="divide-y divide-line">
            {Object.entries(res.by_regime_days).map(([k, v]) => {
              const t = res.by_regime_trades[k];
              return <tr key={k}><td className="px-3 py-1">{k}</td><td className="num px-3 py-1 text-right">{v.days} gg</td>
                <td className={cn("px-3 py-1 text-right", signed(v.annualized_return_pct))}>{num(v.annualized_return_pct)}%</td>
                <td className="num px-3 py-1 text-right text-muted">{t ? `${t.trades} op · PF ${n(t.profit_factor)}` : "—"}</td></tr>;
            })}
          </tbody></table>
        </Card>
        <Card>
          <CardHeader title="Per setup" />
          <table className="w-full text-xs"><tbody className="divide-y divide-line">
            {Object.entries(res.by_setup).map(([k, m]) => (
              <tr key={k}><td className="px-3 py-1">{k}</td><td className="num px-3 py-1 text-right">{m.trades} op</td>
                <td className="num px-3 py-1 text-right">WR {n(m.win_rate_pct, 1)}%</td><td className={cn("px-3 py-1 text-right", signed(m.total_pnl_base))}>{n(m.total_pnl_base)} €</td></tr>
            ))}
          </tbody></table>
        </Card>
      </div>

      <Card>
        <CardHeader title="Walk-forward" subtitle={`Ottimizzazione del punteggio minimo su ${res.walk_forward.train_days} giorni, verifica sui ${res.walk_forward.test_days} successivi (griglia ${res.walk_forward.grid_min_score.join(", ")})`} />
        <div className="overflow-x-auto"><table className="w-full text-xs">
          <thead><tr className="border-b border-line text-left text-muted">{["Training", "Test", "Score scelto", "Rend. test %", "Max DD %", "Sharpe", "Operazioni"].map((h) => <th key={h} className="px-3 py-1.5 font-medium">{h}</th>)}</tr></thead>
          <tbody className="divide-y divide-line">{res.walk_forward.windows.map((w, i) => (
            <tr key={i}><td className="px-3 py-1">{w.train.join(" → ")}</td><td className="px-3 py-1">{w.test.join(" → ")}</td>
              <td className="num px-3 py-1">{w.chosen_min_score}</td><td className={cn("px-3 py-1", signed(w.result.net_return_pct))}>{n(w.result.net_return_pct)}</td>
              <td className="num px-3 py-1">{n(w.result.max_drawdown_pct)}</td><td className="num px-3 py-1">{n(w.result.sharpe)}</td><td className="num px-3 py-1">{w.result.trades}</td></tr>
          ))}</tbody>
        </table></div>
      </Card>

      <Card>
        <CardHeader title="Operazioni simulate" subtitle={`Ultime ${Math.min(100, res.trades.length)} di ${res.trades_total}`} />
        <div className="max-h-[420px] overflow-auto"><table className="w-full text-xs">
          <thead className="sticky top-0 bg-panel"><tr className="border-b border-line text-left text-muted">{["Titolo", "Setup", "Regime", "Ingresso", "Prezzo", "Stop", "Uscita", "Motivo", "P&L €", "R", "Giorni"].map((h) => <th key={h} className="px-3 py-1.5 font-medium">{h}</th>)}</tr></thead>
          <tbody className="divide-y divide-line">{res.trades.slice(-100).reverse().map((t, i) => (
            <tr key={i}><td className="px-3 py-1">{t.ticker}</td><td className="px-3 py-1">{t.setup}</td><td className="px-3 py-1">{t.regime}</td>
              <td className="px-3 py-1">{t.entry_date}</td><td className="num px-3 py-1">{num(t.entry_price)}</td><td className="num px-3 py-1">{num(t.stop)}</td>
              <td className="px-3 py-1">{t.exit_date}</td><td className="px-3 py-1">{t.exit_reason}</td>
              <td className={cn("px-3 py-1", signed(t.pnl_base))}>{num(t.pnl_base)}</td><td className="num px-3 py-1">{n(t.r_multiple)}</td><td className="num px-3 py-1">{t.holding_days}</td></tr>
          ))}</tbody>
        </table></div>
      </Card>

      <Card>
        <CardHeader title="Controllo dei bias e limiti" />
        <ul className="space-y-1 p-4 text-xs">
          {Object.entries(res.bias_controls).map(([k, v]) => <li key={k}><b>{k}</b>: {v}</li>)}
          {res.notes.map((x) => <li key={x} className="text-warn">{x}</li>)}
          {res.missing?.length ? <li className="text-warn">Esclusi: {res.missing.slice(0, 20).join(", ")}</li> : null}
        </ul>
      </Card>
    </div>
  );
}

export default function LabPage() {
  const { user } = useSession();
  const list = useApi<Page<BacktestRun>>("/api/backtests?page_size=50", 5000);
  const [sel, setSel] = useState<number | null>(null);
  const [cmp, setCmp] = useState<number[]>([]);
  const runs = list.data?.items ?? [];
  const current = sel ?? runs.find((r) => r.status === "DONE")?.id ?? null;
  const compared = runs.filter((r) => cmp.includes(r.id));

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-lg font-semibold">Strategy Laboratory</h1>
        <p className="text-xs text-muted">Backtest point-in-time con costi, gap, walk-forward, bootstrap e stress test. I risultati simulati non garantiscono rendimenti futuri.</p>
      </div>
      {user.role !== "viewer" && <NewBacktest onDone={(id) => { setSel(id); list.reload(); }} />}

      <Card>
        <CardHeader title="Backtest eseguiti" subtitle="Seleziona per i dettagli; spunta due o più righe per confrontarle" />
        {runs.length === 0 ? <Empty title="Nessun backtest" body="Avvia il primo backtest con il modulo qui sopra." /> : (
          <div className="overflow-x-auto"><table className="w-full text-xs">
            <thead><tr className="border-b border-line text-left text-muted">
              {["", "#", "Nome", "Stato", "Periodo", "Strum.", "Rend. %", "CAGR %", "Max DD %", "Sharpe", "PF", "Op.", "OOS WF %", "Calibr.", ""].map((h, i) => <th key={i} className="whitespace-nowrap px-3 py-1.5 font-medium">{h}</th>)}
            </tr></thead>
            <tbody className="divide-y divide-line">
              {runs.map((r) => (
                <tr key={r.id} className={cn("cursor-pointer hover:bg-white/[0.02]", current === r.id && "bg-accent/5")} onClick={() => setSel(r.id)}>
                  <td className="px-3 py-1" onClick={(e) => e.stopPropagation()}>
                    <input type="checkbox" disabled={r.status !== "DONE"} checked={cmp.includes(r.id)}
                      onChange={(e) => setCmp(e.target.checked ? [...cmp, r.id] : cmp.filter((x) => x !== r.id))} /></td>
                  <td className="num px-3 py-1">{r.id}</td><td className="px-3 py-1">{r.name}</td>
                  <td className="px-3 py-1"><Badge tone={STATUS_TONE[r.status]}>{r.status}</Badge></td>
                  <td className="whitespace-nowrap px-3 py-1">{r.period_start ? `${r.period_start} → ${r.period_end}` : when(r.created_at)}</td>
                  <td className="num px-3 py-1">{r.universe_size || "—"}</td>
                  <td className={cn("px-3 py-1", signed(r.summary.net_return_pct))}>{n(r.summary.net_return_pct)}</td>
                  <td className="num px-3 py-1">{n(r.summary.cagr_pct)}</td><td className="num px-3 py-1">{n(r.summary.max_drawdown_pct)}</td>
                  <td className="num px-3 py-1">{n(r.summary.sharpe)}</td><td className="num px-3 py-1">{n(r.summary.profit_factor)}</td>
                  <td className="num px-3 py-1">{r.summary.trades ?? "—"}</td>
                  <td className={cn("px-3 py-1", signed(r.summary.oos_walk_forward?.net_return_pct))}>{n(r.summary.oos_walk_forward?.net_return_pct)}</td>
                  <td className="px-3 py-1">{r.status === "DONE" ? (r.summary.calibration_validated ? <Badge tone="up">Sì</Badge> : <Badge>No</Badge>) : "—"}</td>
                  <td className="px-3 py-1">{r.is_demo && <DemoBadge />}</td>
                </tr>
              ))}
            </tbody>
          </table></div>
        )}
        <ErrorNote error={list.error} />
      </Card>

      {compared.length >= 2 && (
        <Card>
          <CardHeader title="Confronto" subtitle="Intero periodo; tra parentesi nessuna garanzia di risultati futuri" />
          <MetricsTable cols={compared.map((r) => [`#${r.id} ${r.name}`, r.summary])} />
          <table className="w-full text-xs"><tbody className="divide-y divide-line border-t border-line">
            {[["Punteggio minimo", "min_score"], ["Posizioni max", "max_positions"], ["Rischio %", "risk_per_trade_pct"], ["Slippage bps", "slippage_bps"], ["Setup", "setups"]].map(([l, k]) => (
              <tr key={k}><td className="px-3 py-1 text-muted">{l}</td>{compared.map((r) => <td key={r.id} className="px-3 py-1 text-right">{String(r.params.params?.[k] ?? "—")}</td>)}</tr>
            ))}
            <tr><td className="px-3 py-1 text-muted">Walk-forward OOS %</td>{compared.map((r) => <td key={r.id} className={cn("px-3 py-1 text-right", signed(r.summary.oos_walk_forward?.net_return_pct))}>{n(r.summary.oos_walk_forward?.net_return_pct)}</td>)}</tr>
          </tbody></table>
        </Card>
      )}

      {current && <Detail id={current} />}
    </div>
  );
}
