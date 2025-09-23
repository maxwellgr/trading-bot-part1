# src/config.py
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from dotenv import load_dotenv


# === Localiza y carga el .env en la raíz del proyecto ===
# Estructura esperada:
#   project_root/
#     .env
#     src/
#       config.py
ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"

# override=False => no pisa variables ya presentes en el entorno del sistema
# Si quieres que el .env tenga prioridad, cambia a override=True.
load_dotenv(dotenv_path=ENV_PATH, override=False)


def _env(*keys: str, default: str | None = None) -> str | None:
    """
    Devuelve el primer valor NO vacío encontrado entre las claves de entorno indicadas.
    Normaliza espacios. Si no encuentra nada, devuelve default.
    """
    for k in keys:
        v = os.getenv(k)
        if v is not None:
            v = str(v).strip()
            if v:
                return v
    return default


@dataclass
class Settings:
    # ---------- Trading API (acepta APCA_* y ALPACA_* como fallback) ----------
    APCA_API_KEY_ID: str | None = _env(
        "APCA_API_KEY_ID", "ALPACA_API_KEY_ID", "ALPACA_API_KEY", "ALPACA_KEY"
    )
    APCA_API_SECRET_KEY: str | None = _env(
        "APCA_API_SECRET_KEY", "ALPACA_API_SECRET_KEY", "ALPACA_API_SECRET", "ALPACA_SECRET"
    )
    APCA_BASE_URL: str | None = _env(
        "APCA_BASE_URL", "APCA_API_BASE_URL", "ALPACA_BASE_URL",
        default="https://paper-api.alpaca.markets",
    )

    # ---------- Market Data API (también con fallback) ----------
    APCA_DATA_BASE_URL: str | None = _env(
        "APCA_DATA_BASE_URL", "ALPACA_DATA_BASE_URL", "ALPACA_DATA_URL",
        default="https://data.alpaca.markets",
    )

    # ---------- Opcionales de la app ----------
    LOG_LEVEL: str = _env("LOG_LEVEL", default="INFO")

    # ---------- Helpers ----------
    def dict(self) -> dict:
        """Representación segura para logs (enmascara secreto)."""
        return {
            "APCA_API_KEY_ID": (self.APCA_API_KEY_ID[:4] + "***") if self.APCA_API_KEY_ID else None,
            "APCA_API_SECRET_KEY": "***" if self.APCA_API_SECRET_KEY else None,
            "APCA_BASE_URL": self.APCA_BASE_URL,
            "APCA_DATA_BASE_URL": self.APCA_DATA_BASE_URL,
            "LOG_LEVEL": self.LOG_LEVEL,
        }

    def require(self, *keys: str) -> None:
        """
        Lanza un error claro si faltan variables críticas.
        Uso típico en BrokerAlpaca.__init__:
            settings.require("APCA_API_KEY_ID", "APCA_API_SECRET_KEY", "APCA_BASE_URL", "APCA_DATA_BASE_URL")
        """
        missing = [k for k in keys if not getattr(self, k)]
        if missing:
            raise RuntimeError(
                f"Faltan variables: {', '.join(missing)}. "
                f"Revisa {ENV_PATH} o tu entorno de sistema."
            )

    # ---------- Aliases (compatibilidad con broker_alpaca.py en minúsculas) ----------
    @property
    def alpaca_api_key_id(self) -> str | None:
        return self.APCA_API_KEY_ID

    @property
    def alpaca_api_secret_key(self) -> str | None:
        return self.APCA_API_SECRET_KEY

    @property
    def alpaca_base_url(self) -> str | None:
        return self.APCA_BASE_URL

    @property
    def alpaca_data_url(self) -> str | None:
        return self.APCA_DATA_BASE_URL
        # --- Aliases extra esperados por broker_alpaca.py ---
    @property
    def alpaca_api_key(self) -> str | None:          # broker usa este nombre
        return self.APCA_API_KEY_ID

    @property
    def alpaca_api_secret(self) -> str | None:       # broker suele pedir este también
        return self.APCA_API_SECRET_KEY



# Instancia global
settings = Settings()


# ---- Test manual opcional (ejecutar: python -m src.config) ----
if __name__ == "__main__":
    s = settings
    print("ROOT:", ROOT)
    print("ENV_PATH exists:", ENV_PATH.exists())
    print("Settings:", s.dict())
