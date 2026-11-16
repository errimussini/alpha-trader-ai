import { PhaseNotice } from "@/components/shell";

export default function LabPage() {
  return <PhaseNotice phase={5} title="Strategy Laboratory" items={[
    "Backtest con storici rettificati, dataset point-in-time e controllo di look-ahead e survivorship bias",
    "Commissioni, spread, slippage, costi valutari, gap ed esecuzione realistica degli ordini",
    "Walk-forward, bootstrap, stress test e confronto con benchmark",
    "Metriche: CAGR, drawdown, Sharpe, Sortino, profit factor, expectancy, turnover",
  ]} />;
}
