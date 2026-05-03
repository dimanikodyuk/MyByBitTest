import sys
from pathlib import Path
from loguru import logger
from utils.config_loader import config

# Створення директорій
Path("logs").mkdir(exist_ok=True)
Path("data").mkdir(exist_ok=True)

# Видалення старих налаштувань
logger.remove()

# Додавання виводу в консоль
logger.add(
    sys.stdout,
    format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan> - <level>{message}</level>",
    level=config.get('logging.level', 'INFO')
)

# Додавання виводу у файл
logger.add(
    config.get('logging.file', 'logs/trading.log'),
    rotation=config.get('logging.rotation', '7 days'),
    compression=config.get('logging.compression', 'zip'),
    format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name} | {message}",
    level=config.get('logging.level', 'INFO'),
    retention="7 days"
)


# Додаємо синк для запису в БД з перевіркою
class DatabaseLogSink:
    """Синк для логування в базу даних"""

    def __init__(self):
        self._db_session = None
        self._table_exists = False

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

    def write(self, message):
        try:
            # Перевіряємо чи існує таблиця
            if not self._check_table():
                return

            if hasattr(message, 'record'):
                record = message.record
                level = record['level'].name
                module = record['name']
                text = record['message']

                from db.database import SessionLocal
                from db.models import Log

                db = SessionLocal()
                log_entry = Log(level=level, module=module, message=text)
                db.add(log_entry)
                db.commit()
                db.close()
        except Exception as e:
            print(f"Помилка запису логу в БД: {e}")


# Додаємо синк для БД
try:
    db_sink = DatabaseLogSink()
    logger.add(db_sink.write, level="INFO")
except Exception as e:
    print(f"Не вдалося додати DB sink: {e}")

__all__ = ['logger']