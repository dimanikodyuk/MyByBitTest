# db/database.py
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from pathlib import Path
from utils.config_loader import config
from db.base import Base
from utils.logger import logger

# Створення директорії для БД
db_path = Path(config.get('database.path', 'data/trading.db'))
db_path.parent.mkdir(parents=True, exist_ok=True)

# SQLite URL
SQLALCHEMY_DATABASE_URL = f"sqlite:///{db_path}"

# Engine з налаштуваннями для SQLite
engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    connect_args={"check_same_thread": False},
    echo=False
)

# Session factory
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def get_db() -> Session:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def init_db():
    """Створення всіх таблиць"""
    # Імпортуємо всі моделі для реєстрації в Base.metadata
    from db import models  # noqa
    Base.metadata.create_all(bind=engine)
    logger.info("База даних ініціалізована")