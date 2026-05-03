import asyncio
import pandas as pd
from typing import Dict, Optional, List, Any  # Додайте Any до імпорту
from datetime import datetime
from db.operations import DatabaseOperations
from db.database import SessionLocal
from db.models import SignalType, OrderSide, Trade
from core.strategy import TradingStrategy
from core.paper_engine import PaperEngine
from core.risk_manager import RiskManager
from exchange.bybit_client import BybitClient
from utils.config_loader import config
from utils.logger import logger

class OrderManager:
    """Головний менеджер ордерів - приймає сигнали та виконує угоди"""

    def __init__(self, db_ops: DatabaseOperations, telegram_bot=None):
        self.db = db_ops
        self.telegram = telegram_bot
        self.exchange = BybitClient()
        self.last_forecast_time = {}
        self.timeframes = ['1m', '5m', '15m', '1h', '4h', '1d']

        # Ініціалізація компонентів
        self.paper_engine = PaperEngine(db_ops)
        self.risk_manager = RiskManager(db_ops, is_paper=True)

        # Стратегії для кожної пари
        self.strategies: Dict[str, TradingStrategy] = {}

        # Кеш для даних (таймфрейми)
        self.cache: Dict[str, Dict[str, pd.DataFrame]] = {}

        # Стан бота
        self.running = True
        self.pairs = config.get('trading.pairs', ['BTCUSDT'])

        # Параметри
        self.base_timeframe = config.get('trading.base_timeframe', '5m')
        self.confirm_timeframes = config.get('trading.confirmation_timeframes', ['15m', '1h'])
        self.signal_check_interval = config.get('trading.signal_check_interval', 30)

        # Ініціалізація
        self._init_strategies()
        self._load_initial_data()

        logger.info(f"OrderManager initialized for {len(self.pairs)} pairs")

    def _init_strategies(self):
        """Ініціалізація стратегій для всіх пар"""
        for pair in self.pairs:
            self.strategies[pair] = TradingStrategy(pair)
            self.cache[pair] = {}
            logger.info(f"Strategy initialized for {pair}")

    def _load_initial_data(self):
        """Завантаження початкових даних для всіх пар"""
        for pair in self.pairs:
            for tf in self.timeframes:
                df = self.exchange.get_klines(pair, tf, limit=200)
                if df is not None and len(df) > 0:
                    df = self.strategies[pair].calculate_indicators(df)
                    self.cache[pair][tf] = df

    def on_new_candle(self, pair: str, candle: Dict):
        """Обробка нової свічки (синхронна)"""
        if not self.running:
            return

        timeframe = self.base_timeframe
        logger.debug(f"Нова свічка для {pair} о {candle['timestamp']}")

        # Оновлюємо кеш
        if pair not in self.cache:
            self.cache[pair] = {}

        # Додаємо нову свічку
        new_df = pd.DataFrame([candle])
        if pair in self.cache and timeframe in self.cache[pair]:
            self.cache[pair][timeframe] = pd.concat([self.cache[pair][timeframe], new_df], ignore_index=True)
            self.cache[pair][timeframe] = self.cache[pair][timeframe].tail(200)
        else:
            self.cache[pair][timeframe] = new_df

        # Розрахунок індикаторів
        df = self.strategies[pair].calculate_indicators(self.cache[pair][timeframe])
        self.cache[pair][timeframe] = df

        # Синхронні перевірки (без await)
        self._check_signals_sync(pair)
        self._check_exits_sync(pair)

    def analyze_market(self, pair: str) -> Dict[str, Any]:
        """Аналіз ринку та генерація прогнозів на основі технічних індикаторів"""

        df = self.cache[pair].get(self.base_timeframe)
        if df is None or len(df) < 50:
            return {"error": "Недостатньо даних"}

        last = df.iloc[-1]
        prev = df.iloc[-2]

        # Отримуємо всі індикатори
        ema_fast = last[f'EMA_{config.get("strategy.ema_fast", 50)}']
        ema_slow = last[f'EMA_{config.get("strategy.ema_slow", 200)}']
        rsi = last['RSI']
        macd = last['MACD']
        macd_signal = last['MACD_Signal']

        # Визначаємо тренд
        trend = "📈 ВИСХІДНИЙ" if ema_fast > ema_slow else "📉 НИЗХІДНИЙ"
        trend_strength = abs((ema_fast - ema_slow) / ema_slow * 100)

        # Аналіз RSI
        rsi_signal = "neutral"
        if rsi > 70:
            rsi_signal = "overbought"
        elif rsi < 30:
            rsi_signal = "oversold"

        # Аналіз MACD
        macd_cross = "neutral"
        if macd > macd_signal and prev['MACD'] <= prev['MACD_Signal']:
            macd_cross = "bullish"
        elif macd < macd_signal and prev['MACD'] >= prev['MACD_Signal']:
            macd_cross = "bearish"

        # Генерація прогнозу
        forecast = None
        confidence = 0

        # Умови для LONG
        if ema_fast > ema_slow and 30 <= rsi <= 70 and macd_cross == "bullish":
            forecast = "LONG"
            confidence = 70 + (70 - rsi) / 70 * 20 if rsi < 50 else 70
            target_price = last['close'] * (1 + config.get('strategy.take_profit_percent', 2.0) / 100)

            # Створюємо прогноз
            self._create_auto_forecast(pair, "LONG", last['close'], target_price, confidence)

        # Умови для SHORT
        elif ema_fast < ema_slow and 30 <= rsi <= 70 and macd_cross == "bearish":
            forecast = "SHORT"
            confidence = 70 + (rsi - 30) / 70 * 20 if rsi > 50 else 70
            target_price = last['close'] * (1 - config.get('strategy.take_profit_percent', 2.0) / 100)

            # Створюємо прогноз
            self._create_auto_forecast(pair, "SHORT", last['close'], target_price, confidence)

        return {
            "pair": pair,
            "current_price": last['close'],
            "trend": trend,
            "trend_strength": round(trend_strength, 2),
            "ema_fast": round(ema_fast, 2),
            "ema_slow": round(ema_slow, 2),
            "rsi": round(rsi, 2),
            "rsi_signal": rsi_signal,
            "macd": round(macd, 6),
            "macd_signal": round(macd_signal, 6),
            "macd_cross": macd_cross,
            "forecast": forecast,
            "confidence": round(confidence, 2),
            "target_price": target_price if forecast else None,
            "timestamp": datetime.now().isoformat()
        }

    def _check_signals_sync(self, pair: str):
        """Перевірка сигналів для входу (синхронна) з детальним логуванням"""

        if not self.can_trade_now():
            logger.debug(f"[{pair}] Торгівля зараз не дозволена (поза робочими годинами)")
            return

        # Перевірка чи можна відкрити нову угоду
        can_open, reason = self.risk_manager.can_open_trade(pair)
        if not can_open:
            logger.debug(f"[{pair}] Не можна відкрити угоду: {reason}")
            return

        # Отримуємо дані для основного таймфрейму
        df_base = self.cache[pair].get(self.base_timeframe)
        if df_base is None or len(df_base) < 50:
            logger.debug(f"[{pair}] Недостатньо даних: {len(df_base) if df_base is not None else 0}")
            return

        last = df_base.iloc[-1]
        prev = df_base.iloc[-2]

        # Отримуємо значення індикаторів
        ema_fast_val = last[f'EMA_{config.get("strategy.ema_fast", 50)}']
        ema_slow_val = last[f'EMA_{config.get("strategy.ema_slow", 200)}']
        rsi_val = last['RSI']
        macd_val = last['MACD']
        macd_signal_val = last['MACD_Signal']
        prev_macd = prev['MACD']
        prev_signal = prev['MACD_Signal']

        # Детальна перевірка LONG
        logger.info(f"[{pair}] 📊 АНАЛІЗ СИГНАЛУ:")
        logger.info(f"  EMA: {ema_fast_val:.0f} vs {ema_slow_val:.0f} -> {'✅' if ema_fast_val > ema_slow_val else '❌'}")
        logger.info(f"  RSI: {rsi_val:.1f} -> {'✅' if 40 <= rsi_val <= 60 else '❌'} (потрібно 40-60)")
        logger.info(
            f"  MACD перетин: було {prev_macd:.2f} vs {prev_signal:.2f}, стало {macd_val:.2f} vs {macd_signal_val:.2f}")

        macd_bullish = macd_val > macd_signal_val and prev_macd <= prev_signal
        macd_bearish = macd_val < macd_signal_val and prev_macd >= prev_signal
        logger.info(f"  MACD бичачий: {'✅' if macd_bullish else '❌'}")

        # Перевірка об'єму
        volume_ratio = last['Volume_Ratio'] if 'Volume_Ratio' in last else 1.0
        min_volume = config.get('strategy.min_volume_ratio', 1.2)
        logger.info(
            f"  Об'єм: {volume_ratio:.2f}x -> {'✅' if volume_ratio > min_volume else '❌'} (потрібно >{min_volume}x)")

        # Перевірка LONG сигналу
        is_long = (ema_fast_val > ema_slow_val and
                   40 <= rsi_val <= 60 and
                   macd_bullish and
                   volume_ratio > min_volume)

        if is_long:
            logger.info(f"📈 [LONG] УМОВИ ВИКОНАНО для {pair}!")

            # Підтвердження на інших таймфреймах
            df_15m = self.cache[pair].get('15m')
            df_1h = self.cache[pair].get('1h')

            confirmed = self.strategies[pair].confirm_with_timeframe(df_15m, df_1h, "LONG")
            logger.info(f"  Підтвердження 15m/1h: {'✅' if confirmed else '❌'}")

            if confirmed:
                tp_price = last['close'] * (1 + config.get('strategy.take_profit_percent', 2.0) / 100)

                # Створюємо прогноз
                self._create_auto_forecast(pair, "LONG", last['close'], tp_price, 85)

                # ВИКОНУЄМО УГОДУ
                logger.info(f"🚀 ВИКОНАННЯ LONG угоди для {pair} за ціною {last['close']:.2f}")
                self._execute_trade_sync(pair, "LONG", last['close'])
            else:
                logger.info(f"⚠️ LONG сигнал для {pair}, але НЕМАЄ підтвердження на 15m/1h")
        else:
            # Перевірка SHORT
            is_short = (ema_fast_val < ema_slow_val and
                        40 <= rsi_val <= 60 and
                        macd_bearish and
                        volume_ratio > min_volume)

            if is_short:
                logger.info(f"📉 [SHORT] УМОВИ ВИКОНАНО для {pair}!")

                df_15m = self.cache[pair].get('15m')
                df_1h = self.cache[pair].get('1h')

                confirmed = self.strategies[pair].confirm_with_timeframe(df_15m, df_1h, "SHORT")
                logger.info(f"  Підтвердження 15m/1h: {'✅' if confirmed else '❌'}")

                if confirmed:
                    tp_price = last['close'] * (1 - config.get('strategy.take_profit_percent', 2.0) / 100)
                    self._create_auto_forecast(pair, "SHORT", last['close'], tp_price, 85)
                    logger.info(f"🚀 ВИКОНАННЯ SHORT угоди для {pair} за ціною {last['close']:.2f}")
                    self._execute_trade_sync(pair, "SHORT", last['close'])
                else:
                    logger.info(f"⚠️ SHORT сигнал для {pair}, але НЕМАЄ підтвердження на 15m/1h")
            else:
                logger.debug(
                    f"[{pair}] Немає сигналу: EMA={ema_fast_val:.0f} {'>' if ema_fast_val > ema_slow_val else '<'} {ema_slow_val:.0f}, RSI={rsi_val:.1f}, MACD перетин={'✅' if macd_bullish or macd_bearish else '❌'}")

    def _execute_trade_sync(self, pair: str, signal_type: str, current_price: float):
        """Виконання угоди (синхронна) - прогноз вже створено в _check_signals_sync"""

        # Отримуємо баланс
        balance = self.db.get_balance("USDT", is_paper=True)

        # Розрахунок TP/SL
        tp_percent = config.get('strategy.take_profit_percent', 2.0)
        sl_percent = config.get('strategy.stop_loss_percent', 1.5)

        if signal_type == "LONG":
            tp_price = current_price * (1 + tp_percent / 100)
            sl_price = current_price * (1 - sl_percent / 100)

            quantity = self.risk_manager.calculate_position_size(balance, current_price, sl_price)

            if quantity <= 0:
                logger.warning(f"Невірний об'єм для {pair}: {quantity}")
                return

            result = self.paper_engine.execute_buy(pair, quantity, current_price)

            if result:
                self.db.update_trade(result['trade_id'], {
                    'take_profit': tp_price,
                    'stop_loss': sl_price
                })

                # Сповіщення в Telegram
                if self.telegram:
                    asyncio.create_task(self.telegram.send_trade_notification({
                        'pair': pair,
                        'side': 'LONG',
                        'quantity': quantity,
                        'entry_price': result['execution_price'],
                        'tp': tp_price,
                        'sl': sl_price,
                        'balance': balance - (quantity * current_price)
                    }))

        elif signal_type == "SHORT":
            tp_price = current_price * (1 - tp_percent / 100)
            sl_price = current_price * (1 + sl_percent / 100)

            quantity = self.risk_manager.calculate_position_size(balance, current_price, sl_price)

            if quantity <= 0:
                logger.warning(f"Невірний об'єм для {pair}: {quantity}")
                return

            result = self.paper_engine.execute_short(pair, quantity, current_price)

            if result:
                self.db.update_trade(result['trade_id'], {
                    'take_profit': tp_price,
                    'stop_loss': sl_price
                })

                if self.telegram:
                    asyncio.create_task(self.telegram.send_trade_notification({
                        'pair': pair,
                        'side': 'SHORT',
                        'quantity': quantity,
                        'entry_price': result['execution_price'],
                        'tp': tp_price,
                        'sl': sl_price,
                        'balance': balance
                    }))

    def _create_auto_forecast(self, pair: str, signal_type: str, entry_price: float, target_price: float,
                              confidence: float):
        """Автоматичне створення прогнозу (без дублювання)"""

        # ПЕРЕВІРКА НА ДУБЛЮВАННЯ
        from web.app import active_forecasts
        import time

        # Перевіряємо чи вже є активний прогноз для цієї пари з таким самим сигналом
        for f in active_forecasts.values():
            if (f.pair == pair and
                    f.signal_type == signal_type and
                    f.status == "active" and
                    abs(f.entry_price - entry_price) / entry_price < 0.01):  # 1% допуск
                logger.debug(f"Прогноз для {pair} {signal_type} вже існує, пропускаємо")
                return

        logger.info(
            f"🔮 Створення автоматичного прогнозу: {pair} {signal_type} | Вхід: {entry_price} -> Ціль: {target_price}")

        def send_forecast():
            try:
                import asyncio
                try:
                    loop = asyncio.get_event_loop()
                except RuntimeError:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)

                async def create_forecast_internal():
                    try:
                        from web.app import active_forecasts, Forecast
                        import sys
                        from pathlib import Path
                        sys.path.insert(0, str(Path(__file__).parent.parent))

                        # Перевіряємо ще раз перед створенням
                        for f in active_forecasts.values():
                            if (f.pair == pair and
                                    f.signal_type == signal_type and
                                    f.status == "active" and
                                    abs(f.entry_price - entry_price) / entry_price < 0.01):
                                return None

                        forecast = Forecast(pair, signal_type, entry_price, target_price, confidence)
                        active_forecasts[forecast.id] = forecast

                        from web.app import active_websockets
                        for ws in active_websockets:
                            try:
                                await ws.send_json({
                                    "type": "new_forecast",
                                    "forecast": forecast.to_dict()
                                })
                            except:
                                pass

                        logger.info(f"✅ Прогноз створено: {pair} {signal_type}")
                        return forecast
                    except Exception as e:
                        logger.error(f"Помилка створення прогнозу: {e}")
                        return None

                if loop.is_running():
                    asyncio.create_task(create_forecast_internal())
                else:
                    loop.run_until_complete(create_forecast_internal())
            except Exception as e:
                logger.error(f"Помилка в потоці створення прогнозу: {e}")

        import threading
        thread = threading.Thread(target=send_forecast, daemon=True)
        thread.start()

    def _check_exits_sync(self, pair: str):
        """Перевірка виходу для відкритих позицій (синхронна)"""

        open_trades = self.db.get_open_trades(pair=pair, is_paper=True)

        if not open_trades:
            return

        # Отримуємо поточну ціну
        current_price = self.exchange.get_current_price(pair)
        if not current_price:
            return

        for trade in open_trades:
            should_exit = False
            exit_reason = ""

            if trade.side == OrderSide.BUY:  # LONG
                if current_price >= trade.take_profit:
                    should_exit = True
                    exit_reason = "TAKE_PROFIT"
                elif current_price <= trade.stop_loss:
                    should_exit = True
                    exit_reason = "STOP_LOSS"
            else:  # SHORT
                if current_price <= trade.take_profit:
                    should_exit = True
                    exit_reason = "TAKE_PROFIT"
                elif current_price >= trade.stop_loss:
                    should_exit = True
                    exit_reason = "STOP_LOSS"

            if should_exit:
                result = self.paper_engine.execute_sell(trade.id, current_price)

                if result and self.telegram:
                    new_balance = self.db.get_balance("USDT", is_paper=True)
                    asyncio.create_task(self.telegram.send_close_notification({
                        'pair': trade.pair,
                        'side': trade.side.value,
                        'entry_price': trade.entry_price,
                        'exit_price': result['execution_price'],
                        'pnl': result['pnl'],
                        'pnl_percent': result['pnl_percent'],
                        'reason': exit_reason,
                        'balance': new_balance
                    }))

                logger.info(f"Closed {trade.pair} {trade.side.value} - {exit_reason} | PnL: {result['pnl']:.2f}")

    async def _check_signals(self, pair: str):
        """Перевірка сигналів для входу"""

        # Перевірка чи можна відкрити нову угоду
        can_open, reason = self.risk_manager.can_open_trade(pair)
        if not can_open:
            logger.debug(f"Cannot open trade: {reason}")
            return

        # Отримуємо дані для основного таймфрейму
        df_base = self.cache[pair].get(self.base_timeframe)
        if df_base is None or len(df_base) < 50:
            return

        # Отримуємо дані для підтвердження
        df_15m = self.cache[pair].get('15m') if '15m' in self.confirm_timeframes else None
        df_1h = self.cache[pair].get('1h') if '1h' in self.confirm_timeframes else None

        # Перевірка LONG сигналу
        is_long, indicators = self.strategies[pair].check_long_signal(df_base)

        if is_long:
            # Підтвердження на інших таймфреймах
            if self.strategies[pair].confirm_with_timeframe(df_15m, df_1h, "LONG"):
                logger.info(f"📈 LONG signal for {pair} at {indicators['price']:.2f}")

                # Збереження сигналу
                self.db.save_signal(pair, SignalType.LONG, indicators['price'], 0.8, indicators)

                # Виконання угоди
                await self._execute_trade(pair, "LONG", indicators['price'])

        # Перевірка SHORT сигналу
        is_short, indicators = self.strategies[pair].check_short_signal(df_base)

        if is_short:
            # Підтвердження на інших таймфреймах
            if self.strategies[pair].confirm_with_timeframe(df_15m, df_1h, "SHORT"):
                logger.info(f"📉 SHORT signal for {pair} at {indicators['price']:.2f}")

                # Збереження сигналу
                self.db.save_signal(pair, SignalType.SHORT, indicators['price'], 0.8, indicators)

                # Виконання угоди
                await self._execute_trade(pair, "SHORT", indicators['price'])

    async def _execute_trade(self, pair: str, signal_type: str, current_price: float):
        """Виконання угоди"""

        # Отримуємо баланс
        balance = self.db.get_balance("USDT", is_paper=True)

        # Розрахунок TP/SL
        tp_percent = config.get('strategy.take_profit_percent', 2.0)
        sl_percent = config.get('strategy.stop_loss_percent', 1.5)

        if signal_type == "LONG":
            tp_price = current_price * (1 + tp_percent / 100)
            sl_price = current_price * (1 - sl_percent / 100)

            # Розрахунок розміру позиції
            quantity = self.risk_manager.calculate_position_size(balance, current_price, sl_price)

            if quantity <= 0:
                logger.warning(f"Invalid quantity for {pair}: {quantity}")
                return

            # Виконання buy
            result = self.paper_engine.execute_buy(pair, quantity, current_price)

            if result:
                # Оновлення TP/SL в БД
                self.db.update_trade(result['trade_id'], {
                    'take_profit': tp_price,
                    'stop_loss': sl_price
                })

                # Сповіщення в Telegram
                if self.telegram:
                    await self.telegram.send_trade_notification({
                        'pair': pair,
                        'side': 'LONG',
                        'quantity': quantity,
                        'entry_price': result['execution_price'],
                        'tp': tp_price,
                        'sl': sl_price,
                        'balance': balance - (quantity * current_price)
                    })

        elif signal_type == "SHORT":
            tp_price = current_price * (1 - tp_percent / 100)
            sl_price = current_price * (1 + sl_percent / 100)

            # Для short - розрахунок позиції
            quantity = self.risk_manager.calculate_position_size(balance, current_price, sl_price)

            if quantity <= 0:
                logger.warning(f"Invalid quantity for {pair}: {quantity}")
                return

            # Виконання short
            result = self.paper_engine.execute_short(pair, quantity, current_price)

            if result:
                self.db.update_trade(result['trade_id'], {
                    'take_profit': tp_price,
                    'stop_loss': sl_price
                })

                if self.telegram:
                    await self.telegram.send_trade_notification({
                        'pair': pair,
                        'side': 'SHORT',
                        'quantity': quantity,
                        'entry_price': result['execution_price'],
                        'tp': tp_price,
                        'sl': sl_price,
                        'balance': balance
                    })

    async def _check_exits(self, pair: str):
        """Перевірка виходу для відкритих позицій"""

        open_trades = self.db.get_open_trades(pair=pair, is_paper=True)

        if not open_trades:
            return

        # Отримуємо поточну ціну
        current_price = self.exchange.get_current_price(pair)
        if not current_price:
            return

        # Отримуємо дані для перевірки сигналів виходу
        df_base = self.cache[pair].get(self.base_timeframe)

        for trade in open_trades:
            # Перевірка TP/SL
            should_exit = False
            exit_reason = ""

            if trade.side == OrderSide.BUY:  # LONG
                if current_price >= trade.take_profit:
                    should_exit = True
                    exit_reason = "TAKE_PROFIT"
                elif current_price <= trade.stop_loss:
                    should_exit = True
                    exit_reason = "STOP_LOSS"
            else:  # SHORT
                if current_price <= trade.take_profit:
                    should_exit = True
                    exit_reason = "TAKE_PROFIT"
                elif current_price >= trade.stop_loss:
                    should_exit = True
                    exit_reason = "STOP_LOSS"

            if should_exit:
                # Закриття угоди
                result = self.paper_engine.execute_sell(trade.id, current_price)

                if result and self.telegram:
                    # Отримуємо оновлений баланс
                    new_balance = self.db.get_balance("USDT", is_paper=True)

                    await self.telegram.send_close_notification({
                        'pair': trade.pair,
                        'side': trade.side.value,
                        'entry_price': trade.entry_price,
                        'exit_price': result['execution_price'],
                        'pnl': result['pnl'],
                        'pnl_percent': result['pnl_percent'],
                        'reason': exit_reason,
                        'balance': new_balance
                    })

                logger.info(f"Closed {trade.pair} {trade.side.value} - {exit_reason} | PnL: {result['pnl']:.2f}")

    def can_trade_now(self) -> bool:
        """Перевірка, чи можна торгувати зараз (години торгівлі)"""
        hours_config = config.get('trading.trading_hours', {})

        if not hours_config.get('enabled', False):
            return True

        from datetime import datetime
        import pytz

        timezone = pytz.timezone(hours_config.get('timezone', 'Europe/Kiev'))
        now = datetime.now(timezone)

        start = datetime.strptime(hours_config.get('start', '09:00'), '%H:%M').time()
        end = datetime.strptime(hours_config.get('end', '21:00'), '%H:%M').time()

        current_time = now.time()

        if start <= current_time <= end:
            return True
        return False

    async def daily_reset_check(self):
        """Перевірка скидання денних лімітів"""
        self.risk_manager.update_daily_check()

        # Відправка денного звіту
        if self.telegram and datetime.now().hour == 0 and datetime.now().minute < 5:
            stats = self.db.get_stats(is_paper=True)
            stats['balance'] = self.db.get_balance("USDT", is_paper=True)
            stats['stopped'] = not self.running
            await self.telegram.send_daily_report(stats)

            # Очищення старих логів
            retention_days = config.get('database.log_retention_days', 7)
            self.db.cleanup_old_logs(retention_days)

    def stop(self):
        """Зупинка бота"""
        self.running = False
        logger.info("OrderManager зупинено")

    async def run(self):
        """Головний цикл бота"""
        logger.info("OrderManager main loop started")

        # Для оновлення цін прогнозів
        last_price_update = datetime.now()

        while self.running:
            try:
                await self.daily_reset_check()

                # Оновлення цін прогнозів кожні 10 секунд
                if (datetime.now() - last_price_update).seconds >= 10:
                    from web.app import update_forecast_prices
                    await update_forecast_prices()
                    last_price_update = datetime.now()

                # Аналіз ринку кожні 5 хвилин
                current_minute = datetime.now().minute
                if current_minute % 5 == 0:
                    for pair in self.pairs:
                        try:
                            analysis = self.analyze_market(pair)
                            if analysis.get('forecast') and analysis.get('confidence', 0) > 70:
                                logger.info(f"📊 АНАЛІЗ {pair}: {analysis['trend']}")
                        except Exception as e:
                            logger.debug(f"Помилка аналізу {pair}: {e}")

                await asyncio.sleep(self.signal_check_interval)

            except Exception as e:
                logger.error(f"OrderManager loop error: {e}")
                await asyncio.sleep(5)