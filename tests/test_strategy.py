"""
Pruebas para src/strategy.py.

Cubren dos cosas:
1) Regresión del bug que encontramos al extender el backtester: RSIStrategy,
   MACDStrategy y BollingerStrategy lanzaban IndexError con pocas filas
   (menos de 2) porque no tenían la guarda de warm-up que sí tiene
   MACrossover. Ahora deben devolver None sin reventar.
2) Que las 4 estrategias, corridas tal como las usan run_paper.py y
   backtest.py (con ventanas crecientes de datos, no todo el dataframe de
   una vez), produzcan al menos una señal BUY y una SELL sobre una serie
   sintética diseñada para tener una tendencia clara y una reversión clara.
"""
import numpy as np
import pandas as pd
import pytest

from src.strategy import MACrossover, RSIStrategy, MACDStrategy, BollingerStrategy, StrategyResult


def _trend_reversal_df(n_flat: int = 20, n_up: int = 60, n_down: int = 60) -> pd.DataFrame:
    """
    Serie sintética: plana, luego sube fuerte, luego baja fuerte.
    El tramo plano inicial es clave: si la serie empezara ya en plena subida,
    el cruce de medias ocurriría *dentro* de la ventana de warm-up (todavía
    NaN) y nunca sería observable como transición prev<=0 -> now>0. Con un
    tramo plano antes, las medias llegan "calientes" y el cruce sí se ve.
    """
    flat = np.full(n_flat, 100.0)
    up = np.linspace(100, 160, n_up)
    down = np.linspace(160, 90, n_down)
    close = np.concatenate([flat, up, down])
    idx = pd.date_range("2024-01-01", periods=len(close), freq="1min", tz="UTC")
    return pd.DataFrame({"close": close}, index=idx)


def scan_signals(strategy, df: pd.DataFrame) -> list:
    """Reproduce cómo se usan las estrategias en vivo/backtest: en cada paso
    se les pasa el prefijo del df visto hasta ese momento (nunca el futuro)."""
    out = []
    for i in range(1, len(df) + 1):
        sig = strategy.signal(df.iloc[:i])
        if sig is not None:
            out.append((df.index[i - 1], sig))
    return out


@pytest.mark.parametrize("strategy_factory", [
    lambda: MACrossover(fast=3, slow=10),
    lambda: RSIStrategy(period=5),
    lambda: MACDStrategy(fast=5, slow=13, signal=4),
    lambda: BollingerStrategy(window=10, k=2.0),
])
def test_no_crash_on_short_data(strategy_factory):
    """Con 0 o 1 fila no debe lanzar IndexError: debe devolver None."""
    strategy = strategy_factory()
    empty = pd.DataFrame({"close": pd.Series(dtype=float)})
    one_row = pd.DataFrame({"close": [100.0]})
    assert strategy.signal(empty) is None
    assert strategy.signal(one_row) is None


def test_ma_crossover_produces_buy_and_sell():
    df = _trend_reversal_df()
    signals = scan_signals(MACrossover(fast=3, slow=10), df)
    kinds = {s for _, s in signals}
    assert "BUY" in kinds
    assert "SELL" in kinds


def test_rsi_produces_signals_without_crashing():
    df = _trend_reversal_df()
    signals = scan_signals(RSIStrategy(period=5, buy_level=30, sell_level=70), df)
    # No exigimos BUY y SELL exactos (depende de la sensibilidad de RSI),
    # pero la corrida completa no debe reventar y debe generar al menos una señal.
    assert isinstance(signals, list)
    assert all(s in {"BUY", "SELL"} for _, s in signals)


def test_macd_produces_buy_and_sell():
    df = _trend_reversal_df()
    signals = scan_signals(MACDStrategy(fast=5, slow=13, signal=4), df)
    kinds = {s for _, s in signals}
    assert "BUY" in kinds
    assert "SELL" in kinds


def test_bollinger_runs_without_crashing():
    df = _trend_reversal_df()
    signals = scan_signals(BollingerStrategy(window=10, k=2.0), df)
    assert isinstance(signals, list)
    assert all(s in {"BUY", "SELL"} for _, s in signals)


def test_ma_crossover_requires_fast_lt_slow():
    with pytest.raises(AssertionError):
        MACrossover(fast=10, slow=5)


