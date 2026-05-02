import asyncio
import sys
from pathlib import Path

# Додаємо корінь проєкту до PYTHONPATH
sys.path.append(str(Path(__file__).parent.parent))

from app.database.db_manager import db
from app.trading.strategy_controller import strategy_controller
from app.telegram_bot.handlers import telegram_bot
from app.web.routes import app as web_app
from app.config import config
from loguru import logger
import uvicorn

# Налаштування логування
logger.add("logs/bot.log", rotation="50 MB", retention="10 days", level="DEBUG")
logger.add(sys.stdout, level="INFO")


async def main():
    """Головна функція запуску"""
    logger.info("Starting ByBit Trading Bot...")

    # Ініціалізація бази даних
    await db.init_db()
    logger.info("Database initialized")

    # Запуск Telegram бота в окремому завданні
    asyncio.create_task(telegram_bot.run())

    # Запуск веб-сервера (FastAPI)
    config_uvicorn = uvicorn.Config(
        web_app,
        host="0.0.0.0",
        port=8000,
        log_level="info"
    )
    server = uvicorn.Server(config_uvicorn)

    # Запуск веб-сервера асинхронно
    asyncio.create_task(server.serve())

    logger.success(
        f"Bot started! Web interface: http://localhost:8000 (login: {config.WEB_USERNAME}/{config.WEB_PASSWORD})")

    # Тримаємо бота запущеним
    try:
        await asyncio.Event().wait()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        await strategy_controller.stop()
        logger.info("Bot stopped")


if __name__ == "__main__":
    asyncio.run(main())