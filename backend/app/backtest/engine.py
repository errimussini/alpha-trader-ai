"""Simulatore di portafoglio giornaliero, senza look-ahead (specifica §8).

Sequenza per ogni giorno di borsa D:
1. uscite delle posizioni aperte sulla barra di D (stop prima dei target: ipotesi prudente);
   - apertura sotto lo stop  -> uscita all'apertura (gap: perdita oltre lo stop);
   - minimo <= stop           -> uscita allo stop meno slippage;
   - massimo >= TP1/TP2       -> uscite parziali (TP1 chiude la quota del piano, poi stop a pareggio e trailing);
   - time stop: nessun TP1 entro metà orizzonte -> uscita in chiusura;
2. ingressi: gli ordini LIMIT emessi alla chiusura di D-1 si eseguono se il minimo di D tocca il limite,
   al prezzo min(apertura, limite) più slippage; annullati se l'apertura è sotto lo stop o se scadono;
3. valorizzazione in chiusura (cambio del giorno);
4. segnali alla chiusura di D usando SOLO dati fino a D (indicatori causali, swing confermati, regime a D)
   e dimensionamento con il motore di rischio deterministico (services/risk.py).

Costi: commissioni fisse e percentuali, slippage in bps su ogni esecuzione, costo di cambio in bps.
Il punteggio usa solo fattori disponibili point-in-time (trend/momentum, setup, regime e forza relativa,
liquidità/volatilità): fondamentali, catalizzatori e sentiment sono esclusi per evitare look-ahead.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from decimal import Decimal

import pandas as pd

from app.analytics import indicators, regime as regime_mod, setups
from app.analytics.planner import HORIZON_DAYS, VALIDITY_DAYS, compute_levels
from app.analytics.scoring import _vol_score
from app.services.risk import Costs, PortfolioContext, RiskLimits, SizingRequest, compute_position_size

ENGINE_VERSION = "backtest/1.0.0"
BT_WEIGHTS = {"trend_momentum": 25, "setup": 20, "macro_sector": 15, "liquidity_vol": 10}


@dataclass
class BTParams:
    initial_capital: float = 10000.0
    min_score: float = 60.0
    max_positions: int = 8
    risk_per_trade_pct: float = 0.5
    max_aggregate_risk_pct: float = 3.0
    max_position_exposure_pct: float = 15.0
    max_sector_exposure_pct: float = 30.0
    min_cash_reserve_pct: float = 5.0
    commission_per_order: float = 2.0
    commission_pct: float = 0.0
    slippage_bps: float = 5.0
    fx_cost_bps: float = 10.0
    min_price: float = 5.0
    min_traded_value: float = 5e6
    setups: tuple[str, ...] = (setups.BREAKOUT, setups.PULLBACK, setups.TREND)
    trailing: bool = True
    time_stop: bool = True
    extra_gap_pct: float = 0.0  # stress: peggiora ogni uscita in stop di questa percentuale

    def to_dict(self) -> dict:
        d = asdict(self)
        d["setups"] = list(self.setups)
        return d


@dataclass
class InstrumentData:
    instrument_id: int
    ticker: str
    sector: str | None
    currency: str
    bars: pd.DataFrame          # open high low close adj_close volume, indice DatetimeIndex
    ind: pd.DataFrame           # indicators.compute_all (causale)


@dataclass
class MarketData:
    instruments: dict[int, InstrumentData]
    regime_closes: dict[str, pd.Series]               # benchmark per il regime
    fx: dict[str, pd.Series]                          # valuta -> unità di valuta per 1 unità di base
    base_currency: str = "EUR"
    is_demo: bool = False
    notes: list[str] = field(default_factory=list)

    def calendar(self) -> pd.DatetimeIndex:
        idx = pd.DatetimeIndex([])
        for d in self.instruments.values():
            idx = idx.union(d.bars.index)
        return idx.sort_values()


@dataclass
class _Pos:
    iid: int
    setup: str
    regime: str
    qty: float
    entry_date: pd.Timestamp
    entry_price: float
    stop: float
    initial_stop: float
    tp1: float
    tp2: float | None
    tp1_frac: float
    atr: float
    horizon: int
    risk_base: float
    score: float
    fx_entry: float = 1.0
    entry_fee_base: float = 0.0
    fees: float = 0.0
    realized: float = 0.0
    tp1_hit: bool = False
    max_close: float = 0.0
    days: int = 0
    exits: list = field(default_factory=list)


@dataclass
class _Order:
    iid: int
    setup: str
    regime: str
    limit: float
    stop: float
    tp1: float
    tp2: float | None
    tp1_frac: float
    qty: float
    atr: float
    horizon: int
    expires_after: int
    score: float
    risk_base: float
    age: int = 0


def regime_series(md: MarketData, dates: pd.DatetimeIndex) -> pd.Series:
    """Regime a ciascuna data con i soli dati disponibili fino a quella data."""
    out = {}
    for d in dates:
        states = []
        for t, c in md.regime_closes.items():
            cc = c.loc[:d]
            st = regime_mod.benchmark_state(t, cc, "backtest") if len(cc) >= 260 else None
            if st:
                states.append(st)
        out[d] = regime_mod.classify(states).regime if states else "UNKNOWN"
    return pd.Series(out)


def _rank_panel(md: MarketData, col: str) -> pd.DataFrame:
    panel = pd.DataFrame({iid: d.ind[col] for iid, d in md.instruments.items() if col in d.ind})
    return panel.rank(axis=1, pct=True) * 100


class Simulator:
    def __init__(self, md: MarketData, params: BTParams, regimes: pd.Series | None = None):
        self.md, self.p = md, params
        self.cal = md.calendar()
        self.regimes = regimes if regimes is not None else regime_series(md, self.cal)
        self.ranks = {c: _rank_panel(md, c) for c in ("mom_12_1", "mom_6m", "mom_3m", "rs_3m", "traded_value20")}
        self.costs = Costs(Decimal(str(params.commission_per_order)), Decimal(str(params.commission_pct)),
                           Decimal(str(params.slippage_bps)), Decimal(str(params.fx_cost_bps)))
        self.limits = RiskLimits(*(Decimal(str(getattr(params, k))) for k in (
            "risk_per_trade_pct", "max_aggregate_risk_pct", "max_position_exposure_pct",
            "max_sector_exposure_pct", "min_cash_reserve_pct")))
        self.slip = params.slippage_bps / 10000

    # --------------------------------------------------------------- utilità
    def _fx(self, ccy: str, d: pd.Timestamp) -> float | None:
        if ccy == self.md.base_currency:
            return 1.0
        s = self.md.fx.get(ccy)
        if s is None:
            return None
        v = s.loc[:d]
        return float(v.iloc[-1]) if len(v) else None

    def _fee(self, notional_ccy: float) -> float:
        return self.p.commission_per_order + notional_ccy * self.p.commission_pct / 100

    def _fx_cost(self, ccy: str) -> float:
        return 0.0 if ccy == self.md.base_currency else self.p.fx_cost_bps / 10000

    def score(self, iid: int, i: int, d: pd.Timestamp, setup_q: float | None, reg: str) -> tuple[float | None, dict]:
        f = self.md.instruments[iid].ind.iloc[i]
        r = lambda c: self.ranks[c].at[d, iid] if iid in self.ranks[c].columns and d in self.ranks[c].index else float("nan")  # noqa: E731
        moms = [x for x in (r("mom_12_1"), r("mom_6m"), r("mom_3m")) if x == x]
        align = None
        if all(f.get(k) == f.get(k) for k in ("close", "ema20", "ema50", "ema200")):
            align = (40 if f.close > f.ema200 else 0) + (35 if f.ema50 > f.ema200 else 0) + (25 if f.ema20 > f.ema50 else 0)
        tm = 0.6 * (sum(moms) / len(moms)) + 0.4 * align if moms and align is not None else None
        reg_score = regime_mod.REGIME_SCORE.get(reg) if reg != "UNKNOWN" else None
        rs = r("rs_3m")
        ms = None if reg_score is None else (0.6 * reg_score + 0.4 * rs if rs == rs else reg_score)
        vs = _vol_score(float(f.vol20) if f.vol20 == f.vol20 else None)
        tv = r("traded_value20")
        lv = 0.5 * tv + 0.5 * vs if tv == tv and vs is not None else None
        fac = {"trend_momentum": tm, "setup": setup_q, "macro_sector": ms, "liquidity_vol": lv}
        avail = {k: v for k, v in fac.items() if v is not None}
        if sum(BT_WEIGHTS[k] for k in avail) / sum(BT_WEIGHTS.values()) < 0.70:
            return None, fac
        return sum(BT_WEIGHTS[k] * v for k, v in avail.items()) / sum(BT_WEIGHTS[k] for k in avail), fac

    # --------------------------------------------------------------- simulazione
    def run(self, start: pd.Timestamp | None = None, end: pd.Timestamp | None = None) -> dict:
        p, md = self.p, self.md
        cal = self.cal if start is None else self.cal[self.cal >= start]
        if end is not None:
            cal = cal[cal <= end]
        cash = p.initial_capital
        pos: dict[int, _Pos] = {}
        orders: dict[int, _Order] = {}
        trades: list[dict] = []
        equity, exposure = {}, {}
        traded = 0.0
        idx = {iid: {d: k for k, d in enumerate(x.bars.index)} for iid, x in md.instruments.items()}
        skipped_fx = set()

        def close_part(ps: _Pos, qty: float, price: float, d, reason: str):
            nonlocal cash, traded
            inst = md.instruments[ps.iid]
            fx = self._fx(inst.currency, d) or 1.0
            gross = qty * price
            fee = self._fee(gross)
            proceeds = (gross - fee) / fx * (1 - self._fx_cost(inst.currency))
            cost = qty * ps.entry_price / ps.fx_entry * (1 + self._fx_cost(inst.currency))
            cash += proceeds
            traded += gross / fx
            ps.realized += proceeds - cost
            ps.fees += fee / fx
            ps.qty -= qty
            ps.exits.append({"date": str(d.date()), "qty": qty, "price": round(price, 4), "reason": reason})
            if ps.qty <= 1e-9:
                pnl = ps.realized - ps.entry_fee_base
                trades.append({
                    "ticker": inst.ticker, "instrument_id": ps.iid, "sector": inst.sector, "setup": ps.setup,
                    "regime": ps.regime, "score": round(float(ps.score), 2), "entry_date": str(ps.entry_date.date()),
                    "entry_price": round(ps.entry_price, 4), "stop": round(ps.initial_stop, 4), "tp1": round(ps.tp1, 4),
                    "tp2": round(ps.tp2, 4) if ps.tp2 else None, "qty": float(sum(e["qty"] for e in ps.exits)),
                    "exit_date": str(d.date()), "exit_reason": reason, "exits": ps.exits, "pnl_base": round(pnl, 2),
                    "r_multiple": round(pnl / ps.risk_base, 3) if ps.risk_base > 0 else None,
                    "holding_days": ps.days, "fees_base": round(ps.fees + ps.entry_fee_base, 2),
                })
                del pos[ps.iid]

        for d in cal:
            reg = self.regimes.get(d, "UNKNOWN")
            # 1) uscite
            for iid in list(pos):
                ps = pos[iid]
                k = idx[iid].get(d)
                if k is None:
                    continue
                b = md.instruments[iid].bars.iloc[k]
                ps.days += 1
                o, h, lo, c = float(b.open), float(b.high), float(b.low), float(b.close)
                stop_slip = 1 - self.slip - p.extra_gap_pct / 100
                if o <= ps.stop:
                    close_part(ps, ps.qty, o * stop_slip, d, "gap_stop" if not ps.tp1_hit else "gap_trailing")
                    continue
                if lo <= ps.stop:
                    close_part(ps, ps.qty, ps.stop * stop_slip, d, "stop" if not ps.tp1_hit else "trailing_stop")
                    continue
                if not ps.tp1_hit and h >= ps.tp1:
                    q1 = math.floor(ps.qty * ps.tp1_frac) if ps.tp2 else ps.qty
                    q1 = ps.qty if q1 <= 0 or q1 >= ps.qty else q1
                    close_part(ps, q1, max(o, ps.tp1) * (1 - self.slip), d, "tp1")
                    if iid not in pos:
                        continue
                    ps.tp1_hit, ps.stop, ps.max_close = True, max(ps.stop, ps.entry_price), c
                if ps.tp1_hit and ps.tp2 and h >= ps.tp2:
                    close_part(ps, ps.qty, max(o, ps.tp2) * (1 - self.slip), d, "tp2")
                    continue
                if ps.tp1_hit and p.trailing:
                    ps.max_close = max(ps.max_close, c)
                    atr_now = md.instruments[iid].ind["atr14"].iloc[k]
                    if atr_now == atr_now:
                        ps.stop = max(ps.stop, ps.max_close - 2.5 * float(atr_now))
                if p.time_stop and not ps.tp1_hit and ps.days >= ps.horizon // 2:
                    close_part(ps, ps.qty, c * (1 - self.slip), d, "time_stop")
                    continue
                if ps.days >= ps.horizon:
                    close_part(ps, ps.qty, c * (1 - self.slip), d, "horizon")
            # 2) ingressi
            for iid in list(orders):
                od = orders[iid]
                k = idx[iid].get(d)
                if k is None:
                    continue
                od.age += 1
                b = md.instruments[iid].bars.iloc[k]
                o, lo = float(b.open), float(b.low)
                inst = md.instruments[iid]
                fx = self._fx(inst.currency, d)
                if o <= od.stop or fx is None:
                    del orders[iid]
                    continue
                if lo <= od.limit:
                    price = min(o, od.limit) * (1 + self.slip)
                    gross = od.qty * price
                    fee = self._fee(gross)
                    need = (gross + fee) / fx * (1 + self._fx_cost(inst.currency))
                    reserve = self._equity(cash, pos, d, idx) * p.min_cash_reserve_pct / 100
                    if need <= cash - reserve:
                        cash -= need
                        traded += gross / fx
                        pos[iid] = _Pos(iid, od.setup, od.regime, od.qty, d, price, od.stop, od.stop, od.tp1, od.tp2,
                                        od.tp1_frac, od.atr, od.horizon, od.risk_base, od.score, fx_entry=fx,
                                        entry_fee_base=fee / fx, max_close=price)
                        if lo <= od.stop:  # stop toccato nello stesso giorno dell'ingresso: ipotesi prudente
                            close_part(pos[iid], od.qty, od.stop * (1 - self.slip - p.extra_gap_pct / 100), d, "stop")
                    del orders[iid]
                elif od.age >= od.expires_after:
                    del orders[iid]
            # 3) valorizzazione
            eq = self._equity(cash, pos, d, idx)
            equity[d] = eq
            exposure[d] = (eq - cash) / eq if eq > 0 else 0.0
            # 4) segnali in chiusura
            slots = p.max_positions - len(pos) - len(orders)
            if slots <= 0:
                continue
            cands = []
            for iid, inst in md.instruments.items():
                if iid in pos or iid in orders:
                    continue
                k = idx[iid].get(d)
                if k is None or k < 260:
                    continue
                f = inst.ind.iloc[k]
                if not (f.close >= p.min_price and f.traded_value20 == f.traded_value20):
                    continue
                fx = self._fx(inst.currency, d)
                if fx is None:
                    skipped_fx.add(inst.ticker)
                    continue
                if f.traded_value20 / fx < p.min_traded_value:
                    continue
                sr = setups.detect(inst.ind.iloc[k - 3:k + 1], inst.bars["low"].iloc[k - 2:k + 1])
                if sr.setup not in p.setups:
                    continue
                sc, _ = self.score(iid, k, d, sr.quality, reg)
                if sc is None or sc < p.min_score:
                    continue
                cands.append((sc, iid, k, sr.setup, fx))
            cands.sort(reverse=True)
            open_risk = sum(max(0.0, (x.entry_price - x.stop) * x.qty / (self._fx(md.instruments[x.iid].currency, d) or 1))
                            for x in pos.values()) + sum(o.risk_base for o in orders.values())
            for sc, iid, k, setup, fx in cands[:slots * 3]:
                if slots <= 0:
                    break
                inst = md.instruments[iid]
                win = inst.bars.iloc[max(0, k - 300):k + 1]
                feats = {c: float(v) if v == v else float("nan") for c, v in inst.ind.iloc[k].items()}
                feats["support"], feats["resistance"] = indicators.support_resistance(win["high"], win["low"], win["close"])
                lv = compute_levels(setup, feats, win)
                if not lv.ok:
                    continue
                same_sec = sum(self._value(x, d, idx) for x in pos.values()
                               if inst.sector and md.instruments[x.iid].sector == inst.sector)
                ctx = PortfolioContext(Decimal(str(round(eq, 2))), Decimal(str(round(cash, 2))), Decimal(str(round(open_risk, 2))),
                                       Decimal("0"), Decimal(str(round(same_sec, 2))))
                try:
                    res = compute_position_size(SizingRequest(
                        Decimal(str(lv.entry_high)), Decimal(str(lv.stop)), Decimal(str(fx)),
                        take_profit_1=Decimal(str(lv.tp1)), take_profit_2=Decimal(str(lv.tp2)) if lv.tp2 else None,
                        tp1_close_fraction=Decimal(str(lv.tp1_fraction))), ctx, self.limits, self.costs)
                except ValueError:
                    continue
                if not res.approved or res.reward_risk_net is None or res.reward_risk_net < 1:
                    continue
                orders[iid] = _Order(iid, setup, reg, lv.entry_high, lv.stop, lv.tp1, lv.tp2, lv.tp1_fraction,
                                     float(res.quantity), float(feats["atr14"]), HORIZON_DAYS[setup],
                                     VALIDITY_DAYS[setup], sc, float(res.est_loss_at_stop_base))
                open_risk += float(res.est_loss_at_stop_base)
                slots -= 1

        # chiusura forzata a fine periodo (marcata come tale)
        if len(cal):
            last = cal[-1]
            for iid in list(pos):
                k = idx[iid].get(last)
                px = float(md.instruments[iid].bars["close"].iloc[k]) if k is not None else pos[iid].entry_price
                close_part(pos[iid], pos[iid].qty, px * (1 - self.slip), last, "end_of_test")
        eq_s = pd.Series(equity, dtype=float)
        notes = list(md.notes)
        if skipped_fx:
            notes.append(f"Strumenti esclusi per cambio storico non disponibile: {', '.join(sorted(skipped_fx)[:10])}")
        return {"equity": eq_s, "exposure": pd.Series(exposure, dtype=float), "trades": trades,
                "traded_notional": traded, "notes": notes}

    def _value(self, ps: _Pos, d, idx) -> float:
        inst = self.md.instruments[ps.iid]
        k = idx[ps.iid].get(d)
        px = float(inst.bars["close"].iloc[k]) if k is not None else float(inst.bars["close"].loc[:d].iloc[-1])
        return ps.qty * px / (self._fx(inst.currency, d) or 1.0)

    def _equity(self, cash: float, pos: dict, d, idx) -> float:
        return cash + sum(self._value(ps, d, idx) for ps in pos.values())


def prepare(instruments: list[tuple], bench_close: dict[str, pd.Series], fx: dict[str, pd.Series], base: str = "EUR",
            is_demo: bool = False, notes: list[str] | None = None) -> MarketData:
    """instruments: (id, ticker, settore, valuta, barre, serie del benchmark per la forza relativa | None)."""
    out = {}
    for iid, ticker, sector, ccy, df, rs_bench in instruments:
        df = df[["open", "high", "low", "close", "adj_close", "volume"]].astype(float).dropna(subset=["open", "high", "low", "close"])
        if len(df) < 300:
            continue
        out[iid] = InstrumentData(iid, ticker, sector, ccy, df, indicators.compute_all(df, rs_bench))
    return MarketData(out, bench_close, fx, base, is_demo, notes or [])
