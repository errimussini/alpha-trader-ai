"""Valutazione prospettica di un piano operativo sulle barre giornaliere SUCCESSIVE alla sua creazione.

Funzione pura e deterministica (nessun accesso al database), regole conservative e documentate:

- Ingresso: ordine LIMIT al prezzo di ingresso del piano, valido fino a `valid_until`. È eseguito sulla prima
  barra con minimo <= ingresso, al prezzo min(apertura, ingresso). Se prima dell'ingresso una barra tocca lo
  stop (minimo <= stop) o apre sotto lo stop, il setup è INVALIDATED.
- Uscita: dalla barra di ingresso inclusa. Se nella stessa barra sono toccati sia lo stop sia TP1 vale lo stop
  (ipotesi peggiore). Stop eseguito a min(apertura, stop) (gap), TP1 a max(apertura, TP1) solo se l'apertura
  è sopra TP1, altrimenti a TP1. Dopo `horizon_days` barre dall'ingresso: TIMEOUT alla chiusura.
- R lordo = (uscita - ingresso) / (ingresso - stop). R netto sottrae commissioni (ingresso + uscita) e
  slippage (ingresso + uscita) stimati con i parametri di costo correnti.
- Barre insufficienti: PENDING (verrà ripreso al ciclo successivo).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

OUTCOME_VERSION = "prospective/1.0.0"
CLOSED = ("WIN", "LOSS", "TIMEOUT")
FINAL = CLOSED + ("NOT_TRIGGERED", "INVALIDATED", "NOT_EVALUABLE")


@dataclass(frozen=True)
class Bar:
    d: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal


@dataclass(frozen=True)
class PlanSpec:
    created_for: date          # ultima data dei dati usati dal piano: si valutano solo barre successive
    valid_until: date
    entry: Decimal
    stop: Decimal
    tp1: Decimal
    horizon_days: int
    quantity: Decimal          # per le commissioni in R (0 = solo slippage)
    fx_rate: Decimal = Decimal("1")  # unità di valuta strumento per 1 unità di valuta base


@dataclass
class Result:
    outcome: str = "PENDING"
    entry_date: date | None = None
    entry_price: Decimal | None = None
    exit_date: date | None = None
    exit_price: Decimal | None = None
    r_gross: Decimal | None = None
    r_net: Decimal | None = None
    mfe_r: Decimal | None = None
    mae_r: Decimal | None = None
    bars_evaluated: int = 0
    last_bar_date: date | None = None
    notes: list[str] = field(default_factory=list)


def _q(x: Decimal) -> Decimal:
    return x.quantize(Decimal("0.0001"))


def evaluate(spec: PlanSpec, bars: list[Bar], *, today: date, commission_per_order: Decimal,
             slippage_bps: Decimal) -> Result:
    res = Result()
    risk = spec.entry - spec.stop
    if risk <= 0 or spec.tp1 <= spec.entry:
        res.outcome = "NOT_EVALUABLE"  # es. piano REJECTED senza livelli operativi validi
        res.notes.append("livelli non validi (stop >= ingresso o TP1 <= ingresso): piano non valutabile")
        return res
    future = sorted((b for b in bars if b.d > spec.created_for), key=lambda b: b.d)
    res.bars_evaluated = len(future)
    res.last_bar_date = future[-1].d if future else None

    i_entry = None
    for i, b in enumerate(future):
        if b.d > spec.valid_until:
            break
        if b.open <= spec.stop:
            res.outcome = "INVALIDATED"
            res.exit_date = b.d
            res.notes.append(f"apertura {b.open} sotto lo stop prima dell'ingresso")
            return res
        if b.low <= spec.entry:  # (low <= stop implica low <= ingresso: lo stop nella stessa barra è gestito sotto)
            i_entry = i
            res.entry_date, res.entry_price = b.d, min(b.open, spec.entry)
            break
    if i_entry is None:
        if future and future[-1].d >= spec.valid_until:
            res.outcome = "NOT_TRIGGERED"
        elif (today - spec.valid_until).days > 7:
            res.outcome = "NOT_TRIGGERED"
            res.notes.append("barre mancanti nella finestra di validità: esito basato sui dati disponibili")
        return res

    entry = res.entry_price
    risk = entry - spec.stop
    if risk <= 0:  # ingresso in gap fin sotto lo stop: già escluso sopra, per sicurezza
        res.outcome = "INVALIDATED"
        return res
    mfe, mae = Decimal("0"), Decimal("0")
    held = future[i_entry: i_entry + spec.horizon_days]
    exit_px = exit_d = None
    for b in held:
        mfe = max(mfe, (b.high - entry) / risk)
        mae = min(mae, (b.low - entry) / risk)
        if b.low <= spec.stop:
            exit_px, exit_d, res.outcome = min(b.open, spec.stop), b.d, "LOSS"
            if b.high >= spec.tp1:
                res.notes.append(f"{b.d}: stop e TP1 nella stessa barra, assunto lo stop")
            break
        if b.high >= spec.tp1:
            exit_px, exit_d, res.outcome = max(b.open, spec.tp1), b.d, "WIN"
            break
    if exit_px is None:
        if len(held) >= spec.horizon_days:
            last = held[-1]
            exit_px, exit_d, res.outcome = last.close, last.d, "TIMEOUT"
        else:
            res.mfe_r, res.mae_r = _q(mfe), _q(mae)
            return res  # ancora in corso
    res.exit_date, res.exit_price = exit_d, exit_px
    gross = (exit_px - entry) / risk
    slip = (entry + exit_px) * slippage_bps / Decimal("10000")
    comm = (2 * commission_per_order * spec.fx_rate / spec.quantity) if spec.quantity > 0 else Decimal("0")
    res.r_gross, res.r_net = _q(gross), _q((exit_px - entry - slip - comm) / risk)
    res.mfe_r, res.mae_r = _q(mfe), _q(mae)
    if spec.quantity <= 0:
        res.notes.append("quantità nulla: R netto con il solo slippage")
    return res
