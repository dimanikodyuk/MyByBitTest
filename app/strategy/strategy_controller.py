from app.database.db_manager import db
from app.bybit_client.real_client import bybit_real
from app.bybit_client.simulation_client import simulation_client
from app.strategy.predictor import predictor
from app.strategy.validator import validator
from app.config import config
from loguru import logger
import asyncio
from datetime import datetime, timedelta
from typing import Dict, Optional


class StrategyController:
    """Контролер стратегії (старт/стоп/скидання)"""

    def __init__(self):
        self.is_running = False
        self.mode = 'simulation'  # 'simulation' or 'real'
        self.tasks = []

    async def start(self, mode: str = None):
        """Запуск стратегії"""
        if mode:
            self.mode = mode

        if self.is_running:
            logger.warning("Strategy already running")
            return False

        # Check if real trading is enabled
        if self.mode == 'real' and not config.REAL_TRADING_ENABLED:
            logger.error("Real trading is disabled in config")
            await db.add_log("START_FAILED", "Real trading disabled in config", "error")
            return False

        self.is_running = True
        await db.set_strategy_running(True)
        await db.set_strategy_mode(self.mode)

        # Start background tasks
        self.tasks = [
            asyncio.create_task(self._analysis_loop()),
            asyncio.create_task(self._validation_loop())
        ]

        await db.add_log(
            f"STRATEGY_START",
            f"Strategy started in {self.mode} mode",
            "success"
        )

        logger.success(f"Strategy started in {self.mode} mode")
        return True

    async def stop(self):
        """Зупинка стратегії"""
        if not self.is_running:
            logger.warning("Strategy not running")
            return False

        self.is_running = False

        # Cancel all tasks
        for task in self.tasks:
            task.cancel()

        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()

        await db.set_strategy_running(False)
        await db.add_log("STRATEGY_STOP", "Strategy stopped", "success")

        logger.info("Strategy stopped")
        return True

    async def reset_simulation(self):
        """Скидання симуляції до $100"""
        if self.mode != 'simulation':
            logger.warning("Can only reset simulation mode")
            return False

        if self.is_running:
            await self.stop()

        await db.reset_simulation()

        await db.add_log("SIMULATION_RESET", "Reset to $100", "success")
        logger.info("Simulation reset to $100")

        return True

    async def _analysis_loop(self):
        """Головний цикл аналізу ринку"""
        while self.is_running:
            try:
                # Аналіз для кожної монети та таймфрейму
                for symbol in config.MONITORED_SYMBOLS:
                    for timeframe in config.TIMEFRAMES:
                        await self._analyze_symbol(symbol, timeframe)
                        await asyncio.sleep(1)  # Невелика затримка між запитами

                # Чекаємо перед наступним циклом (найменший таймфрейм)
                await asyncio.sleep(60)  # 1 хвилина

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Analysis loop error: {e}")
                await asyncio.sleep(10)

    async def _analyze_symbol(self, symbol: str, timeframe: str):
        """Аналіз конкретної монети"""
        try:
            # Отримуємо дані
            client = bybit_real if self.mode == 'real' else simulation_client
            klines = await client.get_klines(symbol, timeframe, 100)

            if not klines:
                return

            # Генеруємо прогноз
            signal = predictor.analyze(klines, symbol, timeframe)

            if not signal:
                return

            # Перевіряємо чи вже є відкритий ордер для цієї пари
            open_orders = await db.get_open_orders_sim()
            has_open = any(o.symbol == symbol for o in open_orders)

            if has_open:
                return

            # Створюємо прогноз в БД
            check_delta = self._get_check_delta(timeframe)
            check_at = datetime.now() + check_delta

            pred_data = {
                'symbol': symbol,
                'timeframe': timeframe,
                'direction': signal['direction'],
                'target_price': signal['target'],
                'stop_loss': signal['stop_loss'],
                'check_at': check_at
            }

            prediction_id = await db.create_prediction(pred_data)

            # Відкриваємо угоду
            quantity = self._calculate_quantity(signal['current_price'])

            order_result = await client.place_order(
                symbol=symbol,
                side=signal['direction'],
                order_type='market',
                quantity=quantity,
                prediction_id=prediction_id
            )

            if order_result:
                await db.add_log(
                    f"TRADE_OPEN",
                    f"{signal['direction'].upper()} {quantity} {symbol} at ${signal['current_price']:.2f}",
                    "success"
                )

                # Відправка Telegram сповіщення (буде реалізовано)
                await self._send_telegram_notification(symbol, signal, quantity)

        except Exception as e:
            logger.error(f"Error analyzing {symbol}: {e}")

    async def _validation_loop(self):
        """Цикл перевірки прогнозів"""
        while self.is_running:
            try:
                await validator.validate_all_pending()
                await asyncio.sleep(30)  # Перевірка кожні 30 секунд
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Validation loop error: {e}")
                await asyncio.sleep(10)

    def _get_check_delta(self, timeframe: str) -> timedelta:
        """Отримання часу для перевірки прогнозу"""
        deltas = {
            '1m': timedelta(minutes=5),
            '5m': timedelta(minutes=15),
            '15m': timedelta(minutes=30),
            '1h': timedelta(hours=2),
            '4h': timedelta(hours=8),
            '1d': timedelta(days=2)
        }
        return deltas.get(timeframe, timedelta(hours=1))

    def _calculate_quantity(self, price: float) -> float:
        """Розрахунок кількості монет (використовуємо ~10% балансу)"""
        # Для спрощення - фіксована сума $10
        investment = 10.0
        quantity = investment / price

        # Округлення до 5 знаків (для криптовалют)
        return round(quantity, 5)

    async def _send_telegram_notification(self, symbol: str, signal: dict, quantity: float):
        """Відправка Telegram сповіщення"""
        # Буде реалізовано пізніше з telegram_bot модулем
        pass


strategy_controller = StrategyController()