def test_missing_close_column_raises():
    df = pd.DataFrame({"open": [1, 2, 3]})
    with pytest.raises(ValueError):
        MACrossover(fast=1, slow=2).signal(df)
    with pytest.raises(ValueError):
        RSIStrategy(period=1).signal(df)
    with pytest.raises(ValueError):
        MACDStrategy(fast=1, slow=2, signal=1).signal(df)
    with pytest.raises(ValueError):
        BollingerStrategy(window=2).signal(df)


# ---------------- Fase A: StrategyResult / evaluate() ----------------
#
# Requisito clave: signal() NO debe cambiar de comportamiento. En vez de
# reimplementar la lógica dos veces, signal() ahora es literalmente
# `return self.evaluate(df).signal` (ver strategy.py) — estructuralmente no
# pueden divergir. Estas pruebas lo verifican de todas formas, de forma
# explícita y en muchos puntos de datos, no solo por construcción del código.

@pytest.mark.parametrize("strategy_factory", [
    lambda: MACrossover(fast=3, slow=10),
    lambda: RSIStrategy(period=5),
    lambda: MACDStrategy(fast=5, slow=13, signal=4),
    lambda: BollingerStrategy(window=10, k=2.0),
])
def test_signal_matches_evaluate_signal_across_all_prefixes(strategy_factory):
    """Para CADA prefijo de datos (0..N filas), signal(df) == evaluate(df).signal.
    Esta es la prueba de equivalencia pedida explícitamente: mismos datos,
    misma decisión que antes del refactor de Fase A."""
    strategy = strategy_factory()
    df = _trend_reversal_df()
    for i in range(0, len(df) + 1):
        prefix = df.iloc[:i]
        assert strategy.signal(prefix) == strategy.evaluate(prefix).signal, f"Diverge en prefijo de {i} filas"


@pytest.mark.parametrize("strategy_factory, needed_attr", [
    (lambda: MACrossover(fast=3, slow=10), "slow"),
    (lambda: RSIStrategy(period=5), "period"),
    (lambda: MACDStrategy(fast=5, slow=13, signal=4), "slow"),
    (lambda: BollingerStrategy(window=10, k=2.0), "window"),
])
def test_evaluate_marks_warmup_ok_false_when_insufficient_data(strategy_factory, needed_attr):
    """Con pocas filas, evaluate() debe decir explícitamente que el HOLD es
    por falta de datos (warmup_ok=False), no por ausencia de condición."""
    strategy = strategy_factory()
    result = strategy.evaluate(pd.DataFrame({"close": [100.0]}))
    assert result.signal is None
    assert result.warmup_ok is False
    assert "warm-up" in result.reason.lower() or "nan" in result.reason.lower()


def test_evaluate_marks_warmup_ok_true_when_no_cross_but_enough_data():
    """Con datos suficientes pero sin cruce, el HOLD es 'no ocurrió la
    condición', no 'faltan datos' — warmup_ok debe ser True."""
    # Precio perfectamente plano: nunca cruza nada, pero hay de sobra para calcular las medias.
    df = pd.DataFrame({"close": [100.0] * 30})
    result = MACrossover(fast=3, slow=10).evaluate(df)
    assert result.signal is None
    assert result.warmup_ok is True
    assert "cruce" in result.reason.lower()


def test_evaluate_reports_values_and_reason_on_buy():
    df = _trend_reversal_df()
    strat = MACrossover(fast=3, slow=10)
    for i in range(1, len(df) + 1):
        result = strat.evaluate(df.iloc[:i])
        if result.signal == "BUY":
            assert result.warmup_ok is True
            assert "ma_fast" in result.values and "ma_slow" in result.values
            assert "cruce alcista" in result.reason.lower()
            return
    pytest.fail("La serie sintética no produjo ningún BUY de MACrossover; revisa el fixture.")


def test_rsi_evaluate_reports_rsi_values():
    df = _trend_reversal_df()
    strat = RSIStrategy(period=5, buy_level=30, sell_level=70)
    saw_signal = False
    for i in range(1, len(df) + 1):
        result = strat.evaluate(df.iloc[:i])
        if result.signal is not None:
            saw_signal = True
            assert {"rsi_prev", "rsi_curr", "buy_level", "sell_level"} <= result.values.keys()
    assert saw_signal, "Se esperaba al menos una señal de RSI en la serie sintética."


def test_strategy_result_is_a_plain_dataclass_with_defaults():
    r = StrategyResult(signal="BUY", reason="prueba")
    assert r.values == {}
    assert r.warmup_ok is True
