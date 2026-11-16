// Componenti base in stile shadcn/ui, adattati al tema scuro della piattaforma.
import * as React from "react";
import { cn } from "@/lib/format";

export function Card({ className, ...p }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("rounded-lg border border-line bg-panel", className)} {...p} />;
}

export function CardHeader({ title, subtitle, right }: { title: string; subtitle?: React.ReactNode; right?: React.ReactNode }) {
  return (
    <div className="flex items-start justify-between gap-3 border-b border-line px-4 py-3">
      <div>
        <h2 className="text-sm font-semibold tracking-tight">{title}</h2>
        {subtitle && <p className="mt-0.5 text-xs text-muted">{subtitle}</p>}
      </div>
      {right}
    </div>
  );
}

type BadgeTone = "neutral" | "demo" | "warn" | "up" | "down" | "accent";
const badgeTones: Record<BadgeTone, string> = {
  neutral: "border-line text-muted",
  demo: "border-warn/50 bg-warn/10 text-warn",
  warn: "border-warn/40 text-warn",
  up: "border-up/40 text-up",
  down: "border-down/40 bg-down/10 text-down",
  accent: "border-accent/40 text-accent",
};
export function Badge({ tone = "neutral", className, ...p }: React.HTMLAttributes<HTMLSpanElement> & { tone?: BadgeTone }) {
  return (
    <span
      className={cn("inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider", badgeTones[tone], className)}
      {...p}
    />
  );
}

export function DemoBadge() {
  return <Badge tone="demo" title="Dati sintetici dimostrativi: non sono quotazioni reali">Demo</Badge>;
}

type BtnVariant = "primary" | "ghost" | "danger" | "outline";
const btn: Record<BtnVariant, string> = {
  primary: "bg-accent text-white hover:bg-accent/90",
  ghost: "text-muted hover:bg-raised hover:text-fg",
  danger: "bg-down text-white hover:bg-down/90",
  outline: "border border-line text-fg hover:bg-raised",
};
export function Button({ variant = "primary", className, ...p }: React.ButtonHTMLAttributes<HTMLButtonElement> & { variant?: BtnVariant }) {
  return (
    <button
      className={cn("inline-flex h-8 items-center justify-center gap-1.5 rounded-md px-3 text-sm font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50", btn[variant], className)}
      {...p}
    />
  );
}

export const Input = React.forwardRef<HTMLInputElement, React.InputHTMLAttributes<HTMLInputElement>>(function Input({ className, ...p }, ref) {
  return (
    <input
      ref={ref}
      className={cn("h-8 w-full rounded-md border border-line bg-bg px-2.5 text-sm outline-none placeholder:text-muted/60 focus:border-accent", className)}
      {...p}
    />
  );
});

export function Label({ children, hint }: { children: React.ReactNode; hint?: string }) {
  return (
    <label className="mb-1 block text-xs font-medium text-muted">
      {children}
      {hint && <span className="ml-1 font-normal text-muted/70">{hint}</span>}
    </label>
  );
}

export function Empty({ title, body }: { title: string; body?: React.ReactNode }) {
  return (
    <div className="px-4 py-8 text-center">
      <p className="text-sm font-medium">{title}</p>
      {body && <p className="mx-auto mt-1 max-w-md text-xs text-muted">{body}</p>}
    </div>
  );
}

export function ErrorNote({ error }: { error: unknown }) {
  if (!error) return null;
  const msg = error instanceof Error ? error.message : String(error);
  return <p className="rounded-md border border-down/40 bg-down/10 px-3 py-2 text-xs text-down">{msg}</p>;
}

export function Table({ head, children }: { head: React.ReactNode[]; children: React.ReactNode }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-line text-left text-[11px] uppercase tracking-wider text-muted">
            {head.map((h, i) => <th key={i} className="whitespace-nowrap px-4 py-2 font-medium">{h}</th>)}
          </tr>
        </thead>
        <tbody className="divide-y divide-line">{children}</tbody>
      </table>
    </div>
  );
}
