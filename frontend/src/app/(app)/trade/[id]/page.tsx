"use client";
import { use, useMemo, useState } from "react";
import Link from "next/link";
import { api, type Bar, type Order, type TradePlan } from "@/lib/api";
import { useApi } from "@/lib/hooks";
import { cn, money, num, pct, when } from "@/lib/format";
import { Badge, Button, Card, CardHeader, DemoBadge, ErrorNote } from "@/components/ui/primitives";
import { Candles, type PriceLine } from "@/components/candles";
import { useSession } from "@/components/shell";

const STATUS_TONE = { READY: "up", WAIT: "warn", REJECTED: "down", EXPIRED: "neutral" } as const;
const STATUS_LABEL = { READY: "Pronto", WAIT: "In attesa", REJECTED: "Respinto", EXPIRED: "Scaduto" };

function List({ title, items, tone }: { title: string; items: string[]; tone?: string }) {
  if (!items?.length) return null;
  return (
    <div>
      <h3 className={cn("mb-1 text-xs font-semibold", tone)}>{title}</h3>
      <ul className="space-y-0.5 text-xs">{items.map((i) => <li key={i}>• {i}</li>)}</ul>
    </div>
  );
}

export default function TradeDetail({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const { user, status } = useSession();
  const plan = useApi<TradePlan>(`/api/opportunities/${id}`);
  const p = plan.data;
  const bars = useApi<Bar[]>(p ? `/api/instruments/${p.instrument_id}/bars` : null);
  const [order, setOrder] = useState<Order | null>(null);
  const [err, setErr] = useState<unknown>(null);

  const hasLevels = !!p && p.stop_loss !== p.entry_price;
  const lines = useMemo<PriceLine[]>(() => !p || p.stop_loss === p.entry_price ? [] : [
    { price: Number(p.entry_price), label: "Ingresso", color: "#4f9dff" },
    { price: Number(p.stop_loss), label: "Stop", color: "#f0535c" },
    { price: Number(p.take_profit_1), label: "TP1", color: "#26c485" },
    ...(p.take_profit_2 ? [{ price: Number(p.take_profit_2), label: "TP2", color: "#26c485" }] : []),
  ], [p]);

  if (plan.error) return <ErrorNote error={plan.error} />;
  if (!p) return <p className="text-sm text-muted">Caricamento…</p>;
  const ccy = p.currency ?? "";
  const sr = p.stop_rationale as Record<string, string | number | null>;

  async function execute() {
    setErr(null);
    try {
      const r = await api<{ order: Order }>("/api/paper/orders", { method: "POST", body: JSON.stringify({
        instrument_id: p!.instrument_id, side: "BUY", order_type: "LIMIT", quantity: p!.quantity,
        limit_price: p!.entry_high, trade_plan_id: p!.id, client_order_id: `plan-${p!.id}`,
      }) });
      setOrder(r.order);
    } catch (e) { setErr(e); }
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <Link href="/radar" className="text-xs text-accent hover:underline">← Opportunity Radar</Link>
          <h1 className="mt-1 text-lg font-semibold">{p.ticker} · {p.name}</h1>
          <p className="text-xs text-muted">
            Piano #{p.id} · {p.direction} · {p.setup} · {p.strategy_style} · orizzonte {p.horizon_days} giorni · ISIN {p.isin ?? "n/d"} · {p.model_version}
          </p>
        </div>
        <div className="flex items-center gap-2">
          {p.is_demo && <DemoBadge />}
          <Badge tone={STATUS_TONE[p.status]} className="text-xs">{STATUS_LABEL[p.status]}</Badge>
        </div>
      </div>
      {p.status_reason && <p className="rounded-md border border-line bg-panel px-3 py-2 text-xs text-muted">{p.status_reason}</p>}

      <div className="grid gap-4 xl:grid-cols-[1fr_360px]">
        <Card className="p-2">{bars.data?.length ? <Candles bars={bars.data} lines={lines} /> : null}</Card>
        {!hasLevels ? (
          <Card className="p-4 text-sm">
            <p className="font-medium">Nessun livello operativo</p>
            <p className="mt-1 text-xs text-muted">Il Trade Planner non ha prodotto ingresso, stop e target per questo titolo: {p.status_reason}</p>
          </Card>
        ) : (
        <Card>
          <CardHeader title="Piano operativo" subtitle={`Prezzo di riferimento ${num(p.price_reference)} ${ccy} · ${p.price_source} · ${when(p.price_time)}`} />
          <dl className="divide-y divide-line text-sm">
            {[
              ["Ingresso (limite)", `${num(p.entry_price)} ${ccy}`],
              ["Intervallo ammesso", `${num(p.entry_low)} – ${num(p.entry_high)}`],
              ["Stop loss", `${num(p.stop_loss)}`],
              ["Take profit 1", `${num(p.take_profit_1)} (chiude ${num(Number(p.tp1_close_fraction) * 100, 0)}%)`],
              ["Take profit 2", num(p.take_profit_2)],
              ["Quantità", num(p.quantity, 0)],
              ["Importo complessivo", money(p.notional_base)],
              ["Perdita stimata allo stop", money(p.est_loss_at_stop_base)],
              ["Guadagno stimato TP1 / TP2", `${money(p.est_gain_tp1_base)} / ${money(p.est_gain_tp2_base)}`],
              ["Rendimento TP1 lordo / netto", `${pct(p.gross_return_tp1_pct)} / ${pct(p.net_return_tp1_pct)}`],
              ["R/R netto (uscite parziali)", num(p.reward_risk_net)],
              ["Probabilità calibrata", p.calibrated_probability ?? "non disponibile"],
              ["Expected value", p.expected_value_base ?? "non disponibile (modello non validato)"],
              ["Valido fino al", when(p.valid_until)],
            ].map(([k, v]) => (
              <div key={k} className="flex justify-between gap-3 px-4 py-1.5"><dt className="text-xs text-muted">{k}</dt><dd className="num text-right text-xs">{v}</dd></div>
            ))}
          </dl>
          {user.role !== "viewer" && (
            <div className="space-y-2 border-t border-line p-4">
              <Button className="w-full" disabled={p.status !== "READY" || status?.mode !== "PAPER"} onClick={execute}>
                Esegui ordine simulato
              </Button>
              <p className="text-[11px] text-muted">
                {p.status !== "READY" ? "Eseguibile solo quando il piano è READY." : status?.mode !== "PAPER" ? "Attiva Paper Trading in Sistema." : "Ordine LIMIT al massimo dell'intervallo, con lo stop del piano."}
              </p>
              {order && <p className="text-xs">Ordine #{order.id}: {order.status}{order.reject_reason ? ` — ${order.reject_reason}` : ""}</p>}
              <ErrorNote error={err} />
            </div>
          )}
        </Card>
        )}
      </div>

      {hasLevels && <div className="grid gap-4 lg:grid-cols-3">
        <Card>
          <CardHeader title="Technical Analyst" subtitle="Ingresso, invalidazione e obiettivi" />
          <div className="space-y-3 p-4">
            <p className="text-xs">Stop sul livello <b>{String(sr.method)}</b> a {num(sr.technical_level as number)} con buffer di {String(sr.buffer_atr)} ATR (ATR {num(sr.atr as number)}): distanza {String(sr.distance_atr)} ATR. TP1 basato su: {String(sr.tp1_basis)}.</p>
            {sr.note ? <p className="text-xs text-warn">{String(sr.note)}</p> : null}
            <List title="Condizioni di ingresso" items={p.entry_conditions} />
            <List title="Evitare l'ingresso se" items={p.avoid_conditions} tone="text-warn" />
            <List title="Uscita anticipata" items={p.early_exit_conditions} />
            {p.trailing_stop && <p className="text-xs text-muted">Trailing: dopo {p.trailing_stop.after} stop a {p.trailing_stop.move_stop_to}, poi {p.trailing_stop.then}.</p>}
          </div>
        </Card>
        <Card>
          <CardHeader title="Risk e Portfolio Manager" subtitle="Dimensionamento, correlazioni, scenari" />
          <div className="space-y-3 p-4">
            <table className="w-full text-xs">
              <tbody className="divide-y divide-line">
                {p.scenarios.map((s) => (
                  <tr key={s.name}><td className="py-1 pr-2">{s.name}</td><td className="num py-1 text-right">{num(s.exit)}</td>
                    <td className={cn("num py-1 text-right", Number(s.pnl_base) >= 0 ? "text-up" : "text-down")}>{money(s.pnl_base)}</td></tr>
                ))}
              </tbody>
            </table>
            <p className="text-[11px] text-muted">Le perdite sono stime: con gap o sospensioni l&apos;uscita può avvenire oltre lo stop.</p>
            <div className="text-xs">
              <p className="font-semibold">Correlazioni (60 giorni) con le posizioni aperte</p>
              {p.correlations.values && Object.keys(p.correlations.values).length
                ? Object.entries(p.correlations.values).map(([k, v]) => <p key={k}>{k}: {v === null ? "n/d" : num(v, 2)}</p>)
                : <p className="text-muted">Nessuna posizione aperta.</p>}
              {p.correlations.notes?.map((n) => <p key={n} className="text-warn">{n}</p>)}
            </div>
          </div>
        </Card>
        <Card>
          <CardHeader title="Tesi e invalidazione" subtitle={`Regime ${p.market_regime ?? "n/d"} · punteggio ${num(p.score, 0)} · rischio ${p.risk_level ?? "n/d"}`} />
          <div className="space-y-3 p-4">
            <List title="A favore" items={p.rationale_for} tone="text-up" />
            <List title="Contro" items={p.rationale_against} tone="text-down" />
            <List title="Il piano è invalidato se" items={p.invalidation_conditions} tone="text-warn" />
          </div>
        </Card>
      </div>}
    </div>
  );
}
