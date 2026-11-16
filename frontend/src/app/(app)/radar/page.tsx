"use client";
import { useState } from "react";
import { Play, RefreshCw } from "lucide-react";
import { api, FACTOR_LABELS, REGIME_LABELS, type Page, type RankItem, type Ranking, type ScanRun } from "@/lib/api";
import { useApi } from "@/lib/hooks";
import { cn, num, when } from "@/lib/format";
import { Badge, Button, Card, CardHeader, DemoBadge, Empty, ErrorNote, Input } from "@/components/ui/primitives";
import { useSession } from "@/components/shell";

const SETUPS = { BREAKOUT: "Breakout", PULLBACK: "Pullback", TREND_FOLLOWING: "Trend following" } as Record<string, string>;
const RISK = { LOW: "Basso", MEDIUM: "Medio", HIGH: "Alto" } as Record<string, string>;

function ScoreBar({ v }: { v: number | null | undefined }) {
  if (v === null || v === undefined) return <span className="text-[11px] text-muted">n/d</span>;
  return (
    <div className="flex items-center gap-2">
      <div className="h-1.5 w-20 rounded bg-raised"><div className="h-1.5 rounded bg-accent" style={{ width: `${v}%` }} /></div>
      <span className="num text-xs">{num(v, 0)}</span>
    </div>
  );
}

