"use client";
import { Fragment, useState } from "react";
import {
  ALERT_KINDS, api, type Alert, type AlertSummary, type EquitySnapshot, type MonitorReport, type Order, type Page,
  type Portfolio, type Position, type Reconciliation,
} from "@/lib/api";
import { useApi } from "@/lib/hooks";
import { cn, money, num, when } from "@/lib/format";
import { Badge, Button, Card, CardHeader, DemoBadge, Empty, ErrorNote, Table } from "@/components/ui/primitives";
import { Lines } from "@/components/lines";
import { useSession } from "@/components/shell";

const LABELS: Record<string, string> = {
  risk_per_trade_pct: "Rischio per operazione",
  max_aggregate_risk_pct: "Rischio aggregato massimo",
  max_position_exposure_pct: "Esposizione massima per titolo",
  max_sector_exposure_pct: "Esposizione massima per settore",
  min_cash_reserve_pct: "Riserva di liquidità",
  max_daily_loss_pct: "Perdita giornaliera massima",
  max_total_drawdown_pct: "Drawdown massimo",
};
const PURPOSE: Record<string, string> = {
  ENTRY: "Ingresso da piano", MANUAL: "Manuale", STOP: "Stop loss", TRAIL: "Trailing stop", TP1: "Take profit 1", TP2: "Take profit 2",
};
const OPEN = ["NEW", "OPEN", "PARTIALLY_FILLED"];

function statusTone(s: string) {
  return s === "FILLED" ? "up" : s === "REJECTED" ? "down" : s === "CANCELLED" ? "neutral" : "warn";
}

function StopEditor({ p, onDone }: { p: Position; onDone: () => void }) {
  const [stop, setStop] = useState(p.stop_loss != null ? String(Number(p.stop_loss)) : "");
  const [reason, setReason] = useState("");
  const [err, setErr] = useState<unknown>(null);
  const wider = p.stop_loss != null && Number(stop) < Number(p.stop_loss);
  const save = async () => {
    setErr(null);
    try {
      await api(`/api/positions/${p.id}/levels`, { method: "PATCH", body: JSON.stringify({
        stop_loss: stop, ...(wider ? { authorize_wider_stop: true, reason } : {}) }) });
      onDone();
    } catch (e) { setErr(e); }
  };
  return (
    <div className="space-y-2 rounded-md border border-line bg-raised/40 p-3 text-xs">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-muted">Nuovo stop</span>
        <input value={stop} onChange={(e) => setStop(e.target.value)} className="num w-28 rounded border border-line bg-bg px-2 py-1" />
        <Button variant="outline" onClick={save} disabled={wider && !reason.trim()}>Salva</Button>
        <Button variant="ghost" onClick={onDone}>Annulla</Button>
      </div>
      {wider && (
        <div className="space-y-1">
          <p className="text-warn">Abbassare lo stop aumenta la perdita ammessa: è una nuova autorizzazione, registrata nell&apos;audit e
            accettata solo se il nuovo rischio resta nel budget per operazione e nel rischio aggregato.</p>
          <input value={reason} onChange={(e) => setReason(e.target.value)} placeholder="Motivazione (obbligatoria)"
            className="w-full rounded border border-line bg-bg px-2 py-1" />
        </div>
      )}
      <ErrorNote error={err} />
    </div>
  );
}

