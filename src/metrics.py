# src/metrics.py
from typing import Iterable, List, Sequence

import numpy as np
import pandas as pd

def equity_to_returns(equity: pd.Series) -> pd.Series:
    """Convierte curva de equity a rendimientos porcentuales por paso."""
    rets = equity.pct_change().fillna(0.0)
    return rets

def sharpe_ratio(returns: pd.Series, steps_per_year: int = 252, risk_free: float = 0.0) -> float:
    """
    Sharpe = (mean(ret - rf) / std(ret)) * sqrt(steps_per_year)
    - returns: rendimientos por paso (no anualizados)
    """
    if returns.std(ddof=0) == 0:
        return 0.0
    excess = returns - (risk_free / steps_per_year)
    return (excess.mean() / (returns.std(ddof=0) + 1e-12)) * np.sqrt(steps_per_year)

def sortino_ratio(returns: pd.Series, steps_per_year: int = 252, risk_free: float = 0.0) -> float:
    """
    Como el Sharpe, pero penaliza solo la volatilidad a la baja (downside
    deviation) en vez de la volatilidad total — más representativo cuando
    la distribución de retornos es asimétrica, algo común en estrategias
    con stops (pérdidas acotadas, ganancias con cola larga o viceversa).
    """
    excess = returns - (risk_free / steps_per_year)
    downside = excess[excess < 0]
    downside_std = float(np.sqrt((downside ** 2).mean())) if len(downside) > 0 else 0.0
    if downside_std == 0:
        return 0.0
    return (excess.mean() / (downside_std + 1e-12)) * np.sqrt(steps_per_year)

def max_drawdown(equity: pd.Series) -> float:
    """
    Máximo drawdown en % (negativo).
    """
    roll_max = equity.cummax()
    dd = equity / (roll_max + 1e-12) - 1.0
    return float(dd.min())

def total_return(equity: pd.Series) -> float:
    return float(equity.iloc[-1] / equity.iloc[0] - 1.0)

def calmar_ratio(equity: pd.Series, steps_per_year: int = 252) -> float:
    """
    Calmar = retorno anualizado / |max drawdown|.
    Es la métrica que más le importa a quien tiene que vivir con el
    drawdown, no solo con la volatilidad promedio (a diferencia de Sharpe).
    """
    n_steps = len(equity)
    if n_steps < 2:
        return 0.0
    years = n_steps / steps_per_year
    if years <= 0:
        return 0.0
    tr = total_return(equity)
    cagr = (1.0 + tr) ** (1.0 / years) - 1.0
    mdd = abs(max_drawdown(equity))
    if mdd < 1e-9:  # equity nunca cae (o el ruido de punto flotante de max_drawdown)
        return 0.0
    return cagr / mdd

def win_rate(pnls: Sequence[float]) -> float:
    """Fracción de trades cerrados con PnL > 0 (0.0 si no hay trades)."""
    pnls = list(pnls)
    if not pnls:
        return 0.0
    wins = sum(1 for p in pnls if p > 0)
    return wins / len(pnls)

def profit_factor(pnls: Sequence[float]) -> float:
    """
    Suma de ganancias / suma de pérdidas (en valor absoluto).
    >1 significa que el sistema gana más de lo que pierde en agregado;
    inf si no hubo pérdidas; 0.0 si no hubo trades o solo hubo pérdidas
    y ninguna ganancia.
    """
    pnls = list(pnls)
    gains = sum(p for p in pnls if p > 0)
    losses = abs(sum(p for p in pnls if p < 0))
    if losses == 0:
        return float("inf") if gains > 0 else 0.0
    return gains / losses

def expectancy(pnls: Sequence[float]) -> float:
    """PnL promedio por trade (en $, no normalizado por riesgo)."""
    pnls = list(pnls)
    if not pnls:
        return 0.0
    return float(np.mean(pnls))

def trade_stats(pnls: Sequence[float]) -> dict:
    """Resumen de métricas por-trade a partir de una lista de PnL realizados."""
    pnls = list(pnls)
    return {
        "n_trades": len(pnls),
        "win_rate": win_rate(pnls),
        "profit_factor": profit_factor(pnls),
        "expectancy": expectancy(pnls),
        "gross_profit": sum(p for p in pnls if p > 0),
        "gross_loss": sum(p for p in pnls if p < 0),
        "best_trade": max(pnls) if pnls else 0.0,
        "worst_trade": min(pnls) if pnls else 0.0,
    }
