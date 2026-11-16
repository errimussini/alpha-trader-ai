"use client";
import { useState } from "react";
import { Power, ShieldCheck } from "lucide-react";
import { api, type Page } from "@/lib/api";
import { useApi } from "@/lib/hooks";
import { when } from "@/lib/format";
import { Badge, Button, Card, CardHeader, ErrorNote, Input, Label, Table } from "@/components/ui/primitives";
import { useSession } from "@/components/shell";

type AuditRow = { id: number; created_at: string; actor: string; action: string; entity: string | null; entity_id: string | null; hash: string };

export default function SettingsPage() {
  const { user, status, reloadStatus } = useSession();
  const isAdmin = user.role === "admin";
  const canAudit = user.role !== "viewer";
  const audit = useApi<Page<AuditRow>>(canAudit ? "/api/system/audit?page_size=30" : null);
  const [verify, setVerify] = useState<{ valid: boolean; first_invalid_id: number | null } | null>(null);
  const [reason, setReason] = useState("");
  const [err, setErr] = useState<unknown>(null);

  async function act(fn: () => Promise<unknown>) {
    setErr(null);
    try { await fn(); reloadStatus(); audit.reload(); } catch (e) { setErr(e); }
  }

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-semibold">Sistema e sicurezza</h1>
      <ErrorNote error={err} />
      <div className="grid gap-4 lg:grid-cols-3">
        <Card>
          <CardHeader title="Modalità operativa" />
          <div className="space-y-3 p-4 text-sm">
            <div className="flex gap-2">
              {(["RESEARCH", "PAPER"] as const).map((m) => (
                <Button key={m} variant={status?.mode === m ? "primary" : "outline"} disabled={!isAdmin}
                  onClick={() => act(() => api("/api/system/mode", { method: "PUT", body: JSON.stringify({ mode: m }) }))}>
                  {m === "RESEARCH" ? "Research" : "Paper Trading"}
                </Button>
              ))}
            </div>
            <p className="text-xs text-muted">Research: sola analisi, nessun ordine. Paper: esecuzione simulata su portafoglio virtuale.</p>
            <p className="text-xs text-muted">Live Assisted: <span className="text-fg">non disponibile in questa release</span>. Richiederà validazione prospettica e conferma umana per ogni ordine.</p>
          </div>
        </Card>
        <Card>
          <CardHeader title="Kill switch" right={status?.kill_switch.active ? <Badge tone="down">Attivo</Badge> : <Badge tone="up">Inattivo</Badge>} />
          <div className="space-y-2 p-4">
            {status?.kill_switch.active ? (
              <>
                <p className="text-xs">Motivo: {status.kill_switch.reason ?? "—"} · dal {when(status.kill_switch.since)}</p>
                <Button variant="outline" disabled={!isAdmin}
                  onClick={() => act(() => api("/api/system/kill-switch", { method: "POST", body: JSON.stringify({ active: false }) }))}>Disattiva</Button>
              </>
            ) : (
              <>
                <Label>Motivo</Label>
                <Input value={reason} onChange={(e) => setReason(e.target.value)} placeholder="es. anomalia dati" />
                <Button variant="danger" disabled={!isAdmin}
                  onClick={() => act(() => api("/api/system/kill-switch", { method: "POST", body: JSON.stringify({ active: true, reason: reason || null }) }))}>
                  <Power className="h-3.5 w-3.5" /> Blocca tutti gli ordini
                </Button>
              </>
            )}
          </div>
        </Card>
        <Card>
          <CardHeader title="Fonte dati" />
          <div className="space-y-1 p-4 text-xs">
            <p className="text-sm">{status?.market_data.name}</p>
            <p className="text-muted">{status?.market_data.license_note}</p>
            <p className="text-muted">Mercati: {status?.market_data.markets.join(", ")}</p>
            <p className="text-muted">Soglia dati obsoleti: {status?.max_quote_age_minutes} min (oltre, ordini e raccomandazioni sono sospesi)</p>
          </div>
        </Card>
      </div>

      {canAudit && (
        <Card>
          <CardHeader title="Audit log" subtitle="Registro append-only con catena di hash verificabile"
            right={<Button variant="outline" onClick={async () => setVerify(await api("/api/system/audit/verify"))}>
              <ShieldCheck className="h-3.5 w-3.5" /> Verifica integrità</Button>} />
          {verify && (
            <p className={`border-b border-line px-4 py-2 text-xs ${verify.valid ? "text-up" : "text-down"}`}>
              {verify.valid ? "Catena integra." : `Catena NON integra dalla riga ${verify.first_invalid_id}.`}
            </p>
          )}
          <Table head={["#", "Data", "Attore", "Azione", "Entità", "Hash"]}>
            {audit.data?.items.map((a) => (
              <tr key={a.id}>
                <td className="num px-4 py-1.5 text-muted">{a.id}</td>
                <td className="px-4 py-1.5 text-xs">{when(a.created_at)}</td>
                <td className="px-4 py-1.5 text-xs">{a.actor}</td>
                <td className="px-4 py-1.5 text-xs">{a.action}</td>
                <td className="px-4 py-1.5 text-xs text-muted">{a.entity ? `${a.entity} ${a.entity_id ?? ""}` : "—"}</td>
                <td className="num px-4 py-1.5 text-[11px] text-muted">{a.hash.slice(0, 12)}…</td>
              </tr>
            ))}
          </Table>
        </Card>
      )}
    </div>
  );
}
