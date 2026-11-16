"use client";
import { useEffect, useRef } from "react";
import { createChart, ColorType } from "lightweight-charts";

export type LineSeries = { name: string; color: string; data: [string, number][]; area?: boolean };

export function Lines({ series, height = 300 }: { series: LineSeries[]; height?: number }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!ref.current) return;
    const c = createChart(ref.current, {
      autoSize: true,
      layout: { background: { type: ColorType.Solid, color: "transparent" }, textColor: "#8791a2", fontSize: 11 },
      grid: { vertLines: { color: "#1c222c" }, horzLines: { color: "#1c222c" } },
      rightPriceScale: { borderColor: "#222833" },
      timeScale: { borderColor: "#222833" },
    });
    for (const s of series) {
      if (!s.data.length) continue;
      const opts = { lineWidth: 2 as const, priceLineVisible: false, lastValueVisible: true, title: s.name };
      const ser = s.area
        ? c.addAreaSeries({ ...opts, lineColor: s.color, topColor: `${s.color}33`, bottomColor: `${s.color}05` })
        : c.addLineSeries({ ...opts, color: s.color });
      ser.setData(s.data.map(([time, value]) => ({ time, value })));
    }
    c.timeScale().fitContent();
    return () => c.remove();
  }, [series]);
  return <div ref={ref} style={{ height }} className="w-full" />;
}
