"use client";
import { useMemo, useState } from "react";
import { RefreshCw, Search } from "lucide-react";
import { api, type Bar, type Instrument, type Order, type Page, type Quote, type RiskCheck, type SyncResult } from "@/lib/api";
import { useApi } from "@/lib/hooks";
import { cn, money, num, when } from "@/lib/format";
import { Badge, Button, Card, CardHeader, DemoBadge, Empty, ErrorNote, Input, Label } from "@/components/ui/primitives";
import { Candles, type PriceLine } from "@/components/candles";
import { useSession } from "@/components/shell";

export default function MarketPage() {
  const { user, status } = useSession();
  const canWrite = user.role !== "viewer";
  const [q, setQ] = useState("");
  const list = useApi<Page<Instrument>>(`/api/instruments?page_size=100${q ? `&q=${encodeURIComponent(q)}` : ""}`);
  const [selected, setSelected] = useState<Instrument | null>(null);
  const [tickers, setTickers] = useState("");
  const [sync, setSync] = useState<SyncResult[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<unknown>(null);

  async function runSync(benchmarks: boolean) {
    setBusy(true); setErr(null);
    try {
      const r = benchmarks
        ? await api<SyncResult[]>("/api/market/sync-benchmarks", { method: "POST" })
        : await api<SyncResult[]>("/api/market/sync", { method: "POST", body: JSON.stringify({ tickers: tickers.split(/[\s,;]+/).filter(Boolean), years: 3 }) });
      setSync(r); list.reload();
    } catch (e) { setErr(e); } finally { setBusy(false); }
  }

  return (
    <div className="grid gap-4 xl:grid-cols-[320px_1fr]">
      <div className="space-y-4">
        {canWrite && (
          <Card>
            <CardHeader title="Acquisizione dati" subtitle={status?.market_data.name} />
            <div className="space-y-2 p-4">
              <Label hint="formato SIMBOLO.BORSA, es. AAPL.US ENI.MI SAP.DE">Ticker</Label>
              <Input value={tickers} onChange={(e) => setTickers(e.target.value)}
                placeholder={status?.market_data.is_demo ? "DEMO1.DEMO DEMO2.DEMO" : "AAPL.US ENI.MI"} />
              <div className="flex gap-2">
                <Button disabled={busy || !tickers.trim()} onClick={() => runSync(false)}>
                  <RefreshCw className={cn("h-3.5 w-3.5", busy && "animate-spin")} /> Scarica
                </Button>
                <Button variant="outline" disabled={busy} onClick={() => runSync(true)}>Benchmark</Button>
              </div>
              <ErrorNote error={err} />
              {sync && (
                <ul className="space-y-1 text-xs">
                  {sync.map((r) => (
                    <li key={r.ticker} className={r.ok ? "text-up" : "text-down"}>
                      {r.ticker}: {r.ok ? `${r.bars_added} barre nuove · ${r.source}` : r.error}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </Card>
        )}
        <Card>
          <div className="relative border-b border-line p-2">
            <Search className="absolute left-4 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" />
            <Input className="pl-7" placeholder="Cerca simbolo, nome, ISIN" value={q} onChange={(e) => setQ(e.target.value)} />
          </div>
          {list.data?.items.length === 0 && <Empty title="Nessuno strumento" body="Scarica i dati di almeno un ticker." />}
          <ul className="max-h-[60vh] divide-y divide-line overflow-y-auto">
            {list.data?.items.map((i) => (
              <li key={i.id}>
                <button onClick={() => setSelected(i)}
                  className={cn("flex w-full items-center gap-2 px-4 py-2 text-left hover:bg-raised", selected?.id === i.id && "bg-raised")}>
                  <div className="min-w-0 flex-1">
                    <p className="text-sm font-medium">{i.symbol}.{i.exchange}</p>
                    <p className="truncate text-[11px] text-muted">{i.name}</p>
                  </div>
                  <span className="text-[11px] text-muted">{i.currency}</span>
                  {i.is_demo && <DemoBadge />}
                </button>
              </li>
            ))}
          </ul>
        </Card>
      </div>
      {selected ? <InstrumentDetail key={selected.id} inst={selected} /> : (
        <Card><Empty title="Seleziona uno strumento" body="Grafico a candele, ultima quotazione verificata, calcolo della size e ordini simulati." /></Card>
      )}
    </div>
  );
}

function InstrumentDetail({ inst }: { inst: Instrument }) {
  const { user, status } = useSession();
  const bars = useApi<Bar[]>(`/api/instruments/${inst.id}/bars`);
  const quote = useApi<Quote | null>(`/api/instruments/${inst.id}/quote`);
  const [f, setF] = useState({ entry: "", stop: "", tp1: "", tp2: "" });
  const [risk, setRisk] = useState<RiskCheck | null>(null);
  const [order, setOrder] = useState<Order | null>(null);
  const [err, setErr] = useState<unknown>(null);

  const lines = useMemo<PriceLine[]>(() => {
    const l: PriceLine[] = [];
    if (Number(f.entry)) l.push({ price: Number(f.entry), label: "Ingresso", color: "#4f9dff" });
    if (Number(f.stop)) l.push({ price: Number(f.stop), label: "Stop", color: "#f0535c" });
    if (Number(f.tp1)) l.push({ price: Number(f.tp1), label: "TP1", color: "#26c485" });
    if (Number(f.tp2)) l.push({ price: Number(f.tp2), label: "TP2", color: "#26c485" });
    return l;
  }, [f]);

  async function check() {
    setErr(null); setOrder(null);
    try {
      setRisk(await api<RiskCheck>("/api/risk/check", { method: "POST", body: JSON.stringify({
        instrument_id: inst.id, entry_price: f.entry, stop_price: f.stop,
        take_profit_1: f.tp1 || null, take_profit_2: f.tp2 || null,
      }) }));
    } catch (e) { setErr(e); setRisk(null); }
  }

  async function placeOrder() {
    if (!risk) return;
    setErr(null);
    try {
      const r = await api<{ order: Order }>("/api/paper/orders", { method: "POST", body: JSON.stringify({
        instrument_id: inst.id, side: "BUY", order_type: "LIMIT", quantity: risk.result.quantity, limit_price: f.entry,
        stop_loss: f.stop, take_profit_1: f.tp1 || null, client_order_id: `ui-${risk.assessment_id}-${inst.id}`,
      }) });
      setOrder(r.order);
    } catch (e) { setErr(e); }
  }

  const qd = quote.data;
  const r = risk?.result;
  return (
    <div className="space-y-4">
      <Card>
        <CardHeader
          title={`${inst.symbol}.${inst.exchange} · ${inst.name ?? ""}`}
          subtitle={<>{inst.instrument_type} · {inst.currency} · {inst.sector ?? "settore n/d"} · ISIN {inst.isin ?? "n/d"}</>}
          right={inst.is_demo ? <DemoBadge /> : <Badge>{inst.data_source}</Badge>} />
        <div className="grid grid-cols-2 gap-4 border-b border-line px-4 py-3 text-xs sm:grid-cols-4">
          <div><p className="text-muted">Ultimo prezzo</p><p className="num text-base font-semibold">{num(qd?.price)} <span className="text-xs text-muted">{qd?.currency}</span></p></div>
          <div><p className="text-muted">Bid / Ask</p><p className="num">{num(qd?.bid)} / {num(qd?.ask)}</p></div>
          <div><p className="text-muted">Orario quotazione</p><p>{when(qd?.quote_time)}</p></div>
          <div><p className="text-muted">Fonte</p><p>{qd?.source ?? "—"}{qd?.delay_minutes ? ` · ritardo ~${qd.delay_minutes} min` : ""}</p></div>
        </div>
        <div className="p-2">
          {bars.data && bars.data.length > 0 ? <Candles bars={bars.data} lines={lines} /> : <Empty title="Nessuno storico disponibile" />}
        </div>
      </Card>

      <Card>
        <CardHeader title="Dimensionamento e controllo del rischio"
          subtitle="Scegli prima lo stop su base tecnica: il motore calcola la quantità compatibile con i limiti del portafoglio." />
        <div className="grid gap-4 p-4 lg:grid-cols-[260px_1fr]">
          <div className="space-y-2">
            {(["entry", "stop", "tp1", "tp2"] as const).map((k) => (
              <div key={k}>
                <Label>{{ entry: "Prezzo di ingresso", stop: "Stop loss", tp1: "Take profit 1", tp2: "Take profit 2 (opz.)" }[k]}</Label>
                <Input inputMode="decimal" value={f[k]} onChange={(e) => setF({ ...f, [k]: e.target.value.replace(",", ".") })} />
              </div>
            ))}
            <Button className="w-full" disabled={!f.entry || !f.stop} onClick={check}>Calcola</Button>
          </div>
          <div className="space-y-3">
            <ErrorNote error={err} />
            {r ? (
              <>
                <div className="flex items-center gap-2">
                  <Badge tone={risk!.approved ? "up" : "down"}>{risk!.approved ? "Entro i limiti" : "Bloccato dal Risk Manager"}</Badge>
                  <span className="text-xs text-muted">vincolo determinante: {String(r.binding_constraint)}</span>
                </div>
                <dl className="grid grid-cols-2 gap-x-6 gap-y-2 text-sm sm:grid-cols-3">
                  {[
                    ["Quantità", num(r.quantity as string, 0)],
                    ["Controvalore", money(r.notional_base as string)],
                    ["Risk budget", money(r.risk_budget_base as string)],
                    ["Perdita stimata allo stop", money(r.est_loss_at_stop_base as string)],
                    ["Scenario gap (stress)", money(r.est_loss_gap_scenario_base as string)],
                    ["Guadagno stimato TP1", money(r.est_gain_tp1_base as string)],
                    ["Guadagno stimato TP2", money(r.est_gain_tp2_base as string)],
                    ["R/R netto", num(r.reward_risk_net as string)],
                    ["Esposizione", `${num(r.exposure_pct as string)}%`],
                  ].map(([k, v]) => (
                    <div key={k}><dt className="text-[11px] text-muted">{k}</dt><dd className="num">{v}</dd></div>
                  ))}
                </dl>
                <p className="text-[11px] text-muted">Cambio: {String(risk!.inputs.fx_rate_used)} ({String(risk!.inputs.fx_source)}) · Probabilità di successo: non disponibile (modello non validato)</p>
                <ul className="space-y-0.5 text-[11px] text-warn">
                  {(r.warnings as string[]).concat((r.blocking_reasons as string[]) ?? []).map((w) => <li key={w}>• {w}</li>)}
                </ul>
                {user.role !== "viewer" && risk!.approved && (
                  <div className="flex items-center gap-3 border-t border-line pt-3">
                    <Button disabled={status?.mode !== "PAPER"} onClick={placeOrder}>Ordine simulato LIMIT BUY</Button>
                    {status?.mode !== "PAPER" && <span className="text-xs text-muted">Attiva Paper Trading Mode in Sistema.</span>}
                    {order && (
                      <span className="text-xs">Ordine #{order.id}: <span className={order.status === "FILLED" ? "text-up" : order.status === "REJECTED" ? "text-down" : "text-warn"}>{order.status}</span>
                        {order.reject_reason && ` — ${order.reject_reason}`}</span>
                    )}
                  </div>
                )}
              </>
            ) : <p className="text-sm text-muted">Inserisci ingresso e stop per calcolare la size.</p>}
          </div>
        </div>
      </Card>
    </div>
  );
}
