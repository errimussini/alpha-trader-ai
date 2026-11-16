"use client";
import { useEffect, useRef, useState } from "react";
import { Send } from "lucide-react";
import { api, TOOL_LABELS, type AiStatus, type ChatAnswer } from "@/lib/api";
import { useApi } from "@/lib/hooks";
import { cn } from "@/lib/format";
import { Badge, Button, Card, CardHeader, ErrorNote } from "@/components/ui/primitives";

type Turn = { role: "user" | "assistant"; content: string; meta?: ChatAnswer };

const EXAMPLES = [
  "Quali sono oggi le tre migliori opportunità?",
  "Con 2.000 € disponibili, quale operazione è compatibile con i miei limiti di rischio?",
  "Perché hai selezionato il titolo con il punteggio più alto?",
  "Cosa potrebbe invalidare il trade migliore?",
  "Quali posizioni richiedono attenzione?",
  "Come ha performato il sistema negli ultimi 90 giorni?",
];

function Sources({ a }: { a: ChatAnswer }) {
  const [open, setOpen] = useState(false);
  if (!a.sources.length) return <p className="mt-2 text-[11px] text-muted">Nessuno strumento consultato: la risposta non cita dati.</p>;
  return (
    <div className="mt-2">
      <button className="text-[11px] text-accent hover:underline" onClick={() => setOpen(!open)}>
        {open ? "Nascondi" : "Mostra"} i dati che giustificano la risposta ({a.sources.length})
      </button>
      {open && (
        <div className="mt-2 space-y-2">
          {a.sources.map((s, i) => (
            <details key={i} className="rounded border border-line bg-bg/40 p-2 text-[11px]">
              <summary className="cursor-pointer">
                {TOOL_LABELS[s.tool] ?? s.tool} <span className="text-muted">{JSON.stringify(s.input)}</span>
                {s.error && <span className="text-down"> · errore: {s.error}</span>}
              </summary>
              <pre className="num mt-2 max-h-64 overflow-auto whitespace-pre-wrap text-[10px] text-muted">{JSON.stringify(s.output, null, 2)}</pre>
            </details>
          ))}
        </div>
      )}
    </div>
  );
}

export default function AssistantPage() {
  const ai = useApi<AiStatus>("/api/ai/status");
  const [turns, setTurns] = useState<Turn[]>([]);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<unknown>(null);
  const end = useRef<HTMLDivElement>(null);
  useEffect(() => { end.current?.scrollIntoView({ behavior: "smooth" }); }, [turns, busy]);

  async function ask(q: string) {
    const question = q.trim();
    if (!question || busy) return;
    const next = [...turns, { role: "user" as const, content: question }];
    setTurns(next); setText(""); setBusy(true); setErr(null);
    try {
      const r = await api<ChatAnswer>("/api/assistant/chat", { method: "POST", body: JSON.stringify({
        messages: next.slice(-12).map(({ role, content }) => ({ role, content })),
      }) });
      setTurns([...next, { role: "assistant", content: r.answer, meta: r }]);
    } catch (e) { setErr(e); setTurns(turns); setText(question); } finally { setBusy(false); }
  }

  return (
    <div className="mx-auto max-w-4xl space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h1 className="text-lg font-semibold">AI Trading Assistant</h1>
          <p className="text-xs text-muted">Risposte basate solo sui dati della piattaforma, con le fonti consultate. Nessun ordine viene inviato da qui.</p>
        </div>
        {ai.data && (ai.data.enabled
          ? <Badge tone="accent">AI · {ai.data.model}</Badge>
          : <Badge title="Aggiungi ANTHROPIC_API_KEY nel .env del server per le domande libere">Modalità deterministica</Badge>)}
      </div>

      <Card>
        <CardHeader title="Conversazione" subtitle="Le informazioni sono di supporto alla decisione, non consulenza finanziaria" />
        <div className="max-h-[60vh] min-h-[240px] space-y-3 overflow-y-auto p-4">
          {!turns.length && (
            <div className="space-y-2">
              <p className="text-xs text-muted">Prova una di queste domande:</p>
              <div className="flex flex-wrap gap-2">
                {EXAMPLES.map((e) => (
                  <button key={e} onClick={() => ask(e)}
                    className="rounded-full border border-line px-3 py-1 text-xs hover:border-accent hover:text-accent">{e}</button>
                ))}
              </div>
            </div>
          )}
          {turns.map((t, i) => (
            <div key={i} className={cn("flex", t.role === "user" ? "justify-end" : "justify-start")}>
              <div className={cn("max-w-[85%] rounded-lg px-3 py-2 text-sm",
                t.role === "user" ? "bg-accent/15" : "border border-line bg-panel")}>
                <p className="whitespace-pre-wrap">{t.content}</p>
                {t.meta && (
                  <>
                    <p className="mt-1 text-[10px] text-muted">
                      {t.meta.mode === "ai" ? `Claude · ${t.meta.model}` : "Regole deterministiche"} · registro #{t.meta.run_id}
                    </p>
                    <Sources a={t.meta} />
                  </>
                )}
              </div>
            </div>
          ))}
          {busy && <p className="text-xs text-muted">Sto consultando i dati…</p>}
          <div ref={end} />
        </div>
        <form className="flex gap-2 border-t border-line p-3" onSubmit={(e) => { e.preventDefault(); ask(text); }}>
          <input value={text} onChange={(e) => setText(e.target.value)} maxLength={4000} placeholder="Fai una domanda sulle opportunità, sul rischio o sul portafoglio…"
            className="min-w-0 flex-1 rounded-md border border-line bg-bg px-3 py-2 text-sm outline-none focus:border-accent" />
          <Button type="submit" disabled={busy || !text.trim()} aria-label="Invia"><Send className="h-4 w-4" /></Button>
        </form>
        <div className="px-3 pb-3"><ErrorNote error={err} /></div>
      </Card>
    </div>
  );
}
