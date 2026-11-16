"use client";
import Link from "next/link";
import type { Alert, MarketOverview, Page, Portfolio, Position, Ranking } from "@/lib/api";
import { useApi } from "@/lib/hooks";
import { cn, money, num, pct, tone, when } from "@/lib/format";
import { Badge, Card, CardHeader, DemoBadge, Empty, ErrorNote, Table } from "@/components/ui/primitives";

function Kpi({ label, value, sub, className }: { label: string; value: React.ReactNode; sub?: React.ReactNode; className?: string }) {
  return (
    <Card className="px-4 py-3">
      <p className="text-[11px] uppercase tracking-wider text-muted">{label}</p>
      <p className={cn("num mt-1 text-xl font-semibold", className)}>{value}</p>
      {sub && <p className="mt-0.5 text-[11px] text-muted">{sub}</p>}
    </Card>
  );
}

export default function Dashboard() {
  const pf = useApi<Portfolio>("/api/portfolio", 30_000);
  const pos = useApi<Page<Position>>("/api/positions?page_size=10", 30_000);
  const ov = useApi<MarketOverview>("/api/market/overview", 60_000);
  const al = useApi<Page<Alert>>("/api/alerts?page_size=8", 30_000);
  const rk = useApi<Ranking>("/api/rankings/latest?page_size=5");
  const p = pf.data;
  const ccy = p?.base_currency ?? "EUR";

  return (
    <div className="space-y-4">
      <div className="flex items-end justify-between">
        <div>
          <h1 className="text-lg font-semibold">Dashboard</h1>
          <p className="text-xs text-muted">Portafoglio operativo simulato · valutato {when(p?.valued_at)}</p>
        </div>
      </div>
      <ErrorNote error={pf.error} />

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4 xl:grid-cols-8">
        <Kpi label="Equity" value={money(p?.equity, ccy)} sub={`Capitale iniziale ${money(p?.initial_capital, ccy)}`} />
        <Kpi label="Liquidità" value={money(p?.cash, ccy)} />
        <Kpi label="Rend. giornaliero" value="n/d" sub="Richiede storico equity (Fase 6)" />
        <Kpi label="Rend. cumulato" value={pct(p?.cumulative_return_pct)} className={tone(p?.cumulative_return_pct)} />
        <Kpi label="P&L realizzato" value={money(p?.realized_pnl, ccy)} className={tone(p?.realized_pnl)} />
        <Kpi label="P&L non realizzato" value={money(p?.unrealized_pnl, ccy)} className={tone(p?.unrealized_pnl)} />
        <Kpi label="Drawdown" value={`${num(p?.drawdown_pct)}%`} className={Number(p?.drawdown_pct) > 0 ? "text-down" : ""} />
        <Kpi label="Rischio aggregato" value={`${num(p?.aggregate_risk_pct)}%`}
          sub={`limite ${p?.risk_params?.max_aggregate_risk_pct ?? "—"}%`} />
      </div>

      {p?.warnings?.length ? (
        <div className="space-y-1 rounded-md border border-warn/40 bg-warn/5 px-3 py-2 text-xs text-warn">
          {p.warnings.map((w) => <p key={w}>{w}</p>)}
        </div>
      ) : null}

      <div className="grid gap-4 xl:grid-cols-3">
        <Card className="xl:col-span-2">
          <CardHeader title="Posizioni aperte" subtitle="Valori in valuta base; prezzo, fonte e orario dell'ultima quotazione" />
          {pos.data && pos.data.items.length === 0 ? (
            <Empty title="Nessuna posizione aperta" body="Gli ordini simulati si inseriscono dalla sezione Mercati e dati in Paper Trading Mode." />
          ) : (
            <Table head={["Titolo", "Qtà", "Prezzo medio", "Ultimo", "Valore", "P&L", "Stop", "Rischio a stop", ""]}>
              {pos.data?.items.map((x) => (
                <tr key={x.id} className="hover:bg-raised/50">
                  <td className="px-4 py-2"><p className="font-medium">{x.ticker}</p><p className="text-[11px] text-muted">{x.name}</p></td>
                  <td className="num px-4 py-2">{num(x.quantity, 0)}</td>
                  <td className="num px-4 py-2">{num(x.avg_price)} <span className="text-muted">{x.currency}</span></td>
                  <td className="num px-4 py-2" title={`${x.price_source ?? ""} ${when(x.price_time)}`}>
                    {num(x.last_price)}{x.stale && <span className="ml-1 text-warn" title="Quotazione obsoleta">●</span>}
                  </td>
                  <td className="num px-4 py-2">{money(x.market_value_base, ccy)}</td>
                  <td className={cn("num px-4 py-2", tone(x.unrealized_pnl_base))}>{money(x.unrealized_pnl_base, ccy)}</td>
                  <td className="num px-4 py-2">{num(x.stop_loss)}</td>
                  <td className="num px-4 py-2">{x.risk_to_stop_base ? money(x.risk_to_stop_base, ccy) : <span className="text-warn">senza stop</span>}</td>
                  <td className="px-4 py-2">{x.is_demo && <DemoBadge />}</td>
                </tr>
              ))}
            </Table>
          )}
        </Card>

        <Card>
          <CardHeader title="Alert attivi" />
          {al.data && al.data.items.length === 0 ? <Empty title="Nessun alert attivo" /> : (
            <ul className="divide-y divide-line">
              {al.data?.items.map((a) => (
                <li key={a.id} className="px-4 py-2.5 text-xs">
                  <div className="flex items-center gap-2">
                    <Badge tone={a.severity === "CRITICAL" ? "down" : a.severity === "WARNING" ? "warn" : "neutral"}>{a.severity}</Badge>
                    <span className="text-muted">{a.kind}</span>
                    <span className="ml-auto text-muted">{when(a.created_at)}</span>
                  </div>
                  <p className="mt-1">{a.message}</p>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>

      <div className="grid gap-4 xl:grid-cols-3">
        <Card className="xl:col-span-2">
          <CardHeader title="Mercati di riferimento"
            subtitle={ov.data ? <>Fonte: {ov.data.provider.name} · {ov.data.provider.license_note}</> : null}
            right={<Link href="/market" className="text-xs text-accent hover:underline">Aggiorna dati →</Link>} />
          <Table head={["Strumento", "Prezzo", "Var. %", "Orario quotazione", "Stato"]}>
            {ov.data?.items.map((i) => (
              <tr key={i.ticker}>
                <td className="px-4 py-2"><p className="font-medium">{i.ticker}</p><p className="text-[11px] text-muted">{i.name ?? "—"}</p></td>
                <td className="num px-4 py-2">{num(i.price)}</td>
                <td className={cn("num px-4 py-2", tone(i.change_pct))}>{pct(i.change_pct)}</td>
                <td className="px-4 py-2 text-xs text-muted">{when(i.quote_time)}</td>
                <td className="px-4 py-2">
                  <div className="flex gap-1">
                    {i.status === "OK" && <Badge tone="up">Aggiornato</Badge>}
                    {i.status === "STALE" && <Badge tone="warn">Obsoleto</Badge>}
                    {i.status === "NO_DATA" && <Badge>Nessun dato</Badge>}
                    {i.is_demo && i.status !== "NO_DATA" && <DemoBadge />}
                  </div>
                </td>
              </tr>
            ))}
          </Table>
          {ov.data && (
            <p className="border-t border-line px-4 py-2 text-[11px] text-muted">
              Regime di mercato: {ov.data.regime.status === "OK"
                ? <span className="text-fg">{String((ov.data.regime as Record<string, unknown>).regime)} (dati al {String((ov.data.regime as Record<string, unknown>).as_of)})</span>
                : <><span className="text-fg">non disponibile</span> — {ov.data.regime.reason}</>}
            </p>
          )}
        </Card>

        <Card>
          <CardHeader title="Migliori candidati" subtitle={rk.data?.scan ? `Scansione #${rk.data.scan.id} · dati al ${rk.data.scan.as_of}` : undefined}
            right={<Link href="/radar" className="text-xs text-accent hover:underline">Radar →</Link>} />
          {rk.data && rk.data.items.length === 0 ? (
            <Empty title="Nessun candidato" body="Avvia una scansione dall'Opportunity Radar. Nessuna opportunità viene inventata." />
          ) : (
            <ul className="divide-y divide-line">
              {rk.data?.items.map((it, i) => (
                <li key={it.id} className="flex items-center gap-3 px-4 py-2.5">
                  <span className="num w-4 text-xs text-muted">{i + 1}</span>
                  <div className="min-w-0 flex-1">
                    <p className="text-sm font-medium">{it.ticker}</p>
                    <p className="truncate text-[11px] text-muted">{it.setup ?? "nessun setup"} · {it.horizon ?? "—"}</p>
                  </div>
                  <span className="num text-sm font-semibold">{num(it.score, 0)}</span>
                  {it.is_demo && <DemoBadge />}
                </li>
              ))}
            </ul>
          )}
          <p className="border-t border-line px-4 py-2 text-[11px] text-muted">Punteggio di ranking, non raccomandazione: i piani operativi arrivano in Fase 3.</p>
        </Card>
      </div>
    </div>
  );
}
