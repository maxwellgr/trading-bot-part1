# setup_docs.ps1 â€” crea README.md, requirements.txt y .env.example (pega y ejecuta en PowerShell)

# ========= README.md =========
$readme = @'
# Trading Bot (Paper) â€” Acciones con Ensemble, Riesgo Avanzado y ProtecciÃ³n de Ganancias

Bot de paper-trading multi-sÃ­mbolo para **acciones/ETFs** usando **Alpaca**, con:
- Estrategias: **MA**, **RSI**, **MACD**, **Bollinger**  
- **Ensemble** (consensus / weighted / stacked) + filtros de **tendencia** y **volatilidad**  
- **Risk Manager avanzado** (sizing por riesgo, RR mÃ­nimo, trailing por ATR, lÃ­mites de apalancamiento/liquidez)  
- **ProtecciÃ³n de ganancias**: break-even por R, **scale-out** por niveles de R, **giveback** por trade y **halt** por objetivo diario  

> âš ï¸ Este bot **opera acciones**. Para **opciones** se usarÃ¡ un bot aparte (`run_options.py`).

---

## Requisitos

- Python 3.10+ (probado con 3.12)
- Cuenta de Alpaca Paper

Instala dependencias (recomendado venv):
```bash
pip install -r requirements.txt
ConfiguraciÃ³n
Crea un archivo .env en la raÃ­z (ejemplo en .env.example):

ini
Copiar cÃ³digo
APCA_BASE_URL=https://paper-api.alpaca.markets
APCA_API_KEY_ID=PKxxxxxx
APCA_API_SECRET_KEY=SKxxxxxx
# Opcional (datos):
APCA_DATA_URL=https://data.alpaca.markets
Validar credenciales
bash
Copiar cÃ³digo
python -c "from src.config import settings; print(settings.APCA_BASE_URL, (settings.APCA_API_KEY_ID[:4]+'***') if settings.APCA_API_KEY_ID else None)"
Estructura (archivos clave)
graphql
Copiar cÃ³digo
src/
  run_paper.py                 # Runner principal (este README documenta sus flags)
  risk_manager_avanzado.py     # Risk manager + config/decisiones
  ensemble.py                  # LÃ³gica de ensemble y wrappers de estrategias
  broker_alpaca.py             # Cliente sencillo para Alpaca (equities)
  strategy.py                  # MACrossover, RSI, MACD, Bollinger
  data.py                      # ConversiÃ³n de barras â†’ DataFrame
  config.py                    # Carga de .env (pydantic + dotenv)
  logger.py                    # Config de logging
Uso rÃ¡pido
Ver ayuda
bash
Copiar cÃ³digo
python -m src.run_paper -h
Dry-run (sin conectar a broker)
bash
Copiar cÃ³digo
python -m src.run_paper --symbols AAPL,MSFT --dry-run
Operar en horario de mercado (tendencia, conservador)
bash
Copiar cÃ³digo
python -m src.run_paper --symbols AAPL,MSFT,NVDA \
  --timeframe 1Min --lookback 150 \
  --strategy ma --fast 3 --slow 7 \
  --allow-shorts \
  --ensemble-mode consensus --ensemble-k 2 \
  --regime-trend-filter
â€œModo rangoâ€ (dÃ­a plano â€” recomendado)
bash
Copiar cÃ³digo
python -m src.run_paper --symbols SPY,QQQ,AAPL,MSFT,NVDA,AMD \
  --timeframe 15Min --lookback 200 \
  --strategy ma --fast 5 --slow 20 \
  --allow-shorts \
  --ensemble-mode weighted --ensemble-weights "bbands=1,rsi=1,ma=0.3,macd=0.3" \
  --ensemble-min-score 0.8 \
  --be-at-r 1.0 --scale-out "1.0:0.5,2.0:0.5" --max-giveback-pct 0.5 \
  --daily-profit-halt 300 \
  --poll-seconds 20
Fuera de horario (usando histÃ³rico)
bash
Copiar cÃ³digo
python -m src.run_paper --symbols AAPL,MSFT \
  --hours-back 72 --ignore-clock \
  --timeframe 1Min --lookback 150 \
  --strategy ma --fast 3 --slow 7
Principales flags (resumen)
Datos & control

--symbols A,B,C Â· --timeframe 1Min|5Min|15Min Â· --lookback N

--hours-back N (rango histÃ³rico) Â· --ignore-clock (no pausa fuera de horario)

--poll-seconds N (intervalo del loop)

Estrategias

--strategy ma|rsi|macd|bbands y sus parÃ¡metros (--fast/--slow, --rsi-*, --macd-*, --bb-*)

Flags por estado (MA): --enter-when-above, --enter-short-when-below, --exit-*

Ensemble

--ensemble-mode off|consensus|weighted|stacked

--ensemble-k (consensus/stacked)

--ensemble-weights "ma=1,macd=1,rsi=0.5,bbands=0.5"

--ensemble-min-score (weighted)

Filtros de rÃ©gimen:

--regime-trend-filter (+ --regime-trend-window)

--regime-atr-filter (+ --regime-atr-window y --regime-atr-threshold)

ProtecciÃ³n de ganancias

--be-at-r 1.0 â†’ mueve stop a break-even al alcanzar 1R

--scale-out "1.0:0.5,2.0:0.5" â†’ parciales por niveles R

--max-giveback-pct 0.5 â†’ si devuelve â‰¥50% del pico de PnL del trade, cierra

--daily-profit-halt 300 â†’ pausa nuevas entradas al llegar a +$300 realizado

Nota: El giveback diario (pausa si devuelves X% del pico del dÃ­a) es opcional y puede aÃ±adirse con un snippet extra (no activado por defecto).

CÃ³mo decide el RiskManager
Sizing por riesgo: arriesga account_risk_pct del equity con stop por ATR (configurable).

RR mÃ­nimo: rechaza entradas con RR < min_rr.

LÃ­mites: max_positions, max_positions_per_symbol, apalancamiento y liquidez (umbral en USD por barra).

Trailing stop por ATR.

Rechazos comunes y soluciÃ³n:

RR < min_rr â†’ sube timeframe o ajusta atr_multiple_* / min_rr.

Apalancamiento excedido â†’ baja account_risk_pct, sube atr_multiple_sl o usa 15Min.

Liquidez no suficiente â†’ baja min_liquidity_dollar o usa 5â€“15Min.

Defaults â€œmodo rangoâ€ en el cÃ³digo:
account_risk_pct=0.005, min_rr=1.3, atr_multiple_sl=2.0, atr_multiple_tp=3.0, min_liquidity_dollar=200_000.

Presets Ãºtiles
Tendencia (conservador)

bash
Copiar cÃ³digo
python -m src.run_paper --symbols AAPL,MSFT,NVDA \
  --timeframe 1Min --lookback 150 \
  --strategy ma --fast 3 --slow 7 \
  --allow-shorts \
  --ensemble-mode consensus --ensemble-k 2 \
  --regime-trend-filter
Rango (mÃ¡s seÃ±ales, filtrado)

bash
Copiar cÃ³digo
python -m src.run_paper --symbols SPY,QQQ,AAPL,MSFT,NVDA,AMD \
  --timeframe 15Min --lookback 200 \
  --strategy ma --fast 5 --slow 20 \
  --allow-shorts \
  --ensemble-mode weighted --ensemble-weights "bbands=1,rsi=1,ma=0.3,macd=0.3" \
  --ensemble-min-score 0.8 \
  --be-at-r 1.0 --scale-out "1.0:0.5,2.0:0.5" --max-giveback-pct 0.5 \
  --daily-profit-halt 300 \
  --poll-seconds 20
Post-evento (volÃ¡til)

bash
Copiar cÃ³digo
python -m src.run_paper --symbols AAPL,MSFT,NVDA,AMD \
  --timeframe 5Min --lookback 200 \
  --strategy ma --fast 5 --slow 20 \
  --allow-shorts \
  --ensemble-mode consensus --ensemble-k 2 \
  --regime-trend-filter \
  --poll-seconds 20
InterpretaciÃ³n de logs (rÃ¡pido)
Ensemble: BUY/SELL/HOLD | votes=... score=... â†’ decisiÃ³n combinada.

BUY/SHORT rechazado: RR ... â†’ riesgo/beneficio insuficiente con tus parÃ¡metros.

Apalancamiento excedido â†’ tamaÃ±o demasiado grande para tu lÃ­mite.

ðŸ”§ Trailing stop -> ... â†’ stop actualizÃ¡ndose por ATR.

ðŸ Break-even activado @ â†’ stop movido al precio de entrada.

âœ‚ï¸ Scale-out ... â†’ toma parcial ejecutada.

ðŸ›¡ï¸ Cierre por giveback â†’ se devolviÃ³ â‰¥X% del pico de PnL del trade.

ðŸ§­ Objetivo diario alcanzado â†’ se pausaron nuevas entradas.

Roadmap corto
Bot separado para opciones (run_options.py) con selecciÃ³n por DTE/Î”, sizing por premium y filtros de liquidez (OI/vol/spread).

Bracket orders nativos en Alpaca (SL/TP en broker).

Persistencia de position_book y CSV de mÃ©tricas por trade.

Licencia
Uso personal/educativo. Paper-trading; ajusta bajo tu propio riesgo.
'@

========= requirements.txt =========
$requirements = @'
requests>=2.31.0
pydantic>=2.5.0
python-dotenv>=1.0.0
pandas>=2.0.0
numpy>=1.24.0
alpaca-trade-api>=3.2.0
'@

========= .env.example =========
$envExample = @'

Credenciales Alpaca (Paper)
APCA_BASE_URL=https://paper-api.alpaca.markets
APCA_API_KEY_ID=PKxxxxxx
APCA_API_SECRET_KEY=SKxxxxxx

(Opcional) Endpoint de datos
APCA_DATA_URL=https://data.alpaca.markets
'@

Set-Content -Encoding UTF8 -Path README.md -Value $readme
Set-Content -Encoding UTF8 -Path requirements.txt -Value $requirements
Set-Content -Encoding UTF8 -Path .env.example -Value $envExample

Write-Host "âœ… Archivos creados: README.md, requirements.txt, .env.example" -ForegroundColor Green

markdown
Copiar cÃ³digo

Si prefieres que te lo dÃ© como **.bat** o **bash**, me dices y te lo convierto.
::contentReference[oaicite:0]{index=0}

## Licencia
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
Este proyecto se distribuye bajo la licencia **MIT**. Consulta [LICENSE](LICENSE).

