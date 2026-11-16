import { PhaseNotice } from "@/components/shell";

export default function AssistantPage() {
  return <PhaseNotice phase={4} title="AI Trading Assistant" items={[
    "Comitato di agenti Claude con output JSON validati e strumenti deterministici per i calcoli",
    "Risposte collegate ai dati e alle simulazioni che le giustificano",
    "Logging di modello, prompt, versione e dati usati",
    "Nessun dato inventato: se i dati mancano, la risposta lo dichiara",
  ]} />;
}