export default function RadarPage() {
  const { user } = useSession();
  const [f, setF] = useState({ exchange: "", sector: "", risk_level: "", setup: "", style: "", instrument_type: "", excluded: false });
  const qs = new URLSearchParams({ page_size: "200", include_excluded: String(f.excluded) });
  Object.entries(f).forEach(([k, v]) => { if (typeof v === "string" && v) qs.set(k, v); });
  const rk = useApi<Ranking>(`/api/rankings/latest?${qs}`);
  const scans = useApi<Page<ScanRun>>("/api/scans?page_size=1", 5000);
  const uni = useApi<Page<{ id: number }>>("/api/universe?page_size=1");
  const [sel, setSel] = useState<RankItem | null>(null);
  const [err, setErr] = useState<unknown>(null);
  const [tickers, setTickers] = useState("");
  const current = scans.data?.items[0];
  const running = current && (current.status === "QUEUED" || current.status === "RUNNING");
  const scan = rk.data?.scan;
  const canWrite = user.role !== "viewer";

  async function act(fn: () => Promise<unknown>) {
    setErr(null);
    try { await fn(); scans.reload(); uni.reload(); rk.reload(); } catch (e) { setErr(e); }
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold">Opportunity Radar</h1>
          <p className="text-xs text-muted">
            Classifica dei candidati per punteggio composito. Il punteggio ordina i titoli: non è una raccomandazione,
            né una probabilità di successo. Ingressi, stop e target arrivano con il Trade Planner (Fase 3).
          </p>
        </div>
        {canWrite && (
          <div className="flex items-center gap-2">
            {running && <span className="text-xs text-muted">Scansione #{current!.id} {current!.status === "QUEUED" ? "in coda" : `in corso ${current!.progress}/${current!.total}`}</span>}
            <Button disabled={!!running} onClick={() => act(() => api("/api/scans", { method: "POST", body: JSON.stringify({ refresh_data: true }) }))}>
              <Play className="h-3.5 w-3.5" /> Avvia scansione
            </Button>
            <Button variant="outline" onClick={() => { scans.reload(); rk.reload(); }}><RefreshCw className="h-3.5 w-3.5" /></Button>
          </div>
        )}
      </div>
      <ErrorNote error={err} />
      {current?.status === "FAILED" && <ErrorNote error={`Ultima scansione #${current.id} fallita: ${current.errors.at(-1)?.error ?? ""}`} />}

      {uni.data && uni.data.total === 0 && canWrite && (
        <Card className="p-4 text-sm">
          <p className="font-medium">L&apos;universo investibile è vuoto.</p>
          <p className="mt-1 text-xs text-muted">Carica la lista iniziale (large cap ed ETF liquidi di USA ed Europa) o aggiungi i tuoi ticker.</p>
          <div className="mt-3 flex flex-wrap gap-2">
            <Button onClick={() => act(() => api("/api/universe/seed", { method: "POST" }))}>Carica lista iniziale</Button>
            <Input className="w-72" placeholder="AAPL.US ENI.MI SAP.DE" value={tickers} onChange={(e) => setTickers(e.target.value)} />
            <Button variant="outline" disabled={!tickers.trim()} onClick={() => act(() => api("/api/universe", { method: "POST", body: JSON.stringify({ tickers: tickers.split(/[\s,;]+/).filter(Boolean) }) }))}>Aggiungi</Button>
          </div>
        </Card>
      )}

      {scan && (
        <div className="grid gap-3 md:grid-cols-4">
          <Card className="px-4 py-3">
            <p className="text-[11px] uppercase tracking-wider text-muted">Regime di mercato</p>
            <p className="mt-1 text-lg font-semibold">{REGIME_LABELS[scan.regime?.regime ?? "UNKNOWN"]}</p>
            <p className="text-[11px] text-muted">{scan.regime?.benchmarks.map((b) => `${b.ticker} ${b.trend}`).join(" · ")}</p>
          </Card>
          <Card className="px-4 py-3">
            <p className="text-[11px] uppercase tracking-wider text-muted">Scansione</p>
            <p className="mt-1 text-sm">#{scan.id} · dati al {scan.as_of}</p>
            <p className="text-[11px] text-muted">{scan.data_source} · {scan.model_version} · {when(scan.finished_at)}</p>
          </Card>
          <Card className="px-4 py-3">
            <p className="text-[11px] uppercase tracking-wider text-muted">Universo</p>
            <p className="num mt-1 text-sm">{scan.stats.analysed} analizzati · {scan.stats.eligible} idonei · {scan.stats.excluded} esclusi</p>
            <p className="text-[11px] text-muted">{scan.stats.errors} errori di acquisizione dati</p>
          </Card>
          <Card className="px-4 py-3">
            <p className="text-[11px] uppercase tracking-wider text-muted">Macro</p>
            <p className="text-xs">VIX: {scan.regime?.vix ? `${num(scan.regime.vix.value, 1)} (${scan.regime.vix.as_of})` : "n/d"}</p>
            <p className="text-xs">US 10Y: {scan.regime?.rates ? `${num(scan.regime.rates.us10y, 2)}% (${num(scan.regime.rates.change_3m, 2)} in 3m)` : "n/d"}</p>
          </Card>
        </div>
      )}

      <Card>
        <div className="flex flex-wrap items-center gap-2 border-b border-line p-3 text-xs">
          {([
            ["exchange", "Mercato", rk.data?.facets?.exchanges ?? []],
            ["sector", "Settore", rk.data?.facets?.sectors ?? []],
            ["risk_level", "Rischio", Object.keys(RISK)],
            ["setup", "Setup", Object.keys(SETUPS)],
            ["style", "Orizzonte", ["SWING", "POSITION"]],
            ["instrument_type", "Strumento", ["STOCK", "ETF"]],
          ] as [keyof typeof f, string, string[]][]).map(([k, label, opts]) => (
            <select key={k} value={f[k] as string} onChange={(e) => setF({ ...f, [k]: e.target.value })}
              className="h-8 rounded-md border border-line bg-bg px-2 text-xs">
              <option value="">{label}: tutti</option>
              {opts.map((o) => <option key={o} value={o}>{SETUPS[o] ?? RISK[o] ?? o}</option>)}
            </select>
          ))}
          <label className="ml-2 inline-flex items-center gap-1.5 text-muted">
            <input type="checkbox" checked={f.excluded} onChange={(e) => setF({ ...f, excluded: e.target.checked })} /> mostra esclusi
          </label>
          <span className="ml-auto text-muted">{rk.data?.total ?? 0} titoli</span>
        </div>
        {!scan ? <Empty title="Nessuna scansione completata" body="Avvia una scansione: il worker scarica i dati, calcola indicatori e regime e produce la classifica." /> : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-line text-left text-[11px] uppercase tracking-wider text-muted">
                  {["#", "Titolo", "Prezzo", "Punteggio", "Setup", "Orizzonte", "Rischio", "Rend. netto", "Confidenza", ""].map((h) => <th key={h} className="whitespace-nowrap px-4 py-2 font-medium">{h}</th>)}
                </tr>
              </thead>
              <tbody className="divide-y divide-line">
                {rk.data?.items.map((it, i) => (
                  <tr key={it.id} onClick={() => setSel(it)} className={cn("cursor-pointer hover:bg-raised/50", sel?.id === it.id && "bg-raised", !it.eligible && "opacity-60")}>
                    <td className="num px-4 py-2 text-muted">{it.score !== null ? i + 1 : "—"}</td>
                    <td className="px-4 py-2"><p className="font-medium">{it.ticker}</p><p className="text-[11px] text-muted">{it.name} · {it.sector ?? it.instrument_type}</p></td>
                    <td className="num px-4 py-2">{num(it.price)} <span className="text-muted">{it.currency}</span><p className="text-[11px] text-muted">{it.price_date}</p></td>
                    <td className="px-4 py-2">{it.eligible ? <ScoreBar v={it.score !== null ? Number(it.score) : null} /> : <Badge tone="down">Escluso</Badge>}</td>
                    <td className="px-4 py-2 text-xs">{it.setup ? SETUPS[it.setup] : <span className="text-muted">nessuno</span>}</td>
                    <td className="px-4 py-2 text-xs">{it.horizon ?? "—"}</td>
                    <td className="px-4 py-2 text-xs">{it.risk_level ? RISK[it.risk_level] : "—"}</td>
                    <td className="px-4 py-2 text-[11px] text-muted" title="Calcolato dal Trade Planner (Fase 3)">n/d</td>
                    <td className="px-4 py-2 text-[11px] text-muted" title="Disponibile solo con modello calibrato out-of-sample">non calibrata</td>
                    <td className="px-4 py-2">{it.is_demo && <DemoBadge />}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {sel && <SignalDetail id={sel.id} weights={rk.data?.weights ?? {}} />}
    </div>
  );
}

function SignalDetail({ id, weights }: { id: number; weights: Record<string, number> }) {
  const d = useApi<RankItem>(`/api/rankings/signals/${id}`);
  const it = d.data;
  if (!it) return null;
  const ft = it.details?.features ?? {};
  const show: [string, string, number][] = [
    ["EMA 20", "ema20", 2], ["EMA 50", "ema50", 2], ["EMA 200", "ema200", 2], ["RSI 14", "rsi14", 1],
    ["MACD", "macd", 3], ["ATR 14", "atr14", 2], ["ADX", "adx", 1], ["Boll. %B", "bb_pct_b", 2],
    ["Vol. relativo", "rvol20", 2], ["VWAP 20g", "vwap20", 2], ["Mom. 1m %", "mom_1m", 1], ["Mom. 3m %", "mom_3m", 1],
    ["Mom. 6m %", "mom_6m", 1], ["Mom. 12m %", "mom_12m", 1], ["Vol. storica 20g %", "vol20", 1],
    ["Forza rel. 3m %", "rs_3m", 1], ["Max 52s", "high_52w", 2], ["Min 52s", "low_52w", 2],
    ["Supporto", "support", 2], ["Resistenza", "resistance", 2],
  ];
  const pctKeys = new Set(["mom_1m", "mom_3m", "mom_6m", "mom_12m", "vol20", "rs_3m"]);
  return (
    <Card>
      <CardHeader title={`${it.ticker} · ${it.name ?? ""}`} subtitle={<>Analisi del {when(it.analysis_time)} · prezzo {num(it.price)} {it.currency} al {it.price_date} · fonte {it.price_source} · copertura dati {num(Number(it.coverage) * 100, 0)}%</>}
        right={it.is_demo ? <DemoBadge /> : null} />
      <div className="grid gap-4 p-4 lg:grid-cols-3">
        <div>
          <h3 className="mb-2 text-xs font-semibold uppercase tracking-wider text-muted">Fattori (peso)</h3>
          <ul className="space-y-2">
            {Object.entries(FACTOR_LABELS).map(([k, l]) => (
              <li key={k} className="flex items-center justify-between gap-2 text-xs">
                <span>{l} <span className="text-muted">({weights[k]}%)</span></span><ScoreBar v={it.factor_scores[k]} />
              </li>
            ))}
          </ul>
          <p className="mt-3 text-[11px] text-muted">I fattori senza dati affidabili non vengono stimati e il loro peso è redistribuito. Il sentiment non ha ancora una fonte affidabile integrata.</p>
        </div>
        <div>
          <h3 className="mb-2 text-xs font-semibold uppercase tracking-wider text-muted">Indicatori</h3>
          <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs">
            {show.map(([l, k, dg]) => (
              <div key={k} className="flex justify-between"><dt className="text-muted">{l}</dt>
                <dd className="num">{ft[k] === null || ft[k] === undefined ? "—" : num(pctKeys.has(k) ? Number(ft[k]) * 100 : ft[k], dg)}</dd></div>
            ))}
          </dl>
        </div>
        <div className="space-y-3 text-xs">
          <div><h3 className="mb-1 font-semibold text-up">A favore</h3><ul className="space-y-0.5">{it.rationale.for.map((r) => <li key={r}>• {r}</li>)}{!it.rationale.for.length && <li className="text-muted">—</li>}</ul></div>
          <div><h3 className="mb-1 font-semibold text-down">Contro</h3><ul className="space-y-0.5">{it.rationale.against.map((r) => <li key={r}>• {r}</li>)}</ul></div>
          <div><h3 className="mb-1 font-semibold text-warn">Potrebbe essere sbagliato se</h3><ul className="space-y-0.5">{it.rationale.could_be_wrong_if.map((r) => <li key={r}>• {r}</li>)}</ul></div>
          {(it.exclusions.length > 0 || it.flags.length > 0) && (
            <div><h3 className="mb-1 font-semibold text-muted">Esclusioni e avvisi</h3><ul className="space-y-0.5">{[...it.exclusions, ...it.flags].map((r) => <li key={r}>• {r}</li>)}</ul></div>
          )}
        </div>
      </div>
    </Card>
  );
}
