"""Motore DETERMINISTICO di risk management e dimensionamento della posizione.

Formula di base (specifica §6.D):
    Risk Budget       = Capitale operativo × Rischio % per trade
    Quantità teorica  = Risk Budget / |Prezzo ingresso − Prezzo stop|

La quantità è poi ridotta per: conversione valutaria, lotto minimo, commissioni, slippage,
esposizione massima per titolo e per settore, liquidità disponibile (con riserva), rischio aggregato.
La perdita allo stop è una STIMA: in caso di gap il prezzo di uscita può essere peggiore.

Lo stop deve essere scelto PRIMA su base tecnica; questo motore dimensiona la posizione di conseguenza
e non sposta mai lo stop per rientrare nel budget.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

ENGINE_VERSION = "risk-engine/1.0.0"
_M = Decimal("0.01")
_HUNDRED = Decimal("100")


@dataclass(frozen=True)
class RiskLimits:
    risk_per_trade_pct: Decimal
    max_aggregate_risk_pct: Decimal
    max_position_exposure_pct: Decimal
    max_sector_exposure_pct: Decimal
    min_cash_reserve_pct: Decimal


@dataclass(frozen=True)
class Costs:
    commission_per_order: Decimal
    commission_pct: Decimal
    slippage_bps: Decimal
    fx_cost_bps: Decimal = Decimal("0")

    def fee(self, notional_ccy: Decimal) -> Decimal:
        return self.commission_per_order + notional_ccy * self.commission_pct / _HUNDRED


@dataclass(frozen=True)
class PortfolioContext:
    equity_base: Decimal
    cash_base: Decimal
    open_risk_base: Decimal
    exposure_same_instrument_base: Decimal = Decimal("0")
    exposure_same_sector_base: Decimal = Decimal("0")


@dataclass(frozen=True)
class SizingRequest:
    entry_price: Decimal
    stop_price: Decimal
    fx_rate: Decimal = Decimal("1")  # unità di valuta strumento per 1 unità di valuta base
    lot_size: Decimal = Decimal("1")
    take_profit_1: Decimal | None = None
    take_profit_2: Decimal | None = None
    tp1_close_fraction: Decimal = Decimal("0.5")
    gap_stress_pct: Decimal = Decimal("3")  # scenario: uscita X% sotto lo stop
    max_quantity: Decimal | None = None      # tetto esterno (es. riduzione del Portfolio Manager)


@dataclass
class SizingResult:
    approved: bool
    quantity: Decimal
    theoretical_quantity: Decimal
    risk_budget_base: Decimal
    per_unit_risk_base: Decimal
    notional_base: Decimal
    entry_fee_base: Decimal
    est_loss_at_stop_base: Decimal
    est_loss_gap_scenario_base: Decimal
    est_gain_tp1_base: Decimal | None
    est_gain_tp2_base: Decimal | None
    reward_risk_net: Decimal | None
    exposure_pct: Decimal
    aggregate_risk_after_pct: Decimal
    binding_constraint: str
    blocking_reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    engine_version: str = ENGINE_VERSION

    def to_dict(self) -> dict:
        d = asdict(self)
        return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in d.items()}


def _floor_lot(q: Decimal, lot: Decimal) -> Decimal:
    if q <= 0:
        return Decimal("0")
    return (q / lot).to_integral_value(ROUND_DOWN) * lot


def compute_position_size(req: SizingRequest, ctx: PortfolioContext, limits: RiskLimits, costs: Costs) -> SizingResult:
    blocking: list[str] = []
    warnings: list[str] = [
        "La perdita allo stop è una stima, non una perdita massima garantita (gap, sospensioni, liquidità).",
    ]
    if req.entry_price <= 0 or req.stop_price <= 0:
        raise ValueError("Prezzi di ingresso e stop devono essere positivi")
    if req.stop_price >= req.entry_price:
        raise ValueError("Per un'operazione long lo stop deve essere inferiore al prezzo di ingresso")
    if req.fx_rate <= 0 or req.lot_size <= 0:
        raise ValueError("Cambio e lotto minimo devono essere positivi")

    slip = costs.slippage_bps / Decimal("10000")
    fx_cost = costs.fx_cost_bps / Decimal("10000")
    fx = req.fx_rate

    risk_budget = ctx.equity_base * limits.risk_per_trade_pct / _HUNDRED
    entry_eff = req.entry_price * (1 + slip)
    stop_eff = req.stop_price * (1 - slip)
    per_unit_risk_ccy = entry_eff - stop_eff
    per_unit_risk_base = per_unit_risk_ccy / fx * (1 + fx_cost)

    def loss_at_stop(q: Decimal) -> Decimal:
        if q == 0:
            return Decimal("0")
        fees = (costs.fee(entry_eff * q) + costs.fee(stop_eff * q)) / fx
        return q * per_unit_risk_base + fees

    theoretical = risk_budget / per_unit_risk_base if per_unit_risk_base > 0 else Decimal("0")
    qty = _floor_lot(theoretical, req.lot_size)
    binding = "risk_budget"
    while qty > 0 and loss_at_stop(qty) > risk_budget:
        qty -= req.lot_size
    qty = max(qty, Decimal("0"))

    def notional_base(q: Decimal) -> Decimal:
        return q * entry_eff / fx * (1 + fx_cost)

    # esposizione massima per titolo
    max_pos = ctx.equity_base * limits.max_position_exposure_pct / _HUNDRED - ctx.exposure_same_instrument_base
    if notional_base(qty) > max_pos:
        qty = _floor_lot(max(max_pos, Decimal("0")) / (entry_eff / fx * (1 + fx_cost)), req.lot_size)
        binding = "max_position_exposure"
    # esposizione massima per settore
    max_sec = ctx.equity_base * limits.max_sector_exposure_pct / _HUNDRED - ctx.exposure_same_sector_base
    if notional_base(qty) > max_sec:
        qty = _floor_lot(max(max_sec, Decimal("0")) / (entry_eff / fx * (1 + fx_cost)), req.lot_size)
        binding = "max_sector_exposure"
    # liquidità disponibile al netto della riserva
    reserve = ctx.equity_base * limits.min_cash_reserve_pct / _HUNDRED
    spendable = ctx.cash_base - reserve
    while qty > 0 and notional_base(qty) + costs.fee(entry_eff * qty) / fx > spendable:
        qty -= req.lot_size
        binding = "cash_available"
    qty = max(qty, Decimal("0"))
    # rischio aggregato
    max_agg = ctx.equity_base * limits.max_aggregate_risk_pct / _HUNDRED
    while qty > 0 and ctx.open_risk_base + loss_at_stop(qty) > max_agg:
        qty -= req.lot_size
        binding = "max_aggregate_risk"
    qty = max(qty, Decimal("0"))
    if req.max_quantity is not None and qty > req.max_quantity:
        qty = _floor_lot(req.max_quantity, req.lot_size)
        binding = "portfolio_manager"

    if qty == 0:
        blocking.append(f"Quantità nulla dopo i vincoli (vincolo determinante: {binding}).")

    loss = loss_at_stop(qty)
    gap_exit = req.stop_price * (1 - req.gap_stress_pct / _HUNDRED)
    gap_loss = (qty * (entry_eff - gap_exit) / fx + (costs.fee(entry_eff * qty) + costs.fee(gap_exit * qty)) / fx) if qty else Decimal("0")

    def gain_at(tp: Decimal | None, q: Decimal) -> Decimal | None:
        if tp is None or q == 0:
            return None
        exit_eff = tp * (1 - slip)
        return q * (exit_eff - entry_eff) / fx - (costs.fee(entry_eff * q) + costs.fee(exit_eff * q)) / fx

    g1 = gain_at(req.take_profit_1, qty)
    g2 = gain_at(req.take_profit_2, qty)
    rr = None
    if req.take_profit_1 is not None and qty > 0 and loss > 0:
        if req.take_profit_1 <= req.entry_price:
            blocking.append("Il take profit 1 deve essere superiore al prezzo di ingresso.")
        if req.take_profit_2 is not None:
            # uscita parziale: frazione a TP1, resto a TP2; una sola commissione di ingresso
            f = req.tp1_close_fraction
            q1 = _floor_lot(qty * f, req.lot_size)
            q2 = qty - q1
            ex1, ex2 = req.take_profit_1 * (1 - slip), req.take_profit_2 * (1 - slip)
            blended = (q1 * (ex1 - entry_eff) + q2 * (ex2 - entry_eff)) / fx
            fees = (costs.fee(entry_eff * qty) + (costs.fee(ex1 * q1) if q1 else 0) + (costs.fee(ex2 * q2) if q2 else 0)) / fx
            rr = (blended - fees) / loss
        elif g1 is not None:
            rr = g1 / loss
        if rr is not None and rr < 1:
            warnings.append("Rapporto rischio/rendimento netto inferiore a 1.")

    warnings.append("Correlazioni con le posizioni aperte non ancora valutate (previste in Fase 3).")
    nb = notional_base(qty)
    q = lambda x: x.quantize(_M, ROUND_HALF_UP) if x is not None else None  # noqa: E731
    return SizingResult(
        approved=not blocking, quantity=qty, theoretical_quantity=theoretical.quantize(Decimal("0.0001")),
        risk_budget_base=q(risk_budget), per_unit_risk_base=per_unit_risk_base.quantize(Decimal("0.0001")),
        notional_base=q(nb), entry_fee_base=q(costs.fee(entry_eff * qty) / fx if qty else Decimal("0")),
        est_loss_at_stop_base=q(loss), est_loss_gap_scenario_base=q(gap_loss),
        est_gain_tp1_base=q(g1), est_gain_tp2_base=q(g2),
        reward_risk_net=rr.quantize(Decimal("0.01")) if rr is not None else None,
        exposure_pct=q(nb / ctx.equity_base * _HUNDRED) if ctx.equity_base > 0 else Decimal("0"),
        aggregate_risk_after_pct=q((ctx.open_risk_base + loss) / ctx.equity_base * _HUNDRED) if ctx.equity_base > 0 else Decimal("0"),
        binding_constraint=binding, blocking_reasons=blocking, warnings=warnings,
    )
