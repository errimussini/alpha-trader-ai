# ALPHA TRADER AI

Piattaforma di analisi e supporto al trading per azioni ed ETF liquidi di USA ed Europa.
Modalità disponibili: **Research** (sola analisi) e **Paper Trading** (esecuzione simulata).
La modalità Live non esiste in questa release.

> Strumento di analisi e simulazione. Nessun rendimento è garantito. I risultati passati o
> simulati non predicono quelli futuri. Le notifiche non equivalgono a ordini eseguiti.

## Stato del progetto

| Fase | Contenuto | Stato |
|---|---|---|
| 1 — Foundation | Database, autenticazione, adapter dati, PaperBroker, motore di rischio, frontend | **completata** |
| 2 — Scanner | Universo, filtri, indicatori, regime di mercato, ranking 0–100, worker | **completata** |
| 3 — Trade Planner | Ingressi, target, stop, dimensionamento completo, correlazioni | da fare |
| 4 — AI Investment Committee | Agenti Claude con output strutturati | da fare |
| 5 — Backtest | Motore di test con controllo dei bias | da fare |
| 6 — Paper Trading | Monitoraggio posizioni, ordini aperti, notifiche, storico equity | da fare |
| 7 — Validazione | Test prospettici e confronto con benchmark | da fare |
| 8 — Integrazioni live | Solo architettura, con conferma umana per ogni ordine | da fare |

Dettagli: [Fase 1](docs/fase-1-foundation.md) · [Fase 2](docs/fase-2-scanner.md).

## Avvio rapido (Docker)

```bash
cp .env.example .env        # imposta JWT_SECRET e la password dell'admin
docker compose up --build
```

- Web app: http://localhost:3000 (accedi con `BOOTSTRAP_ADMIN_EMAIL` / `BOOTSTRAP_ADMIN_PASSWORD`)
- API e documentazione interattiva: http://localhost:8000/docs

Primi passi nell'interfaccia:
1. **Mercati e dati** → *Benchmark* per scaricare gli ETF di riferimento, oppure inserisci i ticker
   (es. `AAPL.US ENI.MI SAP.DE`).
2. **Sistema** → passa a *Paper Trading* per abilitare gli ordini simulati.
3. **Opportunity Radar** → *Carica lista iniziale* (151 titoli ed ETF USA/Europa), poi *Avvia scansione*:
   il worker scarica i dati e produce la classifica.
4. Seleziona uno strumento, inserisci ingresso e stop: il Risk Manager calcola la quantità
   compatibile con i limiti; da lì puoi inviare un ordine simulato.

## Avvio in sviluppo (senza Docker)

```bash
# backend (richiede PostgreSQL e, opzionale, Redis)
cd backend
pip install -e ".[dev]"
cp ../.env.example .env     # aggiungi DATABASE_URL=postgresql+psycopg://alpha:alpha@localhost:5432/alpha_trader
alembic upgrade head
uvicorn app.main:app --reload

python -m app.worker        # in un secondo terminale: scansioni in coda e programmate

# frontend
cd frontend
npm install
NEXT_PUBLIC_API_URL=http://localhost:8000 npm run dev
```

Creare utenti dalla riga di comando: `python -m app.cli create-user nome@dominio.it --role analyst`.

## Fonte dei dati

| Provider | Costo | Copertura | Note |
|---|---|---|---|
| `yahoo` (predefinito) | gratuito, senza chiave | USA + Europa, storici rettificati, fondamentali, utili, notizie | **non ufficiale**: solo uso personale, può cambiare senza preavviso, quotazioni EU ritardate ~15-20 min |
| `demo` | — | 5 strumenti fittizi `DEMOn.DEMO` | dati **sintetici**, marcati ovunque come DEMO |
| `csv` | — | file forniti dall'utente in `backend/data/csv` | vedi `app/adapters/market_data/csv_adapter.py` |
| `eodhd` | a pagamento | USA + Europa professionale | pronto ma disattivato; richiede `EODHD_API_KEY` |

Il provider si cambia con `MARKET_DATA_PROVIDER` senza toccare il resto del codice. Se i dati mancano,
sono incoerenti o più vecchi di `MAX_QUOTE_AGE_MINUTES`, ordini e raccomandazioni vengono sospesi.
Fuori dagli orari di mercato le quotazioni risultano quindi "obsolete" e gli ordini simulati sono bloccati:
è il comportamento voluto.

Convenzione ticker: `SIMBOLO.BORSA` — `US`, `MI` (Milano), `DE` (Xetra), `PA`, `AS`, `MC`, `L`, `SW`, ...

## Test

```bash
cd backend && python -m pytest -q      # richiede PostgreSQL (database alpha_trader_test)
cd frontend && npm run typecheck && npm run build
```

## Struttura

```
backend/
  app/
    adapters/market_data/   yahoo, eodhd, csv, demo (interfaccia MarketDataAdapter)
    adapters/broker/        BrokerAdapter astratto + PaperBroker
    api/routes/             auth, system, market, trading, portfolio
    services/               risk (deterministico), paper_trading, portfolio, safety, audit, market_data
    models/entities.py      schema completo (19 entità della specifica + utenti e impostazioni)
  alembic/                  migrazioni (incluso trigger che rende immutabile l'audit log)
  tests/                    test automatici
    analytics/              indicatori, setup, regime di mercato, scoring (puri e deterministici)
    worker.py               coda dei job e scheduler
frontend/                   Next.js 15 + TypeScript + Tailwind, tema scuro, TradingView Lightweight Charts
docs/                       architettura e criteri di accettazione per fase
```
