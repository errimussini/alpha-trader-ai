import numpy as np
import pandas as pd
import pytest

from app.analytics import indicators as ind
from app.analytics.regime import benchmark_state, classify
from app.analytics.scoring import Candidate, FilterConfig, apply_filters, score_universe
from app.analytics.setups import detect


def series(vals):
    return pd.Series(vals, index=pd.bdate_range("2024-01-01", periods=len(vals)), dtype=float)


def ohlc(close, spread=1.0, vol=1e6):
    c = series(close)
    return pd.DataFrame({"open": c, "high": c + spread, "low": c - spread, "close": c, "adj_close": c,
                         "volume": vol}, index=c.index)


def test_ema_and_sma_hand_computed():
    s = series([1, 2, 3, 4, 5])
    e = ind.ema(s, 3)  # alpha = 0.5, seed = 1 -> 1, 1.5, 2.25, 3.125, 4.0625
    assert np.isnan(e.iloc[1])
    assert e.iloc[2] == pytest.approx(2.25) and e.iloc[4] == pytest.approx(4.0625)
    assert ind.sma(s, 3).iloc[-1] == pytest.approx(4.0)


def test_rsi_extremes_and_wilder():
    up = series(range(1, 40))
    assert ind.rsi(up).iloc[-1] == pytest.approx(100.0)
    down = series(range(40, 1, -1))
    assert ind.rsi(down).iloc[-1] == pytest.approx(0.0)
    alt = series([10, 11] * 20)
    assert 45 < ind.rsi(alt).iloc[-1] < 55


def test_atr_constant_range():
    df = ohlc([100.0] * 30, spread=2.0)  # true range costante = 4
    assert ind.atr(df.high, df.low, df.close).iloc[-1] == pytest.approx(4.0)


def test_bollinger_and_relative_volume():
    s = series([10.0] * 25)
    b = ind.bollinger(s)
    assert b["upper"].iloc[-1] == pytest.approx(10.0) and b["lower"].iloc[-1] == pytest.approx(10.0)
    v = series([100.0] * 20 + [300.0])
    assert ind.relative_volume(v, 20).iloc[-1] == pytest.approx(3.0)


def test_momentum_and_relative_strength():
    s = series(np.linspace(100, 200, 300))
    assert ind.momentum(s, 252).iloc[-1] == pytest.approx(s.iloc[-1] / s.iloc[-253] - 1)
    assert ind.relative_strength(s, s, 63).iloc[-1] == pytest.approx(0.0)


def test_adx_strong_trend():
    df = ohlc(np.linspace(100, 160, 80))
    a = ind.adx(df.high, df.low, df.close)
    assert a["adx"].iloc[-1] > 40 and a["plus_di"].iloc[-1] > a["minus_di"].iloc[-1]


def test_no_lookahead():
    rng = np.random.default_rng(1)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, 400)))
    full = ind.compute_all(ohlc(close))
    cut = ind.compute_all(ohlc(close[:300]))
    cols = [c for c in cut.columns]
    pd.testing.assert_frame_equal(full.iloc[:300][cols], cut[cols], check_freq=False)


def test_swing_points_are_confirmed_later():
    high = series([1, 2, 3, 10, 3, 2, 1, 1, 1, 1, 1, 1, 1])
    ph, _ = ind.swing_points(high, high - 1, k=3)
    assert np.isnan(ph.iloc[3]) and ph.iloc[6] == 10  # noto solo 3 barre dopo


def test_regime_bull_and_bear():
    up = series(np.linspace(100, 200, 300) * (1 + 0.002 * np.sin(np.arange(300))))
    st = benchmark_state("UP", up, "test")
    assert st.trend == "UP"
    down = series(np.linspace(200, 100, 300) * (1 + 0.002 * np.sin(np.arange(300))))
    sd = benchmark_state("DN", down, "test")
    assert sd.trend == "DOWN"
    assert classify([st, sd]).regime in ("SIDEWAYS", "HIGH_VOLATILITY")
    assert classify([]).regime == "UNKNOWN"
    assert classify([st], vix_close=series([30.0] * 10)).regime == "HIGH_VOLATILITY"


def test_trend_setup_detected():
    df = ohlc(np.linspace(100, 180, 300) * (1 + 0.003 * np.sin(np.arange(300) / 3)))
    r = detect(ind.compute_all(df), df["low"])
    assert r.setup is not None and r.quality > 0


def _cand(i, **kw):
    c = Candidate(instrument_id=i, ticker=f"T{i}.US", instrument_type="ETF", sector=None, exchange="US",
                  price=50.0, price_date=pd.Timestamp("2025-01-10").date(), source="t", is_demo=False)
    c.features = {"traded_value20": 1e8, "vol20": 0.25, "mom_12_1": i / 10, "mom_6m": i / 10, "mom_3m": i / 10,
                  "rs_3m": i / 100, "close": 50, "ema20": 49, "ema50": 48, "ema200": 45, "rsi14": 55}
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def test_filters_exclude_with_reasons():
    bars = ohlc([3.0] * 300)
    c = _cand(1, price=3.0)
    apply_filters(c, bars, pd.Timestamp("2025-01-10").date(), FilterConfig())
    assert not c.eligible and any("penny" in e for e in c.exclusions)
    c2 = _cand(2, price_date=pd.Timestamp("2024-12-01").date())
    apply_filters(c2, ohlc([50.0] * 300), pd.Timestamp("2025-01-10").date(), FilterConfig())
    assert any("aggiornati" in e for e in c2.exclusions)


def test_scores_rank_and_coverage():
    from app.analytics.setups import SetupResult
    cs = [_cand(i, setup=SetupResult("TREND_FOLLOWING", ["TREND_FOLLOWING"], 60.0)) for i in range(1, 6)]
    score_universe(cs, 80.0)
    scores = [c.score for c in cs]
    assert all(s is not None for s in scores) and scores == sorted(scores)  # più momentum -> punteggio più alto
    assert cs[0].factors["sentiment"] is None and cs[0].factors["fundamentals"] is None
    assert 0.70 <= cs[0].coverage < 1
    lonely = [_cand(9)]
    score_universe(lonely, None)  # niente setup, niente regime -> copertura insufficiente
    assert lonely[0].score is None
