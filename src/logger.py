# src/logger.py
import logging
from logging.handlers import RotatingFileHandler
import os
import sys

# En Windows, la consola por defecto (PowerShell/cmd) suele usar cp1252, no
# UTF-8. Los prints del bot usan emojis (✅⚠️🚨...) para que el operador vea
# de un vistazo qué pasó; sin este ajuste, el primer print con emoji lanza
# UnicodeEncodeError y mata el proceso en el acto — el bot ni siquiera
# arranca. reconfigure() está disponible desde Python 3.7.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

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
