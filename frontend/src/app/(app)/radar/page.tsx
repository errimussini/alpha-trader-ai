import { PhaseNotice } from "@/components/shell";

export default function RadarPage() {
  return <PhaseNotice phase={2} title="Opportunity Radar" items={[
    "Scansione automatica dell'universo USA ed Europa con filtri di liquidità, capitalizzazione e spread",
    "Indicatori tecnici (EMA, RSI, MACD, ATR, ADX, Bollinger, momentum, forza relativa)",
    "Punteggio composito 0–100 con pesi iniziali da validare statisticamente",
    "Filtri per mercato, settore, rischio e orizzonte operativo",
  ]} />;
}
