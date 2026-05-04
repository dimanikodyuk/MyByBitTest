import asyncio
import pandas as pd
from typing import Dict, Optional, List, Any
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

        logger.info(f"✅ OrderManager ініціалізовано для {len(self.pairs)} пар")

    def _init_strategies(self):
        """Ініціалізація стратегій для всіх пар"""
        for pair in self.pairs:
            self.strategies[pair] = TradingStrategy(pair)
            self.cache[pair] = {}
            logger.info(f"📊 Стратегія ініціалізована для {pair}")

    def _load_initial_data(self):
        """Завантаження початкових даних для всіх пар"""
        for pair in self.pairs:
            for tf in self.timeframes:
                df = self.exchange.get_klines(pair, tf, limit=200)
                if df is not None and len(df) > 0:
                    df = self.strategies[pair].calculate_indicators(df)
                    self.cache[pair][tf] = df
                    logger.debug(f"📥 Завантажено {len(df)} свічок для {pair} {tf}")

    def on_new_candle(self, pair: str, candle: Dict):
        """Обробка нової свічки (синхронна)"""
        if not self.running:
            return

        timeframe = self.base_timeframe
        logger.debug(f"🕯️ Нова свічка для {pair} о {candle['timestamp']}")

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

        # Синхронні перевірки
        self._check_signals_sync(pair)
        self._check_exits_sync(pair)

    def analyze_market(self, pair: str) -> Dict[str, Any]:
        """Аналіз ринку та генерація прогнозів на основі технічних індикаторів"""

        df = self.cache[pair].get(self.base_timeframe)
        if df is None or len(df) < 50:
            logger.warning(f"⚠️ [{pair}] Недостатньо даних для аналізу")
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

            logger.info(
                f"📊 [{pair}] АНАЛІЗ: Тренд={trend}, RSI={rsi:.1f}, MACD={macd_cross} → Прогноз {forecast} (впевн.{confidence:.0f}%)")

            # Створюємо прогноз
            self._create_auto_forecast(pair, "LONG", last['close'], target_price, confidence)

        # Умови для SHORT
        elif ema_fast < ema_slow and 30 <= rsi <= 70 and macd_cross == "bearish":
            forecast = "SHORT"
            confidence = 70 + (rsi - 30) / 70 * 20 if rsi > 50 else 70
            target_price = last['close'] * (1 - config.get('strategy.take_profit_percent', 2.0) / 100)

            logger.info(
                f"📊 [{pair}] АНАЛІЗ: Тренд={trend}, RSI={rsi:.1f}, MACD={macd_cross} → Прогноз {forecast} (впевн.{confidence:.0f}%)")

            # Створюємо прогноз
            self._create_auto_forecast(pair, "SHORT", last['close'], target_price, confidence)
        else:
            logger.debug(f"📊 [{pair}] АНАЛІЗ: Тренд={trend}, RSI={rsi:.1f}, MACD={macd_cross} → Немає сигналу")

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

    def stop(self):
        """Зупинка бота"""
        self.running = False
        logger.info("🛑 OrderManager зупинено")

        # Форсоване закриття всіх задач
        import asyncio
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # Відміняємо всі задачі
                for task in asyncio.all_tasks(loop):
                    if task != asyncio.current_task():
                        task.cancel()
        except:
            pass

    def _check_signals_sync(self, pair: str):
        """Перевірка сигналів для входу (синхронна) - покращене логування"""

        if not self.can_trade_now():
            logger.debug(f"⏰ [{pair}] Поза робочими годинами")
            return

        # Перевірка чи можна відкрити нову угоду
        can_open, reason = self.risk_manager.can_open_trade(pair)
        if not can_open:
            logger.info(f"❌ [{pair}] Угода не створена: {reason}")
            return

        # Отримуємо дані для основного таймфрейму
        df_base = self.cache[pair].get(self.base_timeframe)
        if df_base is None or len(df_base) < 50:
            logger.debug(f"⚠️ [{pair}] Недостатньо даних: {len(df_base) if df_base is not None else 0}")
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

        macd_bullish = macd_val > macd_signal_val and prev_macd <= prev_signal
        macd_bearish = macd_val < macd_signal_val and prev_macd >= prev_signal

        # Перевірка об'єму
        volume_ratio = last['Volume_Ratio'] if 'Volume_Ratio' in last else 1.0
        min_volume = config.get('strategy.min_volume_ratio', 1.2)

        # Формуємо рядок умов для логування
        ema_ok = ema_fast_val > ema_slow_val
        rsi_ok = 40 <= rsi_val <= 60
        macd_ok = macd_bullish
        volume_ok = volume_ratio > min_volume

        conditions = f"EMA:{'✅' if ema_ok else '❌'} RSI:{'✅' if rsi_ok else '❌'} MACD:{'✅' if macd_ok else '❌'} VOL:{'✅' if volume_ok else '❌'}"

        # Перевірка LONG сигналу
        is_long = (ema_ok and rsi_ok and macd_bullish and volume_ok)

        if is_long:
            logger.info(f"🔍 [{pair}] Умови LONG: {conditions}")

            # Підтвердження на інших таймфреймах
            df_15m = self.cache[pair].get('15m')
            df_1h = self.cache[pair].get('1h')

            confirmed = self.strategies[pair].confirm_with_timeframe(df_15m, df_1h, "LONG")

            if confirmed:
                tp_price = last['close'] * (1 + config.get('strategy.take_profit_percent', 2.0) / 100)

                logger.info(f"🎯 [{pair}] LONG СИГНАЛ! Ціна={last['close']:.2f}, TP={tp_price:.2f}, RSI={rsi_val:.1f}")

                # Створюємо прогноз
                self._create_auto_forecast(pair, "LONG", last['close'], tp_price, 85)

                # ВИКОНУЄМО УГОДУ
                logger.info(f"🚀 [{pair}] ВИКОНАННЯ LONG угоди!")
                self._execute_trade_sync(pair, "LONG", last['close'])
            else:
                logger.info(f"⚠️ [{pair}] LONG сигнал, але НЕМАЄ підтвердження на 15m/1h")
        else:
            # Перевірка SHORT
            ema_ok_short = ema_fast_val < ema_slow_val
            macd_ok_short = macd_bearish

            conditions_short = f"EMA:{'✅' if ema_ok_short else '❌'} RSI:{'✅' if rsi_ok else '❌'} MACD:{'✅' if macd_ok_short else '❌'} VOL:{'✅' if volume_ok else '❌'}"

            is_short = (ema_ok_short and rsi_ok and macd_bearish and volume_ok)

            if is_short:
                logger.info(f"🔍 [{pair}] Умови SHORT: {conditions_short}")

                df_15m = self.cache[pair].get('15m')
                df_1h = self.cache[pair].get('1h')

                confirmed = self.strategies[pair].confirm_with_timeframe(df_15m, df_1h, "SHORT")

                if confirmed:
                    tp_price = last['close'] * (1 - config.get('strategy.take_profit_percent', 2.0) / 100)

                    logger.info(
                        f"🎯 [{pair}] SHORT СИГНАЛ! Ціна={last['close']:.2f}, TP={tp_price:.2f}, RSI={rsi_val:.1f}")

                    self._create_auto_forecast(pair, "SHORT", last['close'], tp_price, 85)

                    logger.info(f"🚀 [{pair}] ВИКОНАННЯ SHORT угоди!")
                    self._execute_trade_sync(pair, "SHORT", last['close'])
                else:
                    logger.info(f"⚠️ [{pair}] SHORT сигнал, але НЕМАЄ підтвердження на 15m/1h")
            else:
                # Компактне логування коли немає сигналу
                if ema_ok:
                    direction = "LONG"
                elif ema_ok_short:
                    direction = "SHORT"
                else:
                    direction = "FLAT"

                logger.debug(
                    f"📊 [{pair}] {direction} | RSI={rsi_val:.1f} | MACD={'↑' if macd_bullish else '↓' if macd_bearish else '='} | VOL={volume_ratio:.1f}x → Немає сигналу")

    def _execute_trade_sync(self, pair: str, signal_type: str, current_price: float):
        """Виконання угоди (синхронна)"""

        # Отримуємо баланс
        balance = self.db.get_balance("USDT", is_paper=True)

        tp_percent = config.get('strategy.take_profit_percent', 2.0)
        sl_percent = config.get('strategy.stop_loss_percent', 1.5)

        if signal_type == "LONG":
            tp_price = current_price * (1 + tp_percent / 100)
            sl_price = current_price * (1 - sl_percent / 100)

            quantity = self.risk_manager.calculate_position_size(balance, current_price, sl_price, pair)

            if quantity <= 0:
                logger.warning(f"❌ [{pair}] Невірний об'єм: {quantity}")
                return

            result = self.paper_engine.execute_buy(pair, quantity, current_price)

            if result:
                self.db.update_trade(result['trade_id'], {
                    'take_profit': tp_price,
                    'stop_loss': sl_price
                })

                logger.info(
                    f"✅ [{pair}] LONG угода відкрита: {quantity} @ {result['execution_price']:.2f} | TP={tp_price:.2f} SL={sl_price:.2f}")

                if self.telegram:
                    asyncio.create_task(self.telegram.send_trade_notification({
                        'pair': pair, 'side': 'LONG', 'quantity': quantity,
                        'entry_price': result['execution_price'], 'tp': tp_price, 'sl': sl_price,
                        'balance': balance - (quantity * current_price)
                    }))

        elif signal_type == "SHORT":
            tp_price = current_price * (1 - tp_percent / 100)
            sl_price = current_price * (1 + sl_percent / 100)

            quantity = self.risk_manager.calculate_position_size(balance, current_price, sl_price, pair)

            if quantity <= 0:
                logger.warning(f"❌ [{pair}] Невірний об'єм: {quantity}")
                return

            result = self.paper_engine.execute_short(pair, quantity, current_price)

            if result:
                self.db.update_trade(result['trade_id'], {
                    'take_profit': tp_price,
                    'stop_loss': sl_price
                })

                logger.info(
                    f"✅ [{pair}] SHORT угода відкрита: {quantity} @ {result['execution_price']:.2f} | TP={tp_price:.2f} SL={sl_price:.2f}")

                if self.telegram:
                    asyncio.create_task(self.telegram.send_trade_notification({
                        'pair': pair, 'side': 'SHORT', 'quantity': quantity,
                        'entry_price': result['execution_price'], 'tp': tp_price, 'sl': sl_price,
                        'balance': balance
                    }))

    def _create_auto_forecast(self, pair: str, signal_type: str, entry_price: float, target_price: float,
                              confidence: float):
        """Автоматичне створення прогнозу"""

        import threading

        logger.info(
            f"🔮 [{pair}] Створення прогнозу: {signal_type} | {entry_price:.0f} → {target_price:.0f} (впевн.{confidence:.0f}%)")

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
                        from web.app import create_forecast_internal as create_fc
                        result = await create_fc(pair, signal_type, entry_price, target_price, confidence)
                        if result:
                            logger.info(f"✅ [{pair}] Прогноз збережено в БД")
                        else:
                            logger.debug(f"⚠️ [{pair}] Прогноз вже існує")
                    except Exception as e:
                        logger.error(f"❌ [{pair}] Помилка створення прогнозу: {e}")

                if loop.is_running():
                    asyncio.create_task(create_forecast_internal())
                else:
                    loop.run_until_complete(create_forecast_internal())
            except Exception as e:
                logger.error(f"❌ [{pair}] Помилка в потоці прогнозу: {e}")

        thread = threading.Thread(target=send_forecast, daemon=True)
        thread.start()

    def _check_exits_sync(self, pair: str):
        """Перевірка виходу для відкритих позицій"""

        open_trades = self.db.get_open_trades(pair=pair, is_paper=True)

        if not open_trades:
            return

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

                if result:
                    logger.info(
                        f"💰 [{pair}] Угода закрита: {exit_reason} | PnL={result['pnl']:.2f} USDT ({result['pnl_percent']:.1f}%)")

                    if self.telegram:
                        new_balance = self.db.get_balance("USDT", is_paper=True)
                        asyncio.create_task(self.telegram.send_close_notification({
                            'pair': trade.pair, 'side': trade.side.value,
                            'entry_price': trade.entry_price, 'exit_price': result['execution_price'],
                            'pnl': result['pnl'], 'pnl_percent': result['pnl_percent'],
                            'reason': exit_reason, 'balance': new_balance
                        }))

    def can_trade_now(self) -> bool:
        """Перевірка, чи можна торгувати зараз"""
        hours_config = config.get('trading.trading_hours', {})

        if not hours_config.get('enabled', False):
            return True

        import pytz
        now = datetime.now(pytz.timezone(hours_config.get('timezone', 'Europe/Kiev')))
        start = datetime.strptime(hours_config.get('start', '09:00'), '%H:%M').time()
        end = datetime.strptime(hours_config.get('end', '21:00'), '%H:%M').time()

        return start <= now.time() <= end

    async def daily_reset_check(self):
        """Перевірка скидання денних лімітів"""
        self.risk_manager.update_daily_check()

        if self.telegram and datetime.now().hour == 0 and datetime.now().minute < 5:
            stats = self.db.get_stats(is_paper=True)
            stats['balance'] = self.db.get_balance("USDT", is_paper=True)
            stats['stopped'] = not self.running
            await self.telegram.send_daily_report(stats)

            retention_days = config.get('database.log_retention_days', 7)
            self.db.cleanup_old_logs(retention_days)


    async def run(self):
        """Головний цикл бота"""
        logger.info("🔄 OrderManager основний цикл запущено")

        last_price_update = datetime.now()
        last_analysis_minute = -1

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
                if current_minute != last_analysis_minute and current_minute % 5 == 0:
                    last_analysis_minute = current_minute
                    for pair in self.pairs:
                        try:
                            analysis = self.analyze_market(pair)
                            if analysis.get('forecast') and analysis.get('confidence', 0) > 70:
                                logger.info(
                                    f"📈 АКТИВНИЙ ПРОГНОЗ: {pair} {analysis['forecast']} з впевненістю {analysis['confidence']}%")
                        except Exception as e:
                            logger.debug(f"Помилка аналізу {pair}: {e}")

                await asyncio.sleep(self.signal_check_interval)

            except Exception as e:
                logger.error(f"❌ Помилка циклу OrderManager: {e}")
                await asyncio.sleep(5)