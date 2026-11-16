"use client";
import { useState } from "react";
import { api } from "@/lib/api";
import { useApi } from "@/lib/hooks";
import { cn, num, pct, tone, when } from "@/lib/format";
import { Badge, Button, Card, CardHeader, DemoBadge, Empty, ErrorNote, Table } from "@/components/ui/primitives";
import { Lines } from "@/components/lines";
import { useSession } from "@/components/shell";

type Block = {
  trades: number; validated: boolean; win_rate?: number; win_rate_ci95?: [number, number]; avg_r?: number;
  avg_r_ci95?: [number, number]; median_r?: number; profit_factor_r?: number | null;
};
type Summary = {
  plans_tracked: number; by_outcome: Record<string, number>; is_demo: boolean; trigger_rate: number | null;
  overall: Block; by_status_at_creation: Record<string, Block>; by_setup: Record<string, Block>;
  by_score: Record<string, Block>; by_committee: Record<string, Block>; min_trades: number;
  backtest_reference: { trades: number; win_rate: number | null; win_rate_ci95: [number, number] | null; ev_r: number | null;
    validated: boolean; is_demo: boolean; period: [string, string] } | null;
  drift: { status: string; message: string }; notes: string[];
  paper_trading: { closed_positions: number; wins: number; win_rate: number | null; realized_pnl_base: number; is_demo: boolean };
  last_run: { at: string; update: { errors: string[]; backfilled: number; changed: number } | null } | null;
  gate: { status: string; message: string } | null; job: { status: string; error: string | null } | null;
};
type Outcome = {
  id: number; trade_plan_id: number; ticker: string; setup: string | null; score: string | null; status_at_creation: string;
  committee_decision: string | null; created_for: string; outcome: string; entry_date: string | null; exit_date: string | null;
  r_net: string | null; mfe_r: string | null; mae_r: string | null; bars_evaluated: number; is_demo: boolean; notes: string[];
};
type Bench = {
  portfolio: [string, number][]; portfolio_return_pct?: number; period?: [string, string]; notes: string[];
  benchmarks: { ticker: string; currency: string; return_pct: number; excess_pct: number; series: [string, number][]; is_demo: boolean }[];
};
type Health = {
  instruments: number; by_status: Record<string, number>; notes: string[];
  items: { ticker: string; source: string; last_bar: string | null; missing_bdays: number; last_quote_age_min: number | null;
    anomalies_7d: number; status: string; issues: string[]; is_demo: boolean }[];
};
type Signals = {
  avg_top_overlap: number | null; avg_error_rate: number | null; avg_optional_missing_rate: number | null; notes: string[];
  scans: { scan_id: number; as_of: string | null; universe: number; eligible: number | null; error_rate: number | null; top_overlap_prev: number | null }[];
};

const OUTCOME: Record<string, string> = {
  PENDING: "In corso", WIN: "TP1 raggiunto", LOSS: "Stop", TIMEOUT: "Orizzonte scaduto", NOT_TRIGGERED: "Ingresso mai raggiunto",
  INVALIDATED: "Invalidato prima dell'ingresso", NOT_EVALUABLE: "Livelli non validi",
};
const DRIFT_TONE: Record<string, "up" | "down" | "warn" | "neutral"> = {
  CONSISTENT: "up", DEGRADED: "down", NEGATIVE_EDGE: "down", INSUFFICIENT: "neutral",
};
const COLORS = ["#f5a524", "#a78bfa", "#22c55e"];

const r = (x: number | null | undefined) => (x == null ? "—" : `${x > 0 ? "+" : ""}${x.toFixed(2)} R`);
const p100 = (x: number | null | undefined) => (x == null ? "—" : `${(x * 100).toFixed(0)}%`);

