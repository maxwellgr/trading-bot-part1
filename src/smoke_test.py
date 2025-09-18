# src/smoke_test.py
from .config import settings
from .logger import logger

def main():
    logger.info("Arrancando smoke_test…")
    logger.info(f"ENV = {settings.env}")
    logger.info(f"Base URL broker = {settings.alpaca_base_url or '(vacía)'}")
    logger.info(f"Risk: daily_max_loss_pct={settings.daily_max_loss_pct}, position_risk_pct={settings.position_risk_pct}")
    print("Smoke test OK ✅ — revisa logs/bot.log")

if __name__ == "__main__":
    main()
