"""
Pruebas para src/ensemble.py, incluyendo el fix del modo 'weighted': antes,
un único voto de baja ponderación en contra vetaba una entrada aunque el
score ya hubiera superado min_score por mucho, lo cual contradice la idea
de "ponderar" votos. Ahora ese veto es opcional (require_no_opposition).
"""
import pandas as pd
import pytest

from src.ensemble import Ensemble, StrategyWrapper
from src.strategy import StrategyResult


class FixedStrategy:
    """Estrategia de prueba: siempre devuelve la señal fija que se le pasó.
    A propósito NO implementa evaluate(), para cubrir el camino de fallback
    del ensemble (estrategias externas que solo tienen signal())."""
    def __init__(self, fixed_signal):
        self.fixed_signal = fixed_signal

    def signal(self, df: pd.DataFrame):
        return self.fixed_signal


class DiagnosticStrategy:
    """Estrategia de prueba que sí implementa evaluate(), para verificar que
    el ensemble propaga reason/values/warmup_ok tal cual al meta['details']."""
    def __init__(self, signal, reason, values=None, warmup_ok=True):
        self._result = StrategyResult(signal, reason, values or {}, warmup_ok)

    def evaluate(self, df: pd.DataFrame) -> StrategyResult:
        return self._result

    def signal(self, df: pd.DataFrame):
        return self._result.signal


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


# ---------------- Fase A: diagnóstico estructurado (meta["details"]) ----------------
#
# Estas pruebas verifican SOLO que el diagnóstico nuevo se arma correctamente;
# no tocan (ni deberían cambiar) la decisión final, que ya cubren las pruebas
# de arriba sin modificarse.

def test_details_present_for_every_wrapper_even_without_evaluate(tiny_df):
    """FixedStrategy no implementa evaluate() — el ensemble debe caer al
    fallback (signal() + reason genérico) sin romperse."""
    ens = Ensemble(mode="consensus", k=1)
    wrappers = _wrappers([("a", "BUY", 1.0), ("b", None, 1.0)])
    _, meta = ens.decide(tiny_df, wrappers)
    assert set(meta["details"].keys()) == {"a", "b"}
    assert meta["details"]["a"]["signal"] == "BUY"
    assert meta["details"]["a"]["warmup_ok"] is True
    assert meta["details"]["b"]["signal"] is None


def test_details_propagates_reason_values_and_warmup_from_evaluate(tiny_df):
    ens = Ensemble(mode="consensus", k=1)
    wrappers = [
        StrategyWrapper("diag", DiagnosticStrategy(
            "BUY", "RSI cruzó hacia arriba el nivel de sobreventa (30): 28.0 -> 31.0",
            values={"rsi_prev": 28.0, "rsi_curr": 31.0}, warmup_ok=True,
        ), 1.0),
    ]
    _, meta = ens.decide(tiny_df, wrappers)
    d = meta["details"]["diag"]
    assert d["signal"] == "BUY"
    assert d["raw_signal"] == "BUY"
    assert "sobreventa" in d["reason"]
    assert d["values"] == {"rsi_prev": 28.0, "rsi_curr": 31.0}
    assert d["warmup_ok"] is True
    assert d["gated_by_regime"] is False


def test_any_warmup_pending_true_when_a_strategy_lacks_data(tiny_df):
    ens = Ensemble(mode="consensus", k=1)
    wrappers = [
        StrategyWrapper("ready", DiagnosticStrategy("BUY", "cruce"), 1.0),
        StrategyWrapper("not_ready", DiagnosticStrategy(None, "warm-up insuficiente (5/16 velas)", warmup_ok=False), 1.0),
    ]
    _, meta = ens.decide(tiny_df, wrappers)
    assert meta["any_warmup_pending"] is True
    assert meta["details"]["not_ready"]["warmup_ok"] is False


def test_any_warmup_pending_false_when_all_strategies_have_enough_data(tiny_df):
    ens = Ensemble(mode="consensus", k=1)
    wrappers = [StrategyWrapper("ready", DiagnosticStrategy(None, "sin cruce"), 1.0)]
    _, meta = ens.decide(tiny_df, wrappers)
    assert meta["any_warmup_pending"] is False


def test_gated_by_regime_marks_veto_from_trend_filter():
    """Con el filtro de tendencia activo, un BUY crudo que el filtro bloquea
    debe quedar marcado como gated_by_regime=True y NO contar para el score
    (esto ya pasaba antes del cambio; aquí solo verificamos que ahora
    también quede anotado para diagnóstico)."""
    # Precio muy por debajo de la SMA(5) -> allow_long=False
    df = pd.DataFrame({"close": [100, 100, 100, 100, 50.0]})
    ens = Ensemble(mode="consensus", k=1, use_trend_filter=True, trend_window=5)
    wrappers = [StrategyWrapper("diag", DiagnosticStrategy("BUY", "cruce alcista"), 1.0)]
    sig, meta = ens.decide(df, wrappers)
    d = meta["details"]["diag"]
    assert d["raw_signal"] == "BUY"          # la estrategia sí dijo BUY
    assert d["signal"] is None               # pero el filtro de régimen lo vetó
    assert d["gated_by_regime"] is True
    assert sig == "HOLD"
    assert meta["votes"]["BUY"] == 0         # el veto no debe contar para el score/votos
