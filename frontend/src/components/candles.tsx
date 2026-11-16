"use client";
import { useEffect, useRef } from "react";
import { createChart, ColorType, type IChartApi } from "lightweight-charts";
import type { Bar } from "@/lib/api";

export type PriceLine = { price: number; label: string; color: string };

export function Candles({ bars, lines = [] }: { bars: Bar[]; lines?: PriceLine[] }) {
  const ref = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);

  useEffect(() => {
    if (!ref.current) return;
    const c = createChart(ref.current, {
      autoSize: true,
      layout: { background: { type: ColorType.Solid, color: "transparent" }, textColor: "#8791a2", fontSize: 11 },
      grid: { vertLines: { color: "#1c222c" }, horzLines: { color: "#1c222c" } },
      rightPriceScale: { borderColor: "#222833" },
      timeScale: { borderColor: "#222833" },
      crosshair: { mode: 0 },
    });
    chart.current = c;
    const s = c.addCandlestickSeries({
      upColor: "#26c485", downColor: "#f0535c", borderVisible: false, wickUpColor: "#26c485", wickDownColor: "#f0535c",
    });
    s.setData(bars.map((b) => ({
      time: b.bar_date, open: Number(b.open), high: Number(b.high), low: Number(b.low), close: Number(b.close),
    })));
    const v = c.addHistogramSeries({ priceScaleId: "vol", priceFormat: { type: "volume" }, color: "#2a3342" });
    c.priceScale("vol").applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
    v.setData(bars.filter((b) => b.volume !== null).map((b) => ({ time: b.bar_date, value: Number(b.volume) })));
    lines.forEach((l) => s.createPriceLine({ price: l.price, color: l.color, lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: l.label }));
    c.timeScale().fitContent();
    return () => { c.remove(); chart.current = null; };
  }, [bars, lines]);

  return <div ref={ref} className="h-[380px] w-full" />;
}
