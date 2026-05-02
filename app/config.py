import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    # ByBit
    BYBIT_API_KEY = os.getenv('BYBIT_API_KEY', '')
    BYBIT_API_SECRET = os.getenv('BYBIT_API_SECRET', '')
    BYBIT_TESTNET = os.getenv('BYBIT_TESTNET', 'true').lower() == 'true'

    # Database - використовуємо SQLite
    DB_PATH = os.getenv('DB_PATH', 'bybit_bot.db')

    @property
    def DATABASE_URL(self):
        return f"sqlite+aiosqlite:///{self.DB_PATH}"

    # Telegram
    TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
    TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')

    # Web
    WEB_USERNAME = os.getenv('WEB_USERNAME', 'admin')
    WEB_PASSWORD = os.getenv('WEB_PASSWORD', 'admin')

    # Trading - тільки ті інтервали, які підтримує ByBit
    MONITORED_SYMBOLS = os.getenv('MONITORED_SYMBOLS', 'BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,ADAUSDT').split(',')
    TIMEFRAMES = ['1', '5', '15', '60', '240', 'D']  # ByBit формат: 1m,5m,15m,1h,4h,1d
    SIMULATION_START_BALANCE = float(os.getenv('SIMULATION_START_BALANCE', '100.0'))
    REAL_TRADING_ENABLED = os.getenv('REAL_TRADING_ENABLED', 'false').lower() == 'true'
    FEE_MAKER = float(os.getenv('FEE_MAKER', '0.001'))
    FEE_TAKER = float(os.getenv('FEE_TAKER', '0.001'))


config = Config()