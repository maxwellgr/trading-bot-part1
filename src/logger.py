# src/logger.py
import logging
from logging.handlers import RotatingFileHandler
import os

# Asegura carpeta de logs
os.makedirs("logs", exist_ok=True)

# Logger principal
logger = logging.getLogger("bot")
logger.setLevel(logging.INFO)

# Formato consistente
formatter = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S"
)

# Handler de archivo rotativo
file_handler = RotatingFileHandler(
    "logs/bot.log", maxBytes=1_000_000, backupCount=5, encoding="utf-8"
)
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

# Handler de consola (imprime en tiempo real)
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)
