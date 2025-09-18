# src/run_paper.py
import argparse
import time
import sys
from .logger import logger
from .broker_alpaca import BrokerAlpaca
from .data import bars_to_df
from .strategy import MACrossover
from .risk import RiskManager

def main():
    parser = argparse.ArgumentParser(description="Loop de paper-trading (Alpaca)")
    parser.add_argument("--symbol", type=str, default="AAPL")
    parser.add_argument("--timeframe", type=str, default="1Min")
    parser.add_argument("--lookback", type=int, default=120)
    parser.add_argument("--dry-run", action="store_true", help="No conecta al broker; solo prueba imports y config")
    args = parser.parse_args()

    print(f"▶️ Iniciando bot: symbol={args.symbol}, timeframe={args.timeframe}, lookback={args.lookback}")

    if args.dry_run:
        logger.info("Dry-run activo. Saliendo sin conectar a broker.")
        print("Dry run OK ✅")
        return

    try:
        broker = BrokerAlpaca()
    except Exception as e:
        logger.error(f"No se pudo inicializar BrokerAlpaca: {e}")
        print(f"Error inicializando broker: {e}")
        sys.exit(1)

    # Obtener cuenta para equity inicial
    acct = broker.get_account()
    equity = float(acct.get("equity", 10_000))
    risk = RiskManager(equity=equity)
    strat = MACrossover(fast=10, slow=30)

    logger.info(f"Comenzando loop paper-trading: symbol={args.symbol}, tf={args.timeframe}, lookback={args.lookback}")
    print("🔁 Loop iniciado. CTRL+C para detener.")

    while True:
        try:
            print("⏳ Tick: pidiendo barras…")
            bars = broker.get_bars(args.symbol, timeframe=args.timeframe, limit=args.lookback)
            df = bars_to_df(bars)
            last = df.iloc[-1]
            print(f"📈 Última {args.timeframe}: close={last['close']:.2f}  (rows={len(df)})")

            sig = strat.signal(df)
            print(f"🧭 Señal: {sig or 'HOLD'}")

            if not risk.can_trade():
                logger.warning("Límite de pérdida diaria alcanzado. Pausando bot.")
                print("⚠️  Límite de pérdida diaria alcanzado. Pausando bot.")
                time.sleep(60)
                continue

            if sig == "BUY":
                qty = risk.size(df["close"].iloc[-1])
                order = broker.place_order(args.symbol, "buy", qty)
                msg = f"✅ BUY {args.symbol} x{qty} -> id={order.get('id', 'sin_id')}"
                logger.info(msg)
                print(msg)
            elif sig == "SELL":
                qty = risk.size(df["close"].iloc[-1])
                order = broker.place_order(args.symbol, "sell", qty)
                msg = f"✅ SELL {args.symbol} x{qty} -> id={order.get('id', 'sin_id')}"
                logger.info(msg)
                print(msg)
            else:
                msg = "Sin señal. Esperando siguiente barra."
                logger.info(msg)
                print(msg)

            # ⏱️ Para desarrollo: 10s. En producción vuelve a 60.
            time.sleep(60)
        except KeyboardInterrupt:
            logger.info("Bot detenido manualmente.")
            print("🛑 Bot detenido manualmente.")
            break
        except Exception as e:
            logger.exception(f"Error en loop principal: {e}")
            print(f"❌ Error en loop: {e}")
            time.sleep(10)

if __name__ == "__main__":
    main()
