# 🤖 Trading Bot (Paper-Trading con Alpaca)

[![Python](https://img.shields.io/badge/Python-3.12-blue?logo=python)](https://www.python.org/)
[![Alpaca](https://img.shields.io/badge/API-Alpaca-orange?logo=alpaca)](https://alpaca.markets/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

Bot de trading algorítmico en **modo paper-trading** usando la API de Alpaca.  
Incluye backtesting, visualización de señales y logging. Proyecto orientado a portafolio.

> ⚠️ **Disclaimer**: Proyecto educativo. No es asesoría financiera.

---

## 📂 Estructura

~~~
trading-bot-part1/
│
├── .env.sample        # Ejemplo (copiar a .env con tus llaves)
├── .gitignore
├── requirements.txt
├── README.md
│
├── src/
│   ├── broker_alpaca.py   # Conexión a Alpaca (cuenta, barras, órdenes)
│   ├── strategy.py        # Estrategia (Cruce de Medias)
│   ├── run_paper.py       # Loop principal paper-trading
│   ├── backtest.py        # Backtest con métricas
│   ├── plot_strategy.py   # Gráficos de precio + MAs + señales
│   ├── logger.py          # Logging (archivo + consola)
│   ├── config.py          # Carga variables de entorno
│   └── ...
│
├── data/
│   ├── sample_bars.csv
│   └── plots/             # Aquí se guardan los PNG de gráficos
└── logs/
    └── bot.log            # Log de ejecución (ignorado por Git)
~~~

---

## ⚡ Instalación rápida (Windows PowerShell)

~~~powershell
# 1) Crear y activar entorno
python -m venv .venv
.\.venv\Scripts\Activate

# 2) Dependencias
python -m pip install --upgrade pip
pip install -r requirements.txt

# 3) Variables de entorno
Copy-Item .env.sample .env
~~~

Edita `.env` con tus credenciales de **Alpaca (Paper)**:

~~~ini
ENV=paper
ALPACA_BASE_URL=https://paper-api.alpaca.markets
ALPACA_DATA_URL=https://data.alpaca.markets/v2
ALPACA_API_KEY_ID=TU_KEY
ALPACA_API_SECRET_KEY=TU_SECRET
DAILY_MAX_LOSS_PCT=2.0
POSITION_RISK_PCT=1.0
~~~

---

## 🚀 Uso

### 📈 Backtest
~~~powershell
python -m src.backtest --file data/sample_bars.csv --cash 10000 --fast 10 --slow 30
~~~

### 🤖 Paper Trading
~~~powershell
python -m src.run_paper --symbol AAPL --timeframe 1Min --lookback 120
~~~

### 📊 Gráficos de estrategia
~~~powershell
python -m src.plot_strategy --symbol MSFT --timeframe 5Min --lookback 300 --fast 5 --slow 15
~~~

Los PNG se guardan en `data/plots/`.

---

## 🧠 Estrategia (MA Crossover)

- **BUY**: la media rápida cruza **de abajo hacia arriba** la media lenta.  
- **SELL**: la media rápida cruza **de arriba hacia abajo** la media lenta.  
- **HOLD**: sin cruce → el bot espera.  

> Para ver más señales en demo, prueba `fast=3`, `slow=7` y `timeframe=1Min`.

---

## 📝 Roadmap

- [x] Conexión Alpaca (paper)  
- [x] Backtest + métricas  
- [x] Logging y visualización  
- [ ] Otras estrategias (RSI, MACD, Bollinger)  
- [ ] Stop-loss / take-profit  
- [ ] Persistencia de trades (DB)  

---

## 👤 Autor

**Maxwell González Rivera**  
GitHub: [https://github.com/maxwellgr](https://github.com/maxwellgr)   

---

## 📜 Licencia

Este proyecto está bajo licencia MIT. Consulta el archivo `LICENSE`.

