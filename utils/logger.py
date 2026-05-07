import sys
from pathlib import Path
from loguru import logger
from utils.config_loader import config

Path("logs").mkdir(exist_ok=True)
Path("data").mkdir(exist_ok=True)

logger.remove()

logger.add(
    sys.stdout,
    format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan> - <level>{message}</level>",
    level=config.get('logging.level', 'INFO')
)

logger.add(
    config.get('logging.file', 'logs/trading.log'),
    rotation=config.get('logging.rotation', '7 days'),
    compression=config.get('logging.compression', 'zip'),
    format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name} | {message}",
    level=config.get('logging.level', 'INFO'),
    retention="7 days"
)


class DatabaseLogSink:
    """Синк для логування в базу даних з категоріями"""

    def __init__(self):
        self._db_session = None
        self._table_exists = False
        self._category_cache = {}

    def _check_table(self):
        try:
            from db.database import SessionLocal
            from sqlalchemy import inspect
            db = SessionLocal()
            inspector = inspect(db.bind)
            self._table_exists = inspector.has_table('logs')
            db.close()
        except Exception:
            self._table_exists = False
        return self._table_exists

    def _get_category_from_module(self, module_name: str) -> str:
        if module_name in self._category_cache:
            return self._category_cache[module_name]

        mappings = {
            'web': 'web',
            'order_manager': 'trading',
            'strategy': 'trading',
            'paper_engine': 'trading',
            'risk_manager': 'trading',
            'news_strategy': 'news',
            'news_trader': 'news',
            'listing_strategy': 'listing',
            'listing_monitor': 'listing',
            'bybit_client': 'exchange',
            'telegram': 'telegram',
            'backtest': 'backtest',
            'config_loader': 'system',
        }

        category = 'system'
        for key, cat in mappings.items():
            if key in module_name:
                category = cat
                break

        self._category_cache[module_name] = category
        return category

    def write(self, message):
        try:
            if not self._check_table():
                return

            if hasattr(message, 'record'):
                record = message.record
                level = record['level'].name
                module = record['name']
                text = record['message']

                category = self._get_category_from_module(module)

                from db.database import SessionLocal
                from db.models import Log

                db = SessionLocal()
                log_entry = Log(level=level, module=module, category=category, message=text)
                db.add(log_entry)
                db.commit()
                db.close()
        except Exception as e:
            print(f"Помилка запису логу в БД: {e}")


try:
    db_sink = DatabaseLogSink()
    logger.add(db_sink.write, level="INFO")
except Exception as e:
    print(f"Не вдалося додати DB sink: {e}")

__all__ = ['logger']