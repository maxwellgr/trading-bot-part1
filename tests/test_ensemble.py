"""
Pruebas para src/ensemble.py, incluyendo el fix del modo 'weighted': antes,
un único voto de baja ponderación en contra vetaba una entrada aunque el
score ya hubiera superado min_score por mucho, lo cual contradice la idea
de "ponderar" votos. Ahora ese veto es opcional (require_no_opposition).
"""
import pandas as pd
import pytest

from src.ensemble import Ensemble, StrategyWrapper


class FixedStrategy:
    """Estrategia de prueba: siempre devuelve la señal fija que se le pasó."""
    def __init__(self, fixed_signal):
        self.fixed_signal = fixed_signal

    def signal(self, df: pd.DataFrame):
        return self.fixed_signal


def _wrappers(signals_weights):
    """signals_weights: lista de (nombre, señal, peso)."""
    return [StrategyWrapper(name, FixedStrategy(sig), weight) for name, sig, weight in signals_weights]


@pytest.fixture
def tiny_df():
    return pd.DataFrame({"close": [100.0, 101.0, 102.0]})


# ---------------- consensus ----------------

def test_consensus_buy_when_k_agree_and_no_dissent(tiny_df):
    ens = Ensemble(mode="consensus", k=2)
    wrappers = _wrappers([("a", "BUY", 1.0), ("b", "BUY", 1.0), ("c", None, 1.0)])
    sig, meta = ens.decide(tiny_df, wrappers)
    assert sig == "BUY"
    assert meta["votes"] == {"BUY": 2, "SELL": 0}


def test_consensus_holds_on_any_dissent(tiny_df):
    ens = Ensemble(mode="consensus", k=2)
    wrappers = _wrappers([("a", "BUY", 1.0), ("b", "BUY", 1.0), ("c", "SELL", 1.0)])
    sig, _ = ens.decide(tiny_df, wrappers)
    assert sig == "HOLD"


# ---------------- weighted ----------------

def test_weighted_buy_ignores_low_weight_dissent_by_default(tiny_df):
    """Antes del fix: un solo SELL de peso 0.1 bloqueaba el BUY aunque el
    score (2.0) superara por mucho min_score (1.0). Ahora, por defecto
    (require_no_opposition=False), el score manda."""
    ens = Ensemble(mode="weighted", min_score=1.0)
    wrappers = _wrappers([("a", "BUY", 1.0), ("b", "BUY", 1.0), ("c", "SELL", 0.1)])
    sig, meta = ens.decide(tiny_df, wrappers)
    assert sig == "BUY"
    assert meta["score"] == pytest.approx(1.9)


def test_weighted_strict_mode_still_vetoes_on_dissent(tiny_df):
    """Con require_no_opposition=True se recupera el comportamiento anterior,
    para quien lo prefiera explícitamente."""
    ens = Ensemble(mode="weighted", min_score=1.0, require_no_opposition=True)
    wrappers = _wrappers([("a", "BUY", 1.0), ("b", "BUY", 1.0), ("c", "SELL", 0.1)])
    sig, _ = ens.decide(tiny_df, wrappers)
    assert sig == "HOLD"


def test_weighted_below_threshold_holds(tiny_df):
    ens = Ensemble(mode="weighted", min_score=5.0)
    wrappers = _wrappers([("a", "BUY", 1.0), ("b", "BUY", 1.0)])
    sig, _ = ens.decide(tiny_df, wrappers)
    assert sig == "HOLD"


# ---------------- stacked ----------------

def test_stacked_buy_with_enough_confirmations(tiny_df):
    ens = Ensemble(mode="stacked", k=2, primary="ma")
    wrappers = _wrappers([("ma", "BUY", 1.0), ("rsi", "BUY", 1.0), ("macd", None, 1.0)])
    sig, meta = ens.decide(tiny_df, wrappers)
    assert sig == "BUY"
    assert "primary=ma" in meta["reason"]


def test_stacked_holds_without_primary_signal(tiny_df):
    ens = Ensemble(mode="stacked", k=2, primary="ma")
    wrappers = _wrappers([("ma", None, 1.0), ("rsi", "BUY", 1.0)])
    sig, _ = ens.decide(tiny_df, wrappers)
    assert sig == "HOLD"
