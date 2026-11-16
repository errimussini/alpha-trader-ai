"use client";
import { useState } from "react";
import { api } from "@/lib/api";
import { useApi } from "@/lib/hooks";
import { money, num, when } from "@/lib/format";
import { Badge, Button, Card, CardHeader, ErrorNote, Input } from "@/components/ui/primitives";

export type LiveStatus = {
  available: boolean; reason: string; mode: string; broker: string; sends_real_orders: boolean;
  account: { equity_base: string; cash_base: string; currency: string; as_of: string; source: string; is_simulated: boolean } | null;
  limits: { confirmation_ttl_seconds: number; max_order_notional_base: string; max_orders_per_day: number; orders_today: number;
    max_price_drift_bps: string; require_second_approver: boolean };
  guarantees: string[];
};
type Ticket = {
  id: number; ticker: string; side: string; order_type: string; quantity: string; limit_price: string; stop_loss: string | null;
  take_profit_1: string | null; currency: string; notional_base: string; est_loss_at_stop_base: string | null;
  quote_price: string; quote_time: string; warnings: string[]; status: string; status_reason: string | null; expires_at: string;
  broker_order_id: string | null; sends_real_orders: boolean; confirmation_code?: string;
};

export function LiveStatusCard({ isAdmin }: { isAdmin: boolean }) {
  const st = useApi<LiveStatus>("/api/live/status");
  const s = st.data;
  return (
    <Card>
      <CardHeader title="Live assistito" right={s?.available ? <Badge tone="warn">Configurato</Badge> : <Badge>Spento</Badge>} />
      {s && (
        <div className="space-y-2 p-4 text-xs">
          <p>{s.reason}</p>
          {s.available && s.account && (
            <p className="text-muted">Conto: {money(s.account.equity_base, s.account.currency)} · {s.account.source}</p>
          )}
          <p className="text-muted">Limiti: {money(s.limits.max_order_notional_base)} per ordine · {s.limits.max_orders_per_day} ordini al giorno
            (oggi {s.limits.orders_today}) · scadenza conferma {s.limits.confirmation_ttl_seconds} s
            {s.limits.require_second_approver ? " · quattro occhi" : ""}</p>
          <ul className="space-y-0.5">{s.guarantees.map((g) => <li key={g} className="flex gap-1.5"><span className="text-accent">›</span>{g}</li>)}</ul>
          {!s.available && isAdmin && (
            <p className="text-muted">Per attivarlo servono, nel file .env del server, LIVE_TRADING_ENABLED=true e un LIVE_BROKER configurato
              (dry_run per il collaudo), poi il passaggio alla modalità Live assistito da questa pagina.</p>
          )}
        </div>
      )}
    </Card>
  );
}

export function LiveTicketPanel({ planId, canAct }: { planId: number; canAct: boolean }) {
  const st = useApi<LiveStatus>("/api/live/status");
  const [ticket, setTicket] = useState<Ticket | null>(null);
  const [code, setCode] = useState("");
  const [ack, setAck] = useState(false);
  const [err, setErr] = useState<unknown>(null);
  const s = st.data;
  if (!s || !s.available || s.mode !== "LIVE_ASSISTED") return null;

  const propose = async () => {
    setErr(null); setCode(""); setAck(false);
    try { setTicket(await api<Ticket>("/api/live/tickets", { method: "POST", body: JSON.stringify({ trade_plan_id: planId }) })); }
    catch (e) { setErr(e); }
  };
  const confirm = async () => {
    if (!ticket) return;
    setErr(null);
    try {
      setTicket(await api<Ticket>(`/api/live/tickets/${ticket.id}/confirm`, { method: "POST",
        body: JSON.stringify({ confirmation_code: code, acknowledge: ack }) }));
      st.reload();
    } catch (e) { setErr(e); }
  };
  const cancel = async () => {
    if (!ticket) return;
    try { setTicket(await api<Ticket>(`/api/live/tickets/${ticket.id}/cancel`, { method: "POST" })); } catch (e) { setErr(e); }
  };

  return (
    <Card className="border-warn/50">
      <CardHeader title="Ordine live assistito"
        subtitle={s.sends_real_orders ? "Ordine REALE sul conto del broker: richiede la tua conferma esplicita" : "Broker di collaudo: nessun ordine reale"}
        right={<Badge tone={s.sends_real_orders ? "down" : "warn"}>{s.broker}</Badge>} />
      <div className="space-y-3 p-4 text-sm">
        {!ticket && canAct && <Button variant="outline" onClick={propose}>Prepara ticket</Button>}
        {ticket && (
          <>
            <dl className="grid grid-cols-2 gap-x-6 gap-y-1 text-xs sm:grid-cols-4">
              <dt className="text-muted">Ordine</dt><dd>{ticket.side} {num(ticket.quantity, 0)} {ticket.ticker}</dd>
              <dt className="text-muted">Limite</dt><dd className="num">{num(ticket.limit_price)} {ticket.currency}</dd>
              <dt className="text-muted">Stop / TP1</dt><dd className="num">{num(ticket.stop_loss)} / {num(ticket.take_profit_1)}</dd>
              <dt className="text-muted">Controvalore</dt><dd className="num">{money(ticket.notional_base)}</dd>
              <dt className="text-muted">Perdita stimata a stop</dt><dd className="num">{money(ticket.est_loss_at_stop_base)}</dd>
              <dt className="text-muted">Quotazione</dt><dd className="num">{num(ticket.quote_price)} · {when(ticket.quote_time)}</dd>
              <dt className="text-muted">Stato</dt><dd><Badge tone={ticket.status === "SUBMITTED" ? "up" : ticket.status === "PENDING_CONFIRMATION" ? "warn" : "neutral"}>{ticket.status}</Badge></dd>
              <dt className="text-muted">Scade</dt><dd>{when(ticket.expires_at)}</dd>
            </dl>
            {ticket.warnings.map((w) => <p key={w} className="text-xs text-warn">{w}</p>)}
            {ticket.status_reason && <p className="text-xs text-muted">{ticket.status_reason}{ticket.broker_order_id ? ` · id broker ${ticket.broker_order_id}` : ""}</p>}
            {ticket.status === "PENDING_CONFIRMATION" && (
              <div className="space-y-2 rounded-md border border-line bg-raised/40 p-3">
                {ticket.confirmation_code && (
                  <p className="text-xs">Codice di conferma (mostrato una sola volta): <span className="num text-base font-semibold tracking-widest">{ticket.confirmation_code}</span></p>
                )}
                <label className="flex items-start gap-2 text-xs">
                  <input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} className="mt-0.5" />
                  Ho riletto il riepilogo. So che il risultato non è garantito e che la perdita allo stop è una stima, non un massimo.
                </label>
                <div className="flex gap-2">
                  <Input value={code} onChange={(e) => setCode(e.target.value)} placeholder="Digita il codice" className="w-40" />
                  <Button onClick={confirm} disabled={!ack || code.length !== 6}>Conferma e invia</Button>
                  <Button variant="ghost" onClick={cancel}>Annulla</Button>
                </div>
              </div>
            )}
          </>
        )}
        <ErrorNote error={err} />
      </div>
    </Card>
  );
}