export default function PortfolioPage() {
  const { user } = useSession();
  const canAct = user.role !== "viewer";
  const pf = useApi<Portfolio>("/api/portfolio", 30_000);
  const pos = useApi<Page<Position>>("/api/positions?page_size=200", 30_000);
  const orders = useApi<Page<Order>>("/api/paper/orders?page_size=50", 30_000);
  const eq = useApi<EquitySnapshot[]>("/api/portfolio/equity?days=365");
  const rec = useApi<Reconciliation>("/api/paper/reconciliation", 60_000);
  const [kind, setKind] = useState("");
  const al = useApi<Page<Alert>>(`/api/alerts?page_size=50${kind ? `&kind=${kind}` : ""}`, 30_000);
  const sum = useApi<AlertSummary>("/api/alerts/summary", 30_000);
  const [report, setReport] = useState<MonitorReport | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<unknown>(null);
  const [editing, setEditing] = useState<number | null>(null);

  const reloadAll = () => { pf.reload(); pos.reload(); orders.reload(); eq.reload(); rec.reload(); al.reload(); sum.reload(); };
  const runMonitor = async () => {
    setBusy(true); setErr(null);
    try { setReport(await api<MonitorReport>("/api/paper/monitor", { method: "POST" })); reloadAll(); }
    catch (e) { setErr(e); } finally { setBusy(false); }
  };
  const act = async (path: string) => {
    setErr(null);
    try { await api(path, { method: "POST" }); reloadAll(); } catch (e) { setErr(e); }
  };

  const p = pf.data;
  const ccy = p?.base_currency ?? "EUR";
  const equity = Number(p?.equity ?? 0);
  const bySector = new Map<string, number>();
  pos.data?.items.forEach((x) => bySector.set(x.sector ?? "Non classificato", (bySector.get(x.sector ?? "Non classificato") ?? 0) + Number(x.market_value_base)));
  const sectorLimit = Number(p?.risk_params?.max_sector_exposure_pct ?? 30);
  const snaps = eq.data ?? [];

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold">Portfolio Control</h1>
          <p className="text-xs text-muted">Il monitoraggio controlla ordini aperti, stop, target e piani attivi ogni 5 minuti (worker).
            Gli stop e i target eseguiti sono simulati sul PaperBroker; le altre notifiche non inviano ordini.</p>
        </div>
        {canAct && <Button onClick={runMonitor} disabled={busy}>{busy ? "Controllo in corso…" : "Esegui monitoraggio ora"}</Button>}
      </div>
      <ErrorNote error={err} />

      {report && (
        <Card className="px-4 py-3 text-xs">
          <div className="flex flex-wrap gap-x-5 gap-y-1">
            <span>Strumenti verificati: <b className="num">{report.instruments}</b></span>
            <span>Ordini controllati: <b className="num">{report.orders_checked}</b></span>
            <span>Eseguiti: <b className="num">{report.orders_filled.length}</b></span>
            <span>Annullati: <b className="num">{report.orders_cancelled.length}</b></span>
            <span>Uscite simulate: <b className="num">{report.exits.length}</b></span>
            <span>Stop alzati: <b className="num">{report.stops_raised.length}</b></span>
            <span>Piani invalidati: <b className="num">{report.plans_invalidated.length}</b></span>
            <span>Nuovi alert: <b className="num">{report.alerts}</b></span>
            <span>Riconciliazione: {report.reconciliation_ok ? <span className="text-up">ok</span> : <span className="text-down">discrepanze</span>}</span>
          </div>
          {!report.can_execute && <p className="mt-1 text-warn">Esecuzioni sospese: {report.blocked_reason} Il monitoraggio emette solo alert.</p>}
          {report.data_issues.map((d) => <p key={d} className="mt-1 text-warn">{d}</p>)}
        </Card>
      )}

      <div className="grid gap-4 xl:grid-cols-3">
        <Card className="xl:col-span-2">
          <CardHeader title="Equity giornaliera" subtitle="Uno snapshot al giorno (l'ultimo controllo del giorno); base del rendimento giornaliero" />
          {snaps.length < 2 ? (
            <Empty title={snaps.length ? "Un solo giorno registrato" : "Nessuno snapshot"}
              body="La curva compare dal secondo giorno di monitoraggio. Nessun valore viene ricostruito a ritroso." />
          ) : (
            <div className="p-2">
              <Lines height={240} series={[{ name: "Equity", color: "#4f8cff", area: true,
                data: snaps.map((s) => [s.snap_date, Number(s.equity)] as [string, number]) }]} />
              {snaps.some((s) => s.is_demo) && <p className="px-2 text-[11px] text-warn">Include giorni con posizioni su dati DEMO sintetici.</p>}
            </div>
          )}
        </Card>
        <Card>
          <CardHeader title="Riconciliazione" subtitle="Cassa, posizioni e ordini confrontati con movimenti ed eseguiti" />
          {rec.data && (
            <div className="space-y-2 p-4 text-sm">
              <p>{rec.data.ok ? <Badge tone="up">Nessuna discrepanza</Badge> : <Badge tone="down">{rec.data.issues.length} discrepanze</Badge>}</p>
              <p className="text-xs text-muted">Liquidità registrata <span className="num text-fg">{money(rec.data.cash, ccy)}</span> ·
                da movimenti <span className="num text-fg">{money(rec.data.cash_from_movements, ccy)}</span></p>
              {rec.data.issues.map((i, k) => <p key={k} className="text-xs text-down">{i.message} {i.expected != null && `(atteso ${i.expected}, trovato ${i.actual})`}</p>)}
              <p className="text-[11px] text-muted">Verificata {when(rec.data.checked_at)}</p>
            </div>
          )}
        </Card>
      </div>

      <Card>
        <CardHeader title="Posizioni e livelli" subtitle="Lo stop si può solo alzare; abbassarlo richiede una nuova autorizzazione motivata" />
        {pos.data?.items.length === 0 ? <Empty title="Nessuna posizione aperta" /> : (
          <Table head={["Titolo", "Qtà", "Medio", "Ultimo", "Stop", "TP1", "TP2", "Gestione", "Controllo", ""]}>
            {pos.data?.items.map((x) => (
              <Fragment key={x.id}>
                <tr>
                  <td className="px-4 py-2"><p className="font-medium">{x.ticker}</p><p className="text-[11px] text-muted">{x.name}</p></td>
                  <td className="num px-4 py-2">{num(x.quantity, 0)}</td>
                  <td className="num px-4 py-2">{num(x.avg_price)}</td>
                  <td className="num px-4 py-2">{num(x.last_price)}{x.stale && <span className="ml-1 text-warn" title="Quotazione obsoleta">●</span>}</td>
                  <td className="num px-4 py-2">{num(x.stop_loss)}</td>
                  <td className={cn("num px-4 py-2", x.tp1_done && "text-up")}>{num(x.take_profit_1)}{x.tp1_done && " ✓"}</td>
                  <td className="num px-4 py-2">{num(x.take_profit_2)}</td>
                  <td className="px-4 py-2 text-xs">
                    {x.trailing_active ? <Badge tone="accent">Trailing {num(x.trailing_distance)}</Badge>
                      : x.tp1_done ? <Badge>Stop a pareggio</Badge> : <span className="text-muted">Stop iniziale</span>}
                  </td>
                  <td className="px-4 py-2 text-[11px] text-muted">{x.last_checked_at ? when(x.last_checked_at) : "mai"}</td>
                  <td className="px-4 py-2 text-right">
                    {x.is_demo && <DemoBadge />}
                    {canAct && <button onClick={() => setEditing(editing === x.id ? null : x.id)} className="ml-2 text-xs text-accent hover:underline">Stop</button>}
                  </td>
                </tr>
                {editing === x.id && (
                  <tr><td colSpan={10} className="px-4 pb-3">
                    <StopEditor p={x} onDone={() => { setEditing(null); pos.reload(); pf.reload(); }} />
                  </td></tr>
                )}
              </Fragment>
            ))}
          </Table>
        )}
      </Card>

      <Card>
        <CardHeader title="Ordini simulati" subtitle="Solo ordini del PaperBroker. Gli ordini LIMIT restano aperti fino all'esecuzione, alla scadenza o all'annullamento" />
        {orders.data?.items.length === 0 ? <Empty title="Nessun ordine" /> : (
          <Table head={["#", "Data", "Strumento", "Lato", "Tipo", "Motivo", "Qtà", "Eseguita", "Limite", "Validità", "Stato", ""]}>
            {orders.data?.items.map((o) => (
              <tr key={o.id}>
                <td className="num px-4 py-2 text-muted">{o.id}</td>
                <td className="px-4 py-2 text-xs">{when(o.created_at)}</td>
                <td className="px-4 py-2">{o.ticker ?? o.instrument_id}</td>
                <td className={cn("px-4 py-2", o.side === "BUY" ? "text-up" : "text-down")}>{o.side}</td>
                <td className="px-4 py-2">{o.order_type}</td>
                <td className="px-4 py-2 text-xs">{PURPOSE[o.purpose] ?? o.purpose}</td>
                <td className="num px-4 py-2">{num(o.quantity, 0)}</td>
                <td className="num px-4 py-2">{num(o.filled_quantity, 0)}</td>
                <td className="num px-4 py-2">{num(o.limit_price)}</td>
                <td className="px-4 py-2 text-[11px] text-muted">{o.valid_until ? when(o.valid_until) : "—"}</td>
                <td className="px-4 py-2"><Badge tone={statusTone(o.status)}>{o.status}</Badge>
                  {o.reject_reason && <p className="mt-0.5 max-w-xs text-[11px] text-muted">{o.reject_reason}</p>}</td>
                <td className="px-4 py-2 text-right">
                  {o.is_demo && <DemoBadge />}
                  {canAct && OPEN.includes(o.status) && (
                    <button onClick={() => act(`/api/paper/orders/${o.id}/cancel`)} className="ml-2 text-xs text-down hover:underline">Annulla</button>
                  )}
                </td>
              </tr>
            ))}
          </Table>
        )}
      </Card>

      <Card id="alert">
        <CardHeader title="Alert" subtitle={`${sum.data?.active ?? 0} da leggere · una notifica non è un ordine eseguito`}
          right={
            <select value={kind} onChange={(e) => setKind(e.target.value)} className="rounded border border-line bg-bg px-2 py-1 text-xs">
              <option value="">Tutti i tipi</option>
              {Object.entries(ALERT_KINDS).map(([k, v]) => <option key={k} value={k}>{v}{sum.data?.by_kind[k] ? ` (${sum.data.by_kind[k]})` : ""}</option>)}
            </select>
          } />
        {al.data?.items.length === 0 ? <Empty title="Nessun alert da leggere" /> : (
          <ul className="divide-y divide-line">
            {al.data?.items.map((a) => (
              <li key={a.id} className="flex gap-3 px-4 py-2.5 text-xs">
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <Badge tone={a.severity === "CRITICAL" ? "down" : a.severity === "WARNING" ? "warn" : "neutral"}>{a.severity}</Badge>
                    <span className="text-muted">{ALERT_KINDS[a.kind] ?? a.kind}</span>
                    <span className="ml-auto text-muted">{when(a.created_at)}</span>
                  </div>
                  <p className="mt-1">{a.message}</p>
                </div>
                {canAct && <button onClick={() => act(`/api/alerts/${a.id}/ack`)} className="self-start text-accent hover:underline">Letto</button>}
              </li>
            ))}
          </ul>
        )}
      </Card>

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
              <p className="text-[11px] text-muted">Liquidità: {money(p?.cash, ccy)} ({num(equity ? (Number(p?.cash) / equity) * 100 : 0)}%)</p>
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
          <p className="border-t border-line px-4 py-2 text-[11px] text-muted">Leva e vendite allo scoperto disabilitate.</p>
        </Card>
      </div>
    </div>
  );
}
