# Fase 3 — Trade Plan Generator e controllo del rischio

## Architettura

```
 scansione completata ──► worker ──► per ogni portafoglio simulato: top N candidati con setup e punteggio ≥ 50
                                      │
          Technical Analyst (analytics/planner.py)   ingresso · intervallo · stop tecnico · TP1/TP2 · trailing
          Risk Manager      (services/risk.py)       size, costi, cambio, esposizioni, rischio aggregato, gap
          Portfolio Manager (services/planner.py)    correlazioni a 60 giorni con le posizioni aperte
                                      ▼
                       trade_plans + risk_assessments + audit log
```

## Regole del piano

| Elemento | Regola |
|---|---|
| Ingresso | Breakout: livello del massimo a 55 giorni + 0,1 ATR (intervallo livello → +0,5 ATR). Pullback: EMA20 (intervallo −0,25 / +0,5 ATR). Trend following: chiusura (−0,5 / +0,25 ATR). |
| Stop | Prima il livello tecnico più vicino sotto l'ingresso (ultimo swing low confermato, EMA50, livello di breakout − 1 ATR), poi un buffer di 0,5 ATR. Se è a meno di 1 ATR viene allargato a 1 ATR (rumore); oltre 3,5 ATR il setup viene scartato. Lo stop non viene mai spostato per rientrare nel budget. |
| Target | TP1 = 2R, oppure appena sotto una resistenza se è più vicina; resistenza entro 1R = piano respinto. TP2 = 3–3,5R (pullback: massimo a 52 settimane se intermedio). Il 50% della posizione chiude a TP1, poi stop a pareggio e trailing a 2,5 ATR. |
| Size | Formula della specifica con slippage, commissioni, cambio, lotto, esposizione per titolo/settore, liquidità con riserva, rischio aggregato. |
| Correlazioni | ≥ 0,80 con una posizione aperta: quantità dimezzata; ≥ 0,90: piano respinto (rischio duplicato). |
| Gap | Scenario di stress = 99° percentile dei gap sfavorevoli dell'ultimo anno (minimo 2%). |
| EV e probabilità | **Non disponibili** finché il modello non è validato out-of-sample (Fase 5/7). |
| Stato | READY solo con quotazione verificata entro `MAX_QUOTE_AGE_MINUTES`, prezzo nell'intervallo, rischio approvato e dati non DEMO. Altrimenti WAIT con il motivo, o REJECTED. Scadenza dopo 3–5 giorni di borsa (job periodico). |
| Esecuzione | Un ordine simulato collegato a un piano è accettato solo se il piano è READY, non scaduto, con quantità ≤ quella approvata e stop non più basso di quello del piano. |

## Output obbligatorio (§13) — dove si trova

Identità del titolo (ticker, nome, ISIN) · timestamp e fonte del prezzo · direzione · strategia/setup · ingresso e
intervallo · TP1/TP2 · stop · quantità · importo · perdita stimata allo stop · guadagno stimato ai target ·
R/R netto · probabilità calibrata (oggi "non disponibile") · orizzonte · motivazioni pro/contro · condizioni di
invalidazione · stato READY/WAIT/REJECTED/EXPIRED · versione del modello (`ranking/x+planner/y`).
Tutto è restituito da `GET /api/opportunities/{id}` e mostrato nella pagina **Trade Detail**.

## Criteri di accettazione

| Criterio | Verifica |
|---|---|
| Livelli ordinati e stop tecnico entro 1–3,5 ATR | `test_levels_are_ordered_and_technical` |
| Nessun piano senza setup o con resistenza troppo vicina | `test_no_setup_no_plan`, `test_close_resistance_rejects` |
| Tetto del Portfolio Manager ricalcolato esattamente dal motore di rischio | `test_max_quantity_cap` |
| Dati DEMO mai READY; probabilità ed EV non inventati | `test_plans_from_demo_scan_never_ready` |
| Ordine solo su piano READY e con quantità approvata | `test_order_requires_ready_plan` |
