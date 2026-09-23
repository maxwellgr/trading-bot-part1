# Trading Bot (Paper) — Stocks/ETFs with *Ensemble*, Risk Management and Profit Protection

[![tests](https://github.com/maxwellgr/trading-bot-part1/actions/workflows/tests.yml/badge.svg)](https://github.com/maxwellgr/trading-bot-part1/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![Python](https://img.shields.io/badge/Python-3.10%2B-blue)
![Status](https://img.shields.io/badge/Status-Actively%20maintained-brightgreen)
![Broker](https://img.shields.io/badge/Broker-Alpaca%20Paper-black)

> **Executive Summary**
> Multi‑symbol *paper trading* bot for **stocks/ETFs** (Alpaca) featuring **technical strategies** (MA, RSI, MACD, Bollinger Bands), **ensemble methods** (consensus / weighted / stacked), an **advanced risk manager** (risk‑based sizing, min R\:R, ATR trailing stop, exposure/leverage limits, circuit breakers) and **profit protection** (break‑even per R, scale‑out, giveback). Position state is persisted to disk and reconciled against the broker on every restart, so a crash never leaves an open position unprotected.

> **Scope**: This repository is focused on **paper trading** for educational and validation purposes. It does not constitute financial advice.

---

## ✨ Key Features

* **Multi‑symbol / multi‑timeframe** (1m, 5m, 15m) with configurable poll interval and a liquidity filter (min $ volume) before every entry.
* **Plug‑and‑play strategies:** MA crossover, RSI, MACD, Bollinger — all share the same `.signal(df) -> "BUY"/"SELL"/None` interface.
* **Signal ensemble:** consensus (k‑of‑n), confidence‑weighted, and stacked (primary + confirmers), with optional trend/volatility regime filters.
* **Advanced risk manager** (`risk_manager_avanzado.py`): position sizing by % of equity, min R\:R validation (net of fees/slippage), ATR‑based stop/take‑profit, ATR trailing stop, max positions/leverage/portfolio‑heat/per‑symbol‑exposure limits, and circuit breakers (daily loss limit, consecutive losses) — **all tunable via CLI flags**, no code edits required.
* **Profit protection:** automatic break‑even at R multiples, partial scale‑outs, configurable giveback, and a daily realized‑profit halt.
* **Crash‑resilient state:** the local position book (stop/take/entry/trailing/scale‑outs) is saved to `data/state.json` after every tick and reconciled against the broker's real positions on startup — an orphaned position from a bot restart gets a conservative stop instead of running unprotected.
* **Circuit breakers only block *new* entries** — when a breaker trips, already‑open positions keep getting their trailing stop, break‑even and exit checks (this used to not be the case; see [Known limitations / fixed issues](#-known-limitations--recently-fixed-issues)).
* **Backtesting:** `backtest.py` runs any of the 4 strategies over a local CSV with fees + slippage (bps), optional shorting, and reports Sharpe/Sortino/Calmar, max drawdown, win rate, profit factor and expectancy.
* **Tests:** a `pytest` suite covers strategy signals, the ensemble, the risk manager's sizing/guards/trailing logic, the metrics module, the backtester, and the position‑persistence/reconciliation logic.
* **Observability:** structured logs (`logs/bot.log`, rotating) + console, UTF‑8 stdout so the status emojis don't crash the process on Windows' default `cp1252` console.

---

## 📂 Repository Structure & Requirements

### Repo Structure (as it actually exists)

```
├── src/
│   ├── run_paper.py            # Main entry point (paper trading loop)
│   ├── backtest.py             # CSV backtester (all 4 strategies)
│   ├── strategy.py             # MACrossover, RSIStrategy, MACDStrategy, BollingerStrategy
│   ├── ensemble.py             # Ensemble (consensus / weighted / stacked) + regime filters
│   ├── risk_manager_avanzado.py # Advanced RiskManager (the one actually used) + RiskConfig
│   ├── broker_base.py          # BrokerBase abstract interface
│   ├── broker_alpaca.py        # BrokerAlpaca (implements BrokerBase) via Alpaca REST
│   ├── data.py                 # bars_to_df / load_csv
│   ├── metrics.py              # Sharpe/Sortino/Calmar/drawdown/win-rate/profit-factor
│   ├── plot_strategy.py        # Price + MA + signals PNG chart
│   ├── logger.py               # Rotating file + console logger (UTF-8 safe)
│   └── config.py               # .env loader (Settings)
├── tests/                      # pytest suite
├── data/                       # gitignored: state.json, trades, plots, backtests
├── logs/                       # gitignored: bot.log
├── .env / .env.sample          # Alpaca Paper credentials (never commit .env)
├── requirements.txt            # runtime dependencies
├── requirements-dev.txt        # + pytest
└── README.md
```

There is **no `config/config.yaml`** — all tunables are CLI flags on `run_paper.py` / `backtest.py` (run with `--help` to see the full list) plus the broker credentials in `.env`.

### Requirements

* Python **3.10+**
* Account at [Alpaca](https://alpaca.markets/) (Paper)
* Runtime libraries: `pandas`, `numpy`, `requests`, `python-dotenv`, `matplotlib` (only needed for `plot_strategy.py`)

Install dependencies:

```bash
pip install -r requirements.txt        # runtime only
pip install -r requirements-dev.txt    # + pytest, for running the test suite
```

---

## ⚡ Installation & Quickstart (Paper)

### 1. Clone the repo and create a virtual environment

```bash
git clone <your-fork-url>
cd trading-bot-part1
python -m venv .venv
source .venv/bin/activate   # Linux/Mac
.venv\Scripts\activate      # Windows
pip install -r requirements-dev.txt
```

### 2. Configure credentials

Copy `.env.sample` → `.env` and fill in your **Alpaca Paper** keys:

```env
APCA_BASE_URL=https://paper-api.alpaca.markets
APCA_DATA_BASE_URL=https://data.alpaca.markets/v2
APCA_API_KEY_ID=your_key_id
APCA_API_SECRET_KEY=your_secret
LOG_LEVEL=INFO
```

`.env` is already in `.gitignore` — never commit it. If a key is ever exposed, rotate it from the [Alpaca paper dashboard](https://app.alpaca.markets/paper/dashboard/overview); it costs nothing and takes a minute.

### 3. Sanity-check the config

```bash
python -m src.smoke_test
```

### 4. Run the test suite

```bash
python -m pytest
```

### 5. Run in paper mode

```bash
python -m src.run_paper --symbols AAPL,TSLA,NVDA --timeframe 1Min --strategy ma --fast 3 --slow 7
```

Add `--ensemble-mode consensus` (or `weighted`/`stacked`) to combine all 4 strategies instead of using a single one. Logs go to console and `logs/bot.log`; position state persists to `data/state.json`.

---

## 🧠 Strategies & Ensemble

* **MA Cross**: crossover of fast/slow MAs.
* **RSI**: crosses back above the oversold level → LONG signal; crosses back below the overbought level → SHORT signal.
* **MACD**: signal-line crossover.
* **Bollinger**: mean‑reversion (re‑entry after a close outside a band).

**Ensemble modes** (`--ensemble-mode`):

* `consensus` — needs `k` agreeing votes and zero opposing votes (`--ensemble-k`).
* `weighted` — `score = Σ(weight · BUY) − Σ(weight · SELL)`; triggers when `|score| ≥ --ensemble-min-score`. By default a single low‑weight dissenting vote does **not** veto the trade (that would defeat the point of weighting); pass `--ensemble-require-no-opposition` to restore the stricter all‑agree behavior.
* `stacked` — the `primary` strategy (MA) must fire, plus `k-1` of the others confirming.

Optional regime gates: `--regime-trend-filter` (only trade with the SMA200 trend) and `--regime-atr-filter` (skip low‑volatility periods).

---

## 🛡️ Risk Management & Profit Protection

All of these are CLI flags on `run_paper.py` (run `--help` for the full, current list) — nothing is hardcoded in source anymore:

| Flag | Meaning | Default |
|---|---|---|
| `--risk-per-trade` | % of equity risked per trade (fixed‑fractional) | 0.005 |
| `--min-rr` | Minimum reward\:risk (net of fees/slippage) to accept an entry | 1.3 |
| `--atr-sl-mult` / `--atr-tp-mult` | ATR multiple for initial stop / take‑profit | 2.0 / 3.0 |
| `--trailing-atr-mult` | ATR multiple for the trailing stop | 1.5 |
| `--max-positions` | Max simultaneous open positions | 4 |
| `--max-portfolio-heat` | Max sum of open per‑position risk / equity | 0.2 |
| `--max-leverage` | Max gross exposure as a multiple of equity | 1.5 |
| `--max-symbol-exposure` | Max gross exposure per symbol / equity | 0.1 |
| `--min-liquidity` | Min average $ volume required to enter | 200,000 |
| `--daily-loss-limit-pct` | Blocks *new* entries once equity drops this % from the day's start | 0.03 |
| `--max-consecutive-losses` | Blocks *new* entries after N losing trades in a row | 3 |
| `--be-at-r` | Moves the stop to break‑even at this R multiple | 1.0 |
| `--scale-out` | Partial exits as `R:pct,R:pct` | `1.0:0.5,2.0:0.5` |
| `--max-giveback-pct` | Closes a trade if it gives back this fraction of its peak PnL | 0.5 |
| `--daily-profit-halt` | Pauses *new* entries after this much realized PnL today (USD) | 300 |

**Important**: the daily‑loss/consecutive‑losses/portfolio‑heat/daily‑profit circuit breakers pause new entries only — they never stop the bot from managing (trailing, break‑even, exit) positions that are already open.

---

## 📈 Backtesting

```bash
python -m src.backtest --file data/AAPL_1min.csv --strategy macd \
    --fee 0.5 --slippage-bps 5 --allow-shorts
```

Supports `--strategy {ma,rsi,macd,bbands}` with the same parameters as `run_paper.py`, fixed per‑side commission (`--fee`), slippage in basis points (`--slippage-bps`), and optional short‑selling (`--allow-shorts`). Prints total return, Sharpe, Sortino, Calmar, max drawdown, and per‑trade stats (win rate, profit factor, expectancy). It warns when the sample is too short (<~30 trades or <~0.1 years) for those numbers to be meaningful.

**Known limitation**: the backtester validates a strategy's *signal logic* with realistic costs — it does **not** run the advanced RiskManager (`risk_manager_avanzado.py`) bar‑by‑bar (no ATR sizing, no R\:R gating, no portfolio‑heat limits). Treat `--risk-per-trade`, `--atr-sl-mult`, etc. as parameters you still need to validate in paper trading, not ones the backtester has pre‑validated for you.

---

## ⚙️ Operations, State & Graceful Shutdown

* Orders are placed via `BrokerAlpaca` (market orders only — no native bracket orders; stops/trailing/take‑profit/scale‑outs are all managed by the bot's own polling loop, since ATR trailing is dynamic and can't be expressed as a static bracket order).
* The position book (entry, stop, take‑profit, trailing state, break‑even flag, scale‑out levels already taken) is saved to `data/state.json` after every symbol tick.
* On startup, `reconcile_positions()` compares that saved state against the broker's real positions: a position the broker has but the bot doesn't know about gets rebuilt with a conservative 2% stop instead of being left unmanaged; a position the bot thinks is open but the broker has already closed is dropped from the local book; quantity mismatches are corrected to the broker's value. The same drift check runs on every tick, not just at startup.
* `Ctrl+C` stops the loop cleanly (`KeyboardInterrupt` is caught).

---

## 📊 Logs, Reports & Troubleshooting

* **Logs:** console + rotating file (`logs/bot.log`, 1MB × 5 backups).
* **State:** `data/state.json` (position book — safe to delete when flat; the bot will just reconcile from the broker on next start).
* **Common issues:**
  * Invalid API keys → `RuntimeError: Faltan variables...` from `config.py`.
  * No trades → check `--min-rr`, `--min-liquidity`, and warm‑up (`--lookback`/`--hours-back`) against your indicator windows.
  * On Windows, if you see `UnicodeEncodeError` from a *fork* of this repo that removed the UTF‑8 stdout fix in `logger.py`, that's what broke — restore it.
* Debug by setting `LOG_LEVEL=DEBUG` in `.env`.

---

## 🧪 Known limitations / recently fixed issues

For transparency (this section will shrink over time as items get addressed):

* **Fixed:** circuit breakers used to `return` before the position‑management block, silently leaving open positions without trailing/break‑even/exit checks exactly when the bot decided risk was elevated. They now only block new entries.
* **Fixed:** position state lived only in memory — a restart with an open position meant no stop/trailing until a new signal appeared. Now persisted + reconciled against the broker.
* **Fixed:** `RSIStrategy`, `MACDStrategy` and `BollingerStrategy` lacked the warm‑up guard `MACrossover` had, and raised `IndexError` on short input (this is exactly what happened extending the backtester to those strategies).
* **Fixed:** the `weighted` ensemble mode vetoed a trade on any single dissenting vote regardless of weight, which contradicted the point of weighting.
* **Fixed:** default Windows console encoding (`cp1252`) crashed the bot on its first emoji `print()`.
* **Not yet integrated:** the backtester doesn't run the advanced RiskManager bar‑by‑bar (see [Backtesting](#-backtesting) above).
* **Not yet implemented:** per‑tick reconciliation catches quantity drift and externally‑closed positions, but doesn't reconcile partial fills mid‑order (orders are assumed to fill fully at the last seen close price for PnL accounting — fine for paper trading, not accurate enough for real‑money accounting).

---

## 🗺️ Roadmap: target architecture

None of what follows is implemented yet beyond what's described earlier in this README — this section exists so every incremental change can be checked against where the project is headed, instead of guessing.

```
Market Data → Indicators → Market Regime Detector → Strategy / Adaptive Strategy Selection → Risk Manager → Execution → Analytics
```

Current work (see [Known limitations](#-known-limitations--recently-fixed-issues) above) is converging on this step by step: structured signal diagnostics (`StrategyResult`, "Phase A") is the first move toward separating a strategy's *decision* from its *explanation*; splitting `indicators.py` out of `strategy.py` ("Phase B") is next.

### Market Regime Detector (future — not implemented)

A new layer between Indicators and Strategy. Its job is to **classify** market context, never to place orders.

Target states: `TRENDING_UP`, `TRENDING_DOWN`, `RANGING`, `HIGH_VOLATILITY`, `LOW_VOLATILITY`, `BREAKOUT`, and `NO_TRADE`/`UNKNOWN` for when there isn't enough confidence to classify.

* **V1 — rule‑based/statistical** (interpretable, no ML): candidate inputs are ADX, ATR, Bollinger Band width, moving‑average slope, volume, returns, and swing high/low structure.
* **V2 — ML, only after backtesting is trustworthy and there's enough data**: an ML‑based detector would still only output a regime classification — it would **never** place trades directly by default. The Strategy layer would use that classification to select/weight strategies, e.g. `TRENDING` → favor trend‑following (MA/MACD), `RANGING` → favor mean‑reversion (RSI/Bollinger), `HIGH_VOLATILITY`/`UNKNOWN` → cut risk or go `NO_TRADE`. The ML version only gets adopted if it objectively beats the rule‑based one on out‑of‑sample data — not by default for using ML.

**Implication for Analytics, starting now**: when the structured Trade Logger (Phase C) gets designed, it's worth capturing the regime‑detector's likely inputs (ADX, normalized ATR, Bollinger width, MA slope, relative volume) alongside each trade/evaluation from day one, so the historical dataset for V1/V2 doesn't have to be reconstructed later. This does **not** expand the scope of the phase in progress — it's a note for when Phase C is designed.

---

## 🤝 Contributing & License

### Contributing

1. Fork the repo.
2. Create a feature branch (`git checkout -b feature/new-feature`).
3. Run the test suite (`python -m pytest`) — please add tests for new strategy/risk/ensemble logic.
4. Commit with clear messages and open a Pull Request.

### License

Licensed under [MIT License](LICENSE).
