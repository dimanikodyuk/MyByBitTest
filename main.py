#!/usr/bin/env python3
"""
Автотрейдинг бот для Bybit
Paper Trading Mode - MVP + Web UI
"""

import asyncio
import signal
import sys
import threading
from pathlib import Path
import traceback
# Додавання кореневої директорії до шляху
sys.path.insert(0, str(Path(__file__).parent))
from datetime import datetime
from utils.logger import logger
from utils.config_loader import config
from db.database import init_db, SessionLocal
from db.operations import DatabaseOperations
from core.order_manager import OrderManager
from telegram.bot import TelegramBot
from exchange.bybit_client import BybitClient

def global_exception_handler(exc_type, exc_value, exc_traceback):
    logger.error("Unhandled exception", exc_info=(exc_type, exc_value, exc_traceback))
    # Тут можна додати відправку в Telegram, якщо бот ініціалізований
sys.excepthook = global_exception_handler

class AutoTradingBot:
    """Головний клас бота"""

    def __init__(self):
        self.order_manager = None
        self.telegram_bot = None
        self.exchange = None
        self.running = True
        self.web_thread = None

        # Налаштування обробки сигналів
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _signal_handler(self, signum, frame):
        logger.info(f"Отримано сигнал {signum}, завершення роботи...")
        self.running = False
        if self.order_manager:
            self.order_manager.stop()  # ← тут потрібен метод stop

    async def initialize(self):
        """Ініціалізація всіх компонентів"""
        logger.info("=" * 50)
        logger.info("AutoTrading Bot v1.0 - Paper Trading Mode + Web UI")
        logger.info("=" * 50)

        # Ініціалізація БД
        logger.info("Ініціалізація бази даних...")
        init_db()
        self.db_session = SessionLocal()
        self.db_ops = DatabaseOperations(self.db_session)

        # Ініціалізація балансу (якщо порожньо)
        balance = self.db_ops.get_balance("USDT", is_paper=True)
        if balance == 0:
            initial_balance = config.get('paper_trading.initial_balance', 100.0)
            self.db_ops.update_balance("USDT", initial_balance, is_paper=True)
            logger.info(f"Початковий paper баланс встановлено {initial_balance} USDT")

        # Ініціалізація Order Manager
        logger.info("Ініціалізація Order Manager...")
        self.order_manager = OrderManager(self.db_ops)

        # Ініціалізація Telegram бота з передачею order_manager
        logger.info("Ініціалізація Telegram бота...")
        self.telegram_bot = TelegramBot(order_manager=self.order_manager)
        await self.telegram_bot.start()

        # Ініціалізація Exchange
        logger.info("Ініціалізація Exchange клієнта...")
        self.exchange = BybitClient()

        # Підписка на WebSocket свічки (синхронний callback)
        await self._subscribe_websockets()

        # Запуск веб-інтерфейсу в окремому потоці
        self._start_web_interface()

        logger.info("✅ Бот успішно ініціалізовано")
        logger.info("🌐 Web UI доступний за адресою http://localhost:8000")

        await self.telegram_bot.send_message(
            "✅ *AutoTrading Bot Initialized*\n\n"
            "Paper Trading Mode Active\n"
            "Web UI: http://localhost:8000\n"
            "Monitoring: " + ", ".join(config.get('trading.pairs', ['BTCUSDT'])))

    async def send_restart_notification(self):
        """Сповіщення про перезапуск бота"""
        if self.telegram_bot:
            await self.telegram_bot.send_message(
                "🔄 *Bot Restarted*\n\n"
                f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                "Status: Online",
                parse_mode="Markdown"
            )

    def _start_web_interface(self):
        """Запуск веб-інтерфейсу в окремому потоці"""
        try:
            from web.app import set_order_manager, start_web_server

            # Передаємо референс на order_manager
            set_order_manager(self.order_manager)

            # Запускаємо веб-сервер в окремому потоці
            self.web_thread = threading.Thread(
                target=start_web_server,
                kwargs={"host": "0.0.0.0", "port": 8000},
                daemon=True
            )
            self.web_thread.start()
            logger.info("Веб-інтерфейс запущено")
        except Exception as e:
            logger.error(f"Помилка запуску веб-інтерфейсу: {e}")

    async def _subscribe_websockets(self):
        """Підписка на WebSocket для всіх пар (використовує один WebSocket для всіх)"""
        pairs = config.get('trading.pairs', ['BTCUSDT'])
        timeframe = config.get('trading.base_timeframe', '5m')

        # Створюємо синхронний callback з обробкою помилок
        def on_candle(candle):
            """Синхронний callback для нових свічок"""
            try:
                if self.order_manager and self.running:
                    pair = candle.get('symbol')
                    if pair:
                        self.order_manager.on_new_candle(pair, candle)
            except Exception as e:
                logger.error(f"Помилка в callback свічки: {e}")

        # Підписуємось на всі пари через один клієнт
        # BybitClient вже оптимізований для багатьох підписок
        for pair in pairs:
            try:
                self.exchange.subscribe_candles(pair, timeframe, on_candle)
                logger.info(f"Підписка на {pair} {timeframe} свічки")
            except Exception as e:
                logger.error(f"Помилка підписки на {pair}: {e}")
            await asyncio.sleep(0.3)  # Невелика затримка між підписками

    async def run(self):
        """Запуск бота"""
        await self.initialize()

        # Запуск головного циклу OrderManager
        order_manager_task = asyncio.create_task(self.order_manager.run())

        # Очікування завершення
        try:
            await order_manager_task
        except asyncio.CancelledError:
            logger.info("Головне завдання скасовано")
        finally:
            await self.shutdown()

    async def shutdown(self):
        """Коректне завершення роботи"""
        logger.info("Завершення роботи...")

        self.running = False

        if self.order_manager:
            self.order_manager.stop()
            # Даємо час на завершення
            await asyncio.sleep(2)

        if self.telegram_bot:
            await self.telegram_bot.send_message("🛑 *Bot is shutting down*", parse_mode="Markdown")
            await self.telegram_bot.stop()

        if self.exchange:
            self.exchange.close()

        if hasattr(self, 'db_session'):
            self.db_session.close()

        logger.info("Завершення роботи завершено")

def main():
    """Точка входу"""
    bot = AutoTradingBot()

    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        logger.info("Перервано користувачем")
    except Exception as e:
        logger.error(f"Фатальна помилка: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()