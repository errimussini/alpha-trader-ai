"use client";
import { useEffect, useState } from "react";
import { api, DECISION_LABELS, type AiStatus, type CommitteeResult, type CommitteeState, type Stance } from "@/lib/api";
import { useApi } from "@/lib/hooks";
import { cn, when } from "@/lib/format";
import { Badge, Button, Card, CardHeader, ErrorNote } from "./ui/primitives";

const STANCE_TONE: Record<Stance, "up" | "neutral" | "down"> = { FAVOREVOLE: "up", NEUTRALE: "neutral", CONTRARIO: "down" };
const DECISION_TONE = { BUY: "up", WAIT: "warn", HOLD: "accent", REDUCE: "warn", EXIT: "down", REJECT: "down" } as const;

function Bullets({ items, className }: { items?: string[]; className?: string }) {
  if (!items?.length) return null;
  return <ul className={cn("space-y-0.5 text-xs", className)}>{items.map((x, i) => <li key={i}>• {x}</li>)}</ul>;
}

function ModeBadge({ mode }: { mode: string }) {
  return mode === "ai"
    ? <Badge tone="accent" title="Sintesi scritta dal modello Claude sui dati verificati">AI</Badge>
    : mode === "mixed"
      ? <Badge tone="warn" title="Alcuni agenti sono ricaduti sulle regole deterministiche">AI parziale</Badge>
      : <Badge title="Regole deterministiche, senza modello AI">Regole</Badge>;
}

function Warnings({ v }: { v?: { unverified_numbers: string[]; unknown_refs: string[] } }) {
  if (!v || (!v.unverified_numbers.length && !v.unknown_refs.length)) return null;
  return (
    <p className="text-[11px] text-warn">
      {v.unverified_numbers.length ? `Numeri non trovati nei dati: ${v.unverified_numbers.join(", ")}. ` : ""}
      {v.unknown_refs.length ? `Riferimenti scartati: ${v.unknown_refs.join(", ")}.` : ""}
    </p>
  );
}

function Result({ c }: { c: CommitteeResult }) {
  const d = c.decision;
  return (
    <div className="space-y-4 p-4">
      <div className="flex flex-wrap items-center gap-3 rounded-md border border-line bg-bg/40 p-3">
        <Badge tone={DECISION_TONE[d.decision]} className="px-2 py-1 text-xs">{d.decision}</Badge>
        <div className="min-w-0 flex-1">
          <p className="text-sm font-medium">{DECISION_LABELS[d.decision]}</p>
          <p className="text-xs text-muted">{d.summary}</p>
        </div>
        <div className="text-right text-[11px] text-muted">
          <ModeBadge mode={c.mode} />
          <p className="mt-1">{c.model ?? "senza modello"} · {when(c.completed_at)}</p>
          <p>ammesse: {c.allowed_decisions.join(", ")}</p>
        </div>
      </div>
      {c.constraints.length > 0 && <p className="text-[11px] text-muted">Vincoli deterministici: {c.constraints.join("; ")}</p>}
      <div className="grid gap-3 md:grid-cols-2">
        <div><h3 className="mb-1 text-xs font-semibold text-up">Ragioni a favore</h3><Bullets items={d.reasons_for} /></div>
        <div><h3 className="mb-1 text-xs font-semibold text-down">Ragioni contrarie</h3><Bullets items={d.reasons_against} /></div>
        <div><h3 className="mb-1 text-xs font-semibold text-warn">Potrebbe essere errata se</h3><Bullets items={d.could_be_wrong_if} /></div>
        <div><h3 className="mb-1 text-xs font-semibold">Condizioni per agire</h3><Bullets items={d.conditions_to_act} />
          {d.dissent.length > 0 && <p className="mt-1 text-[11px] text-muted">In disaccordo: {d.dissent.join(", ")}</p>}</div>
      </div>
      <Warnings v={d.validation} />
      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
        {c.agents.map((a) => (
          <div key={a.agent} className="space-y-2 rounded-md border border-line p-3">
            <div className="flex items-center justify-between gap-2">
              <p className="text-xs font-semibold">{a.title}</p>
              <div className="flex gap-1"><ModeBadge mode={a.mode} /><Badge tone={STANCE_TONE[a.stance]}>{a.stance}</Badge></div>
            </div>
            <p className="text-xs">{a.summary}</p>
            <Bullets items={a.points_for} className="text-up" />
            <Bullets items={a.points_against} className="text-down" />
            <Bullets items={a.risks} className="text-warn" />
            {a.data_refs.length > 0 && <p className="text-[10px] text-muted">Dati: {a.data_refs.join(", ")}</p>}
            {a.error && <p className="text-[10px] text-muted">Modello non disponibile per questo agente: {a.error}</p>}
            <Warnings v={a.validation} />
          </div>
        ))}
      </div>
      <p className="text-[11px] text-muted">{c.disclaimer} Versione {c.version} · prompt {c.prompt_version} · dati {c.inputs_hash.slice(0, 12)}</p>
    </div>
  );
}

export function CommitteePanel({ planId, canRequest }: { planId: number; canRequest: boolean }) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<unknown>(null);
  const ai = useApi<AiStatus>("/api/ai/status");
  const [poll, setPoll] = useState(false);
  const q = useApi<{ committee: CommitteeState }>(`/api/opportunities/${planId}/committee`, poll ? 4000 : undefined);
  const c = q.data?.committee;
  const pending = !!c && (c.status === "QUEUED" || c.status === "RUNNING");
  useEffect(() => { setPoll(pending); }, [pending]);
  const shown: CommitteeResult | null | undefined = c?.status === "DONE" ? (c as CommitteeResult) : (c as { previous?: CommitteeResult | null })?.previous;

  async function convene() {
    setBusy(true); setErr(null);
    try { await api(`/api/opportunities/${planId}/committee`, { method: "POST" }); await q.reload(); }
    catch (e) { setErr(e); } finally { setBusy(false); }
  }

  return (
    <Card>
      <CardHeader title="Investment Committee" subtitle="7 agenti: Market, Selector, Technical, Fundamental, Risk (veto), Portfolio, Committee"
        right={canRequest ? (
          <Button variant="outline" disabled={busy || !!pending} onClick={convene}
            title={ai.data?.enabled ? `Modello ${ai.data.model}, effort ${ai.data.effort}` : "Senza ANTHROPIC_API_KEY il comitato usa le regole deterministiche"}>
            {pending ? "Comitato in corso…" : ai.data?.enabled ? "Convoca il comitato AI" : "Ricalcola (regole)"}
          </Button>) : null} />
      {pending && c && <p className="border-b border-line px-4 py-2 text-xs text-muted">
        {c.status === "QUEUED" ? "In coda: il worker eseguirà i 7 agenti a breve." : "Gli agenti stanno valutando il piano…"}
        {shown ? " Sotto, l'ultima valutazione disponibile." : ""}</p>}
      {c?.status === "FAILED" && <p className="border-b border-line px-4 py-2 text-xs text-down">Comitato non riuscito: {c.error}</p>}
      {!ai.data?.enabled && <p className="border-b border-line px-4 py-2 text-[11px] text-muted">
        Modalità deterministica: aggiungi ANTHROPIC_API_KEY nel file .env del server per la sintesi degli agenti con Claude.</p>}
      <ErrorNote error={err ?? q.error} />
      {shown ? <Result c={shown} /> : !pending && <p className="p-4 text-xs text-muted">Nessuna valutazione del comitato per questo piano.</p>}
    </Card>
  );
}
