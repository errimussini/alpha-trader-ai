"use client";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { createContext, useContext, useEffect, useState } from "react";
import {
  Activity, Bot, Briefcase, FlaskConical, LayoutDashboard, LineChart, LogOut, Power, Radar, Settings,
} from "lucide-react";
import { api, getToken, setToken, type SystemStatus, type User } from "@/lib/api";
import { useApi } from "@/lib/hooks";
import { cn } from "@/lib/format";
import { Badge } from "./ui/primitives";

const NAV = [
  { href: "/", label: "Dashboard", icon: LayoutDashboard },
  { href: "/market", label: "Mercati e dati", icon: LineChart },
  { href: "/radar", label: "Opportunity Radar", icon: Radar },
  { href: "/portfolio", label: "Portfolio Control", icon: Briefcase },
  { href: "/lab", label: "Strategy Lab", icon: FlaskConical, phase: 5 },
  { href: "/assistant", label: "AI Assistant", icon: Bot },
  { href: "/settings", label: "Sistema", icon: Settings },
];

type Ctx = { user: User; status: SystemStatus | null; reloadStatus: () => void };
const SessionCtx = createContext<Ctx | null>(null);
export const useSession = () => {
  const c = useContext(SessionCtx);
  if (!c) throw new Error("useSession fuori da Shell");
  return c;
};

export function Shell({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const path = usePathname();
  const [user, setUser] = useState<User | null>(null);
  const status = useApi<SystemStatus>(user ? "/api/system/status" : null, 30_000);

  useEffect(() => {
    if (!getToken()) { router.replace("/login"); return; }
    api<User>("/api/auth/me").then(setUser).catch(() => router.replace("/login"));
  }, [router]);

  if (!user) return <div className="grid min-h-screen place-items-center text-sm text-muted">Caricamento…</div>;
  const s = status.data;

  return (
    <SessionCtx.Provider value={{ user, status: s, reloadStatus: status.reload }}>
      <div className="flex min-h-screen">
        <aside className="hidden w-56 shrink-0 flex-col border-r border-line bg-panel md:flex">
          <div className="flex h-14 items-center gap-2 border-b border-line px-4">
            <Activity className="h-5 w-5 text-accent" />
            <span className="text-sm font-bold tracking-tight">ALPHA TRADER <span className="text-accent">AI</span></span>
          </div>
          <nav className="flex-1 space-y-0.5 p-2">
            {NAV.map(({ href, label, icon: Icon, phase }) => (
              <Link key={href} href={href}
                className={cn("flex items-center gap-2.5 rounded-md px-2.5 py-2 text-sm text-muted hover:bg-raised hover:text-fg",
                  path === href && "bg-raised text-fg")}>
                <Icon className="h-4 w-4" />
                <span className="flex-1">{label}</span>
                {phase && <span className="text-[10px] text-muted/70">F{phase}</span>}
              </Link>
            ))}
          </nav>
          <div className="border-t border-line p-3 text-xs text-muted">
            <p className="truncate">{user.email}</p>
            <p className="uppercase tracking-wider">{user.role}</p>
            <button onClick={() => { setToken(null); router.replace("/login"); }}
              className="mt-2 inline-flex items-center gap-1 hover:text-fg"><LogOut className="h-3 w-3" /> Esci</button>
          </div>
        </aside>

        <div className="flex min-w-0 flex-1 flex-col">
          <header className="flex h-14 items-center gap-3 border-b border-line bg-panel/60 px-4 text-xs">
            <span className="font-semibold md:hidden">ALPHA <span className="text-accent">AI</span></span>
            {s && (
              <>
                <Badge tone={s.mode === "PAPER" ? "accent" : "neutral"}>{s.mode === "PAPER" ? "Paper Trading" : "Research"}</Badge>
                <Badge tone="neutral" title="La modalità Live non è disponibile in questa release">Live: off</Badge>
                {s.kill_switch.active && <Badge tone="down"><Power className="h-3 w-3" /> Kill switch attivo</Badge>}
                <span className="hidden truncate text-muted sm:inline">
                  Dati: <span className="text-fg">{s.market_data.name}</span>
                </span>
                {s.market_data.is_demo && <Badge tone="demo">Dati demo</Badge>}
                {!s.market_data.is_demo && s.market_data.name.includes("non ufficiale") && (
                  <Badge tone="warn" title={s.market_data.license_note}>Fonte non ufficiale</Badge>
                )}
                <span className="ml-auto hidden text-muted lg:inline">
                  DB {s.database} · Redis {s.redis}
                </span>
              </>
            )}
          </header>
          <main className="flex-1 p-4 lg:p-6">{children}</main>
          <footer className="border-t border-line px-4 py-2 text-[11px] text-muted">
            Strumento di analisi e simulazione. Nessun rendimento è garantito; i risultati passati o simulati non
            predicono quelli futuri. Le notifiche non equivalgono a ordini eseguiti.
          </footer>
        </div>
      </div>
    </SessionCtx.Provider>
  );
}

export function PhaseNotice({ phase, title, items }: { phase: number; title: string; items: string[] }) {
  return (
    <div className="mx-auto max-w-2xl rounded-lg border border-line bg-panel p-6">
      <Badge tone="accent">Fase {phase}</Badge>
      <h1 className="mt-3 text-lg font-semibold">{title}</h1>
      <p className="mt-1 text-sm text-muted">
        Questa sezione verrà attivata nella Fase {phase}. Per scelta di progetto non mostra dati simulati o
        segnaposto: comparirà quando il motore che la alimenta sarà implementato e testato.
      </p>
      <ul className="mt-4 space-y-1.5 text-sm">
        {items.map((i) => <li key={i} className="flex gap-2"><span className="text-accent">›</span>{i}</li>)}
      </ul>
    </div>
  );
}