function Groups({ title, data }: { title: string; data: Record<string, Block> }) {
  const rows = Object.entries(data);
  return (
    <Card>
      <CardHeader title={title} />
      {rows.length === 0 ? <Empty title="Nessuna operazione chiusa" /> : (
        <Table head={["Gruppo", "Operazioni", "Win rate (IC 95%)", "R medio", ""]}>
          {rows.map(([k, b]) => (
            <tr key={k}>
              <td className="px-4 py-2">{k}</td>
              <td className="num px-4 py-2">{b.trades}</td>
              <td className="num px-4 py-2">{p100(b.win_rate)} <span className="text-muted">{b.win_rate_ci95 ? `${p100(b.win_rate_ci95[0])}–${p100(b.win_rate_ci95[1])}` : ""}</span></td>
              <td className={cn("num px-4 py-2", tone(b.avg_r))}>{r(b.avg_r)}</td>
              <td className="px-4 py-2">{b.validated ? <Badge tone="up">Validato</Badge> : <Badge>Campione insufficiente</Badge>}</td>
            </tr>
          ))}
        </Table>
      )}
    </Card>
  );
}

export default function ValidationPage() {
  const { user } = useSession();
  const s = useApi<Summary>("/api/validation/summary", 30_000);
  const out = useApi<{ items: Outcome[]; total: number }>("/api/validation/outcomes?page_size=50", 60_000);
  const bench = useApi<Bench>("/api/validation/benchmark");
  const health = useApi<Health>("/api/validation/data-health");
  const sig = useApi<Signals>("/api/validation/signals");
  const [err, setErr] = useState<unknown>(null);
  const [queued, setQueued] = useState(false);

  const run = async () => {
    setErr(null);
    try { await api("/api/validation/run", { method: "POST" }); setQueued(true); setTimeout(() => { s.reload(); out.reload(); }, 4000); }
    catch (e) { setErr(e); }
  };
  const d = s.data;
  const o = d?.overall;
  const ref = d?.backtest_reference;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold">Validazione prospettica</h1>
          <p className="max-w-3xl text-xs text-muted">Ogni piano viene giudicato solo con i dati arrivati dopo la sua creazione. Servono almeno
            {" "}{d?.min_trades ?? 30} operazioni chiuse su dati reali per trarre conclusioni; con dati DEMO la validazione non si applica.
            Se i risultati peggiorano in modo significativo, i nuovi piani smettono di mostrare probabilità ed EV.</p>
        </div>
        {user.role !== "viewer" && <Button onClick={run}>Aggiorna esiti</Button>}
      </div>
      <ErrorNote error={err} />
      {queued && <p className="text-xs text-muted">Aggiornamento accodato al worker{d?.job ? ` (stato: ${d.job.status})` : ""}.</p>}

      {d && (
        <Card className="px-4 py-3">
          <div className="flex flex-wrap items-center gap-2">
            <Badge tone={DRIFT_TONE[d.drift.status] ?? "neutral"}>{d.drift.status}</Badge>
            <span className="text-sm">{d.drift.message}</span>
            {d.is_demo && <DemoBadge />}
          </div>
          {d.gate && <p className="mt-1 text-xs text-down">Probabilità ed EV calibrati sospesi nei nuovi piani: {d.gate.message}</p>}
          <p className="mt-1 text-[11px] text-muted">Ultimo aggiornamento: {d.last_run ? when(d.last_run.at) : "mai"}
            {d.last_run?.update?.errors?.length ? ` · ${d.last_run.update.errors.length} strumenti senza dati aggiornati` : ""}</p>
        </Card>
      )}

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-6">
        {[
          ["Piani tracciati", d?.plans_tracked ?? "—"],
          ["Operazioni chiuse", o?.trades ?? "—"],
          ["Win rate", o?.trades ? `${p100(o.win_rate)} (${p100(o.win_rate_ci95?.[0])}–${p100(o.win_rate_ci95?.[1])})` : "—"],
          ["R medio netto", o?.trades ? r(o.avg_r) : "—"],
          ["Profit factor (R)", o?.profit_factor_r != null ? num(o.profit_factor_r) : "—"],
          ["Tasso di ingresso", d?.trigger_rate != null ? p100(d.trigger_rate) : "—"],
        ].map(([k, v]) => (
          <Card key={String(k)} className="px-4 py-3">
            <p className="text-[11px] uppercase tracking-wider text-muted">{k}</p>
            <p className="num mt-1 text-lg font-semibold">{v}</p>
          </Card>
        ))}
      </div>

      <div className="grid gap-4 xl:grid-cols-2">
        <Card>
          <CardHeader title="Confronto con il backtest" subtitle="Calibrazione walk-forward fuori campione (Fase 5)" />
          {!ref ? <Empty title="Nessun walk-forward disponibile" body="Esegui un backtest dallo Strategy Lab." /> : (
            <Table head={["", "Operazioni", "Win rate", "R atteso / medio"]}>
              <tr><td className="px-4 py-2">Backtest {ref.is_demo && <DemoBadge />}</td><td className="num px-4 py-2">{ref.trades}</td>
                <td className="num px-4 py-2">{p100(ref.win_rate)}</td><td className="num px-4 py-2">{r(ref.ev_r)}</td></tr>
              <tr><td className="px-4 py-2">Prospettico {d?.is_demo && <DemoBadge />}</td><td className="num px-4 py-2">{o?.trades ?? 0}</td>
                <td className="num px-4 py-2">{p100(o?.win_rate)}</td><td className="num px-4 py-2">{r(o?.avg_r)}</td></tr>
            </Table>
          )}
          <p className="border-t border-line px-4 py-2 text-[11px] text-muted">
            Paper trading: {d?.paper_trading.closed_positions ?? 0} posizioni chiuse, P&amp;L realizzato {num(d?.paper_trading.realized_pnl_base)}
            {d?.paper_trading.is_demo ? " (dati DEMO)" : ""}.
          </p>
        </Card>
        <Card>
          <CardHeader title="Portafoglio simulato e benchmark" subtitle={bench.data?.period ? `${bench.data.period[0]} → ${bench.data.period[1]}` : "Base 100 = equity del primo giorno"} />
          {!bench.data || bench.data.portfolio.length < 2 ? <Empty title="Confronto non ancora disponibile" body={bench.data?.notes[0]} /> : (
            <div className="p-2">
              <Lines height={220} series={[
                { name: "Portafoglio", color: "#4f8cff", data: bench.data.portfolio },
                ...bench.data.benchmarks.map((b, i) => ({ name: b.ticker, color: COLORS[i % COLORS.length], data: b.series })),
              ]} />
              <div className="flex flex-wrap gap-x-4 px-2 text-xs">
                <span>Portafoglio <span className={cn("num", tone(bench.data.portfolio_return_pct))}>{pct(bench.data.portfolio_return_pct)}</span></span>
                {bench.data.benchmarks.map((b) => (
                  <span key={b.ticker}>{b.ticker} <span className="num">{pct(b.return_pct)}</span> · extra <span className={cn("num", tone(b.excess_pct))}>{pct(b.excess_pct)}</span>{b.is_demo && " (DEMO)"}</span>
                ))}
              </div>
              {bench.data.notes.map((n) => <p key={n} className="px-2 text-[11px] text-muted">{n}</p>)}
            </div>
          )}
        </Card>
      </div>

      <div className="grid gap-4 xl:grid-cols-2">
        <Groups title="Per stato iniziale del piano (READY deve fare meglio di WAIT e REJECTED)" data={d?.by_status_at_creation ?? {}} />
        <Groups title="Per fascia di punteggio" data={d?.by_score ?? {}} />
        <Groups title="Per setup" data={d?.by_setup ?? {}} />
        <Groups title="Per decisione del comitato" data={d?.by_committee ?? {}} />
      </div>

      <Card>
        <CardHeader title="Esiti dei piani" subtitle={`${out.data?.total ?? 0} piani · uno per segnale`} />
        {out.data?.items.length === 0 ? <Empty title="Nessun piano tracciato" body="I piani generati dalle scansioni vengono registrati automaticamente." /> : (
          <Table head={["Dati al", "Titolo", "Setup", "Punteggio", "Stato iniziale", "Comitato", "Esito", "Ingresso", "Uscita", "R netto", "MFE / MAE", ""]}>
            {out.data?.items.map((x) => (
              <tr key={x.id} title={x.notes.join(" · ")}>
                <td className="whitespace-nowrap px-4 py-2 text-xs">{x.created_for}</td>
                <td className="px-4 py-2 font-medium">{x.ticker}</td>
                <td className="px-4 py-2 text-xs">{x.setup ?? "—"}</td>
                <td className="num px-4 py-2">{num(x.score, 0)}</td>
                <td className="px-4 py-2 text-xs">{x.status_at_creation}</td>
                <td className="px-4 py-2 text-xs">{x.committee_decision ?? "—"}</td>
                <td className="whitespace-nowrap px-4 py-2"><Badge tone={x.outcome === "WIN" ? "up" : x.outcome === "LOSS" ? "down" : x.outcome === "PENDING" ? "warn" : "neutral"}>{OUTCOME[x.outcome] ?? x.outcome}</Badge></td>
                <td className="px-4 py-2 text-xs">{x.entry_date ?? "—"}</td>
                <td className="px-4 py-2 text-xs">{x.exit_date ?? "—"}</td>
                <td className={cn("num px-4 py-2", tone(x.r_net))}>{x.r_net != null ? r(Number(x.r_net)) : "—"}</td>
                <td className="num px-4 py-2 text-xs text-muted">{x.mfe_r != null ? `${num(x.mfe_r)} / ${num(x.mae_r)}` : "—"}</td>
                <td className="px-4 py-2">{x.is_demo && <DemoBadge />}</td>
              </tr>
            ))}
          </Table>
        )}
      </Card>

      <div className="grid gap-4 xl:grid-cols-3">
        <Card className="xl:col-span-2">
          <CardHeader title="Affidabilità dei dati" subtitle={health.data ? `${health.data.instruments} strumenti in uso · ${Object.entries(health.data.by_status).map(([k, v]) => `${k} ${v}`).join(" · ")}` : undefined} />
          {health.data?.items.length === 0 ? <Empty title="Nessuno strumento in uso" /> : (
            <Table head={["Titolo", "Fonte", "Ultima barra", "Giorni mancanti", "Età quotazione", "Stato"]}>
              {health.data?.items.map((h) => (
                <tr key={h.ticker} title={h.issues.join(" · ")}>
                  <td className="px-4 py-2 font-medium">{h.ticker}</td>
                  <td className="px-4 py-2 text-xs">{h.source}</td>
                  <td className="px-4 py-2 text-xs">{h.last_bar ?? "—"}</td>
                  <td className="num px-4 py-2">{h.missing_bdays}</td>
                  <td className="num px-4 py-2 text-xs">{h.last_quote_age_min != null ? `${h.last_quote_age_min} min` : "—"}</td>
                  <td className="px-4 py-2"><Badge tone={h.status === "OK" ? "up" : h.status === "BAD" ? "down" : "warn"}>{h.status}</Badge>
                    {h.issues[0] && <p className="mt-0.5 text-[11px] text-muted">{h.issues[0]}</p>}</td>
                </tr>
              ))}
            </Table>
          )}
          {health.data?.notes.map((n) => <p key={n} className="border-t border-line px-4 py-1.5 text-[11px] text-muted">{n}</p>)}
        </Card>
        <Card>
          <CardHeader title="Stabilità dei segnali" subtitle="Ultime scansioni completate" />
          {sig.data?.scans.length === 0 ? <Empty title="Nessuna scansione" /> : (
            <div className="space-y-1 p-4 text-xs">
              <p>Sovrapposizione media dei primi titoli: <b className="num">{sig.data?.avg_top_overlap != null ? p100(sig.data.avg_top_overlap) : "—"}</b></p>
              <p>Titoli con errori su prezzi o storico: <b className="num">{sig.data?.avg_error_rate != null ? p100(sig.data.avg_error_rate) : "—"}</b></p>
              <p>Titoli senza dati facoltativi: <b className="num">{sig.data?.avg_optional_missing_rate != null ? p100(sig.data.avg_optional_missing_rate) : "—"}</b></p>
              <ul className="mt-2 divide-y divide-line">
                {sig.data?.scans.map((x) => (
                  <li key={x.scan_id} className="flex justify-between py-1"><span>#{x.scan_id} · {x.as_of ?? "—"}</span>
                    <span className="num text-muted">{x.eligible ?? "—"}/{x.universe} idonei · overlap {x.top_overlap_prev != null ? p100(x.top_overlap_prev) : "—"}</span></li>
                ))}
              </ul>
              {sig.data?.notes.map((n) => <p key={n} className="pt-2 text-[11px] text-muted">{n}</p>)}
            </div>
          )}
        </Card>
      </div>
      {d?.notes && <ul className="space-y-0.5 text-[11px] text-muted">{d.notes.map((n) => <li key={n}>· {n}</li>)}</ul>}
    </div>
  );
}
