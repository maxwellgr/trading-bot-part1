# src/metrics.py
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

def max_drawdown(equity: pd.Series) -> float:
    """
    Máximo drawdown en % (negativo).
    """
    roll_max = equity.cummax()
    dd = equity / (roll_max + 1e-12) - 1.0
    return float(dd.min())

def total_return(equity: pd.Series) -> float:
    return float(equity.iloc[-1] / equity.iloc[0] - 1.0)
