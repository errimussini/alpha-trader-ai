"use client";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Activity } from "lucide-react";
import { login } from "@/lib/api";
import { Button, ErrorNote, Input, Label } from "@/components/ui/primitives";

export default function LoginPage() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true); setError(null);
    try { await login(email, password); router.replace("/"); }
    catch (err) { setError(err); }
    finally { setBusy(false); }
  }

  return (
    <div className="grid min-h-screen place-items-center px-4">
      <form onSubmit={submit} className="w-full max-w-sm rounded-lg border border-line bg-panel p-6">
        <div className="mb-6 flex items-center gap-2">
          <Activity className="h-5 w-5 text-accent" />
          <span className="font-bold tracking-tight">ALPHA TRADER <span className="text-accent">AI</span></span>
        </div>
        <div className="space-y-3">
          <div><Label>Email</Label><Input type="email" autoComplete="username" value={email} onChange={(e) => setEmail(e.target.value)} required /></div>
          <div><Label>Password</Label><Input type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required /></div>
          <ErrorNote error={error} />
          <Button className="w-full" disabled={busy}>{busy ? "Accesso…" : "Accedi"}</Button>
        </div>
        <p className="mt-4 text-[11px] leading-relaxed text-muted">
          Modalità disponibili: Research e Paper Trading. Nessun ordine reale viene inviato.
        </p>
      </form>
    </div>
  );
}
