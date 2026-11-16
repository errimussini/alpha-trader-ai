import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export const cn = (...c: ClassValue[]) => twMerge(clsx(c));

export function money(v: string | number | null | undefined, ccy = "EUR") {
  if (v === null || v === undefined || v === "") return "—";
  return new Intl.NumberFormat("it-IT", { style: "currency", currency: ccy.length === 3 ? ccy : "EUR" }).format(Number(v));
}

export function num(v: string | number | null | undefined, digits = 2) {
  if (v === null || v === undefined || v === "") return "—";
  return new Intl.NumberFormat("it-IT", { minimumFractionDigits: digits, maximumFractionDigits: digits }).format(Number(v));
}

export function pct(v: string | number | null | undefined) {
  if (v === null || v === undefined || v === "") return "—";
  const n = Number(v);
  return `${n > 0 ? "+" : ""}${num(n)}%`;
}

export function when(iso: string | null | undefined) {
  if (!iso) return "—";
  return new Intl.DateTimeFormat("it-IT", { dateStyle: "short", timeStyle: "short" }).format(new Date(iso));
}

export const tone = (v: string | number | null | undefined) =>
  v === null || v === undefined || Number(v) === 0 ? "text-fg" : Number(v) > 0 ? "text-up" : "text-down";
