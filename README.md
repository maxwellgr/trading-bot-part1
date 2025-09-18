# 🤖 Trading Bot (Paper Trading con Alpaca)

Bot de trading educativo en Python, basado en **cruce de medias móviles (MA crossover)**.  
Permite conectarse a la API de Alpaca en modo *paper trading*, ejecutar backtests offline y visualizar señales BUY/SELL en gráficos.

⚠️ **Aviso**: Este proyecto es solo con fines educativos. No constituye asesoría financiera.

---

## 🚀 Instalación

### Requisitos
- Python 3.12+
- Cuenta en [Alpaca](https://alpaca.markets/) (usa *paper trading* para pruebas)

### Pasos rápidos (Windows PowerShell)

```powershell
# Clonar el repo o descomprimir el ZIP
cd trading-bot-part1

# Crear entorno virtual
python -m venv .venv
.\.venv\Scripts\Activate

# Instalar dependencias
python -m pip install --upgrade pip
pip install -r requirements.txt

# Copiar variables de entorno
Copy-Item .env.sample .env

