"use client";
import type { Order, Page, Portfolio, Position } from "@/lib/api";
import { useApi } from "@/lib/hooks";
import { cn, money, num, when } from "@/lib/format";
import { Badge, Card, CardHeader, DemoBadge, Empty, Table } from "@/components/ui/primitives";

const LABELS: Record<string, string> = {
  risk_per_trade_pct: "Rischio per operazione",
  max_aggregate_risk_pct: "Rischio aggregato massimo",
  max_position_exposure_pct: "Esposizione massima per titolo",
  max_sector_exposure_pct: "Esposizione massima per settore",
  min_cash_reserve_pct: "Riserva di liquidità",
  max_daily_loss_pct: "Perdita giornaliera massima",
  max_total_drawdown_pct: "Drawdown massimo",
};

export default function PortfolioPage() {
  const pf = useApi<Portfolio>("/api/portfolio");
  const pos = useApi<Page<Position>>("/api/positions?page_size=200");
  const orders = useApi<Page<Order>>("/api/paper/orders?page_size=50");
  const p = pf.data;
  const equity = Number(p?.equity ?? 0);
  const bySector = new Map<string, number>();
  pos.data?.items.forEach((x) => bySector.set(x.sector ?? "Non classificato", (bySector.get(x.sector ?? "Non classificato") ?? 0) + Number(x.market_value_base)));
  const sectorLimit = Number(p?.risk_params?.max_sector_exposure_pct ?? 30);

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-semibold">Portfolio Control</h1>
      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader title="Esposizione per settore" subtitle={`Limite per settore: ${sectorLimit}% dell'equity`} />
          {bySector.size === 0 ? <Empty title="Nessuna esposizione" /> : (
            <div className="space-y-3 p-4">
              {[...bySector.entries()].sort((a, b) => b[1] - a[1]).map(([s, v]) => {
                const share = equity ? (v / equity) * 100 : 0;
                return (
                  <div key={s}>
                    <div className="mb-1 flex justify-between text-xs"><span>{s}</span><span className="num">{num(share)}%</span></div>
                    <div className="relative h-2 rounded bg-raised">
                      <div className={cn("h-2 rounded", share > sectorLimit ? "bg-down" : "bg-accent")} style={{ width: `${Math.min(share, 100)}%` }} />
                      <div className="absolute top-[-3px] h-[14px] w-px bg-warn" style={{ left: `${sectorLimit}%` }} title="limite" />
                    </div>
                  </div>
                );
              })}
              <p className="text-[11px] text-muted">Liquidità: {money(p?.cash)} ({num(equity ? (Number(p?.cash) / equity) * 100 : 0)}%)</p>
            </div>
          )}
        </Card>
        <Card>
          <CardHeader title="Parametri di rischio" subtitle="Valori iniziali da sottoporre a test, non ottimizzati" />
          <dl className="divide-y divide-line text-sm">
            {p && Object.entries(p.risk_params).map(([k, v]) => (
              <div key={k} className="flex justify-between px-4 py-2"><dt className="text-muted">{LABELS[k] ?? k}</dt><dd className="num">{v}%</dd></div>
            ))}
          </dl>
          <p className="border-t border-line px-4 py-2 text-[11px] text-muted">Leva e vendite allo scoperto disabilitate. Correlazioni e distribuzione dei risultati: Fase 3 e 6.</p>
        </Card>
      </div>

      <Card>
        <CardHeader title="Ordini simulati" subtitle="Le notifiche non equivalgono a ordini eseguiti; qui sono elencati solo gli ordini del PaperBroker" />
        {orders.data?.items.length === 0 ? <Empty title="Nessun ordine" /> : (
          <Table head={["#", "Data", "Strumento", "Lato", "Tipo", "Qtà", "Eseguita", "Limite", "Stato", ""]}>
            {orders.data?.items.map((o) => (
              <tr key={o.id}>
                <td className="num px-4 py-2 text-muted">{o.id}</td>
                <td className="px-4 py-2 text-xs">{when(o.created_at)}</td>
                <td className="px-4 py-2">{o.instrument_id}</td>
                <td className={cn("px-4 py-2", o.side === "BUY" ? "text-up" : "text-down")}>{o.side}</td>
                <td className="px-4 py-2">{o.order_type}</td>
                <td className="num px-4 py-2">{num(o.quantity, 0)}</td>
                <td className="num px-4 py-2">{num(o.filled_quantity, 0)}</td>
                <td className="num px-4 py-2">{num(o.limit_price)}</td>
                <td className="px-4 py-2"><Badge tone={o.status === "FILLED" ? "up" : o.status === "REJECTED" ? "down" : "warn"}>{o.status}</Badge>
                  {o.reject_reason && <p className="mt-0.5 text-[11px] text-down">{o.reject_reason}</p>}</td>
                <td className="px-4 py-2">{o.is_demo && <DemoBadge />}</td>
              </tr>
            ))}
          </Table>
        )}
      </Card>
    </div>
  );
}
