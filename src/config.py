# src/config.py
from pydantic import BaseModel
from dotenv import load_dotenv
import os

# Carga variables del archivo .env si existe
load_dotenv()

class Settings(BaseModel):
    # Entorno
    env: str = os.getenv("ENV", "paper")

    # Alpaca (trading y datos)
    alpaca_base_url: str = os.getenv("ALPACA_BASE_URL", "")
    alpaca_data_url: str = os.getenv("ALPACA_DATA_URL", "")
    alpaca_api_key: str = os.getenv("ALPACA_API_KEY_ID", "")
    alpaca_api_secret: str = os.getenv("ALPACA_API_SECRET_KEY", "")

    # Riesgo
    daily_max_loss_pct: float = float(os.getenv("DAILY_MAX_LOSS_PCT", "2.0"))
    position_risk_pct: float = float(os.getenv("POSITION_RISK_PCT", "1.0"))

settings = Settings()


