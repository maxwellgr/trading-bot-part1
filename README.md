# Trading Bot (Paper) — Stocks/ETFs with *Ensemble*, Risk Management and Profit Protection

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![Python](https://img.shields.io/badge/Python-3.10%2B-blue)
![Status](https://img.shields.io/badge/Status-Actively%20maintained-brightgreen)
![Broker](https://img.shields.io/badge/Broker-Alpaca%20Paper-black)

> **Executive Summary**
> Multi‑symbol *paper trading* bot for **stocks/ETFs** (Alpaca) featuring **technical strategies** (MA, RSI, MACD, Bollinger Bands), **ensemble methods** (consensus / weighted / stacked), **advanced risk manager** (risk‑based sizing, min R\:R, ATR trailing stop, exposure limits) and **profit protection** (break‑even per R, scale‑out, giveback). Modular architecture ready to extend to other brokers via adapters.

---

## ✨ Key Features

* **Multi‑symbol / multi‑timeframe** (1m, 5m, 15m) with API throttling and liquidity filters.
* **Plug‑and‑play strategies:** MA crossover, RSI, MACD, Bollinger.
* **Signal ensemble:** consensus, confidence‑weighted, and stacked (meta‑rule) for robustness.
* **Risk manager:** position sizing by % of equity or fixed R, min R\:R validation, max drawdown and per‑symbol limits.
* **Profit protection:** automatic break‑even at R multiples, ATR trailing, partial scale‑outs and configurable giveback.
* **Safe execution (paper):** slippage control, pre‑order validations and circuit breakers.
* **Observability:** structured logs, basic backtesting, run reports and CSV export.
* **Extensible:** interface‑driven design (Strategy, RiskManager, BrokerAdapter).

> **Scope**: This repository is focused on **paper trading** for educational and validation purposes. It does not constitute financial advice.

---

## 🧭 How to Use this README Step by Step

We’ll build it section by section so you can copy it into your `README.md` without friction.

* Step 1: Header & summary ✅
* Step 2: Repo structure & requirements ✅
* Step 3: Installation & Quickstart ✅
* Step 4: Configuration (`.env` / `config.yaml`) ✅
* Step 5: Strategies & Ensemble ✅
* Step 6: Risk management & profit protection ✅
* Step 7: Operations (open/close positions & graceful shutdown) ✅
* Step 8: Logs, reports & troubleshooting ✅
* Step 9: Contributing & License ✅

---

## 📂 Step 2: Repository Structure & Requirements

### Repo Structure

```
├── config/             # Configs (.yaml / .env)
├── core/               # Strategies, risk, adapters
│   ├── strategies/     # MA, RSI, MACD, Bollinger...
│   ├── risk/           # Risk manager
│   ├── adapters/       # BrokerAdapter (Alpaca, others)
│   └── utils/          # Helpers (logging, indicators...)
├── data/               # Exported trades / backtests
├── tests/              # Unit tests
├── run_paper.py        # Main entry point (Paper trading)
├── requirements.txt    # Dependencies
└── README.md
```

### Requirements

* Python **3.10+**
* Account at [Alpaca](https://alpaca.markets/) (Paper)
* Main libraries: `alpaca-trade-api`, `pandas`, `numpy`, `ta`, `pyyaml`, `loguru`

Install dependencies:

```bash
pip install -r requirements.txt
```

---

## ⚡ Step 3: Installation & Quickstart (Paper)

### 1. Clone the repo

```bash
git clone https://github.com/your-username/trading-bot-paper.git
cd trading-bot-paper
```

### 2. Create virtual environment (recommended)

```bash
python -m venv .venv
source .venv/bin/activate   # Linux/Mac
.venv\Scripts\activate      # Windows
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure credentials & parameters

* Copy `config/config.example.yaml` → `config/config.yaml`
* Edit Alpaca (paper) credentials in `.env`

### 5. Run in paper mode

```bash
python run_paper.py --symbols AAPL,TSLA,NVDA --timeframe 1m
```

This will launch the bot in **paper trading** with the chosen symbols and 1‑minute timeframe. Logs appear in console and `data/`.

---

## 🧩 Step 4: Configuration (`.env` and `config.yaml`)

> **Where files go**
>
> * **`.env`** at project root (never commit to Git).
> * **`config/config.yaml`** in `config/`.

### `.env` — credentials & environment settings

Create `.env` with placeholders (replace with your real **Alpaca Paper** keys):

```env
# Alpaca (Paper)
ALPACA_API_KEY_ID=YOUR_KEY_ID
ALPACA_API_SECRET_KEY=YOUR_SECRET
ALPACA_BASE_URL=https://paper-api.alpaca.markets

# App
LOG_LEVEL=INFO
TZ=America/Puerto_Rico
DATA_DIR=./data
```

### `config.yaml` — bot parameters

See Spanish version above (all keys apply). Example includes `app`, `risk`, `strategies`, `ensemble`, `execution`, and `logging` sections with adjustable values.

---

## 🧠 Step 5: Strategies & Ensemble

* MA Cross: crossover of fast/slow MAs with trend confirmation.
* RSI: oversold (<30) → LONG, overbought (>70) → SHORT.
* MACD: line crossover with histogram acceleration.
* Bollinger: mean reversion or breakout modes.

**Ensemble modes:**

* `consensus` (quorum of strategies).
* `weighted` (sum of weighted confidences).
* `stacked` (meta‑rule: trend + timing).

---

## 🛡️ Step 6: Risk Management & Profit Protection

* Position sizing by % equity risk (R).
* Validate min R\:R before entry.
* Stops by ATR, trailing stops, break‑even shifts.
* Scale‑outs at R multiples.
* Giveback % to lock profits.
* Circuit breakers: daily loss cap, max positions/orders.

---

## ⚙️ Step 7: Operations & Graceful Shutdown

* Orders placed via BrokerAdapter.
* Monitors in real time, updates stops, applies scale‑outs.
* Closes on stop, trailing, giveback or shutdown event.
* `--close-all-on-exit` ensures no open positions remain.

---

## 📊 Step 8: Logs, Reports & Troubleshooting

* **Logs:** console + file (`./data/run.log`).
* **CSV trades:** `./data/trades.csv` with PnL, R multiples.
* **Session report:** total trades, win rate, avg R, max DD.
* **Common issues:** invalid API keys, no trades (filters), NaN indicators, Alpaca paper quirks.
* Debug by setting `LOG_LEVEL=DEBUG`.

---

## 🤝 Step 9: Contributing & License

### Contributing

1. Fork the repo.
2. Create a feature branch (`git checkout -b feature/new-feature`).
3. Commit with clear messages.
4. Run tests (`pytest`).
5. Open a Pull Request.

### License

Licensed under [MIT License](LICENSE).

---
