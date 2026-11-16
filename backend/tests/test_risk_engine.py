from decimal import Decimal as D

import pytest

from app.services.risk import Costs, PortfolioContext, RiskLimits, SizingRequest, compute_position_size

LIMITS = RiskLimits(D("0.5"), D("3"), D("15"), D("30"), D("5"))
NO_COSTS = Costs(D("0"), D("0"), D("0"))
CTX = PortfolioContext(equity_base=D("10000"), cash_base=D("10000"), open_risk_base=D("0"))


def test_spec_formula_without_costs():
    # Risk budget = 10.000 × 0,5% = 50 ; quantità = 50 / |100 − 95| = 10
    r = compute_position_size(SizingRequest(D("100"), D("95")), CTX, LIMITS, NO_COSTS)
    assert r.risk_budget_base == D("50.00")
    assert r.theoretical_quantity == D("10.0000")
    assert r.quantity == D("10")
    assert r.est_loss_at_stop_base == D("50.00")
    assert r.approved


def test_costs_and_slippage_reduce_quantity():
    costs = Costs(D("2"), D("0"), D("10"))
    r = compute_position_size(SizingRequest(D("100"), D("95")), CTX, LIMITS, costs)
    assert r.quantity < 10
    assert r.est_loss_at_stop_base <= r.risk_budget_base


def test_max_position_exposure_caps_size():
    # stop molto stretto -> quantità teorica enorme, limitata dal 15% (1.500 €)
    r = compute_position_size(SizingRequest(D("100"), D("99.9")), CTX, LIMITS, NO_COSTS)
    assert r.binding_constraint == "max_position_exposure"
    assert r.notional_base <= D("1500")
    assert r.quantity == D("15")


def test_cash_reserve_and_override():
    ctx = PortfolioContext(equity_base=D("10000"), cash_base=D("1000"), open_risk_base=D("0"))
    r = compute_position_size(SizingRequest(D("100"), D("99.9")), ctx, LIMITS, NO_COSTS)
    # liquidità spendibile = 1000 − 5% di 10.000 = 500
    assert r.binding_constraint == "cash_available"
    assert r.notional_base <= D("500")


def test_aggregate_risk_blocks():
    ctx = PortfolioContext(equity_base=D("10000"), cash_base=D("10000"), open_risk_base=D("300"))
    r = compute_position_size(SizingRequest(D("100"), D("95")), ctx, LIMITS, NO_COSTS)
    assert r.quantity == 0
    assert not r.approved
    assert r.binding_constraint == "max_aggregate_risk"


def test_fx_conversion():
    # strumento in USD, 1 EUR = 1,10 USD: rischio per azione 5 USD = 4,545 EUR -> 11 azioni
    r = compute_position_size(SizingRequest(D("100"), D("95"), fx_rate=D("1.10")), CTX, LIMITS, NO_COSTS)
    assert r.quantity == D("11")
    assert r.est_loss_at_stop_base <= D("50")


def test_reward_risk_with_partial_exit():
    req = SizingRequest(D("100"), D("95"), take_profit_1=D("110"), take_profit_2=D("120"), tp1_close_fraction=D("0.5"))
    r = compute_position_size(req, CTX, LIMITS, NO_COSTS)
    # metà a +10, metà a +20 => +15 medio vs −5: R/R = 3
    assert r.reward_risk_net == D("3.00")


def test_invalid_long_stop():
    with pytest.raises(ValueError):
        compute_position_size(SizingRequest(D("100"), D("105")), CTX, LIMITS, NO_COSTS)


def test_loss_is_flagged_as_estimate():
    r = compute_position_size(SizingRequest(D("100"), D("95")), CTX, LIMITS, NO_COSTS)
    assert any("stima" in w for w in r.warnings)
    assert r.est_loss_gap_scenario_base > r.est_loss_at_stop_base
