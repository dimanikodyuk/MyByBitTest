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
        self.start_time = datetime.now()

        self.paper_engine = PaperEngine(db_ops)
        self.risk_manager = RiskManager(db_ops, is_paper=True)

        self.strategies: Dict[str, TradingStrategy] = {}
        self.cache: Dict[str, Dict[str, pd.DataFrame]] = {}

        self.running = True
        self.pairs = config.get('trading.pairs', ['BTCUSDT'])

        self.base_timeframe = config.get('trading.base_timeframe', '5m')
        self.confirm_timeframes = config.get('trading.confirmation_timeframes', ['15m', '1h'])
        self.signal_check_interval = config.get('trading.signal_check_interval', 30)

        self._init_strategies()
        self._load_initial_data()

        logger.info(f"✅ OrderManager ініціалізовано для {len(self.pairs)} пар")

    def _init_strategies(self):
        for pair in self.pairs:
            self.strategies[pair] = TradingStrategy(pair)
            self.cache[pair] = {}
            logger.info(f"📊 Стратегія ініціалізована для {pair}")

    def _load_initial_data(self):
        for pair in self.pairs:
            for tf in self.timeframes:
                df = self.exchange.get_klines(pair, tf, limit=200)
                if df is not None and len(df) > 0:
                    df = self.strategies[pair].calculate_indicators(df)
                    self.cache[pair][tf] = df
                    logger.debug(f"📥 Завантажено {len(df)} свічок для {pair} {tf}")

    def on_new_candle(self, pair: str, candle: Dict):
        if not self.running:
            return

        timeframe = self.base_timeframe
        logger.debug(f"🕯️ Нова свічка для {pair} о {candle['timestamp']}")

        if pair not in self.cache:
            self.cache[pair] = {}

        new_df = pd.DataFrame([candle])
        if pair in self.cache and timeframe in self.cache[pair]:
            self.cache[pair][timeframe] = pd.concat([self.cache[pair][timeframe], new_df], ignore_index=True)
            self.cache[pair][timeframe] = self.cache[pair][timeframe].tail(200)
        else:
            self.cache[pair][timeframe] = new_df

        df = self.strategies[pair].calculate_indicators(self.cache[pair][timeframe])
        self.cache[pair][timeframe] = df

        self._check_signals_sync(pair)
        self._check_exits_sync(pair)

    def analyze_market(self, pair: str) -> Dict[str, Any]:
        df = self.cache[pair].get(self.base_timeframe)
        if df is None or len(df) < 50:
            logger.warning(f"⚠️ [{pair}] Недостатньо даних для аналізу")
            return {"error": "Недостатньо даних"}

        last = df.iloc[-1]
        prev = df.iloc[-2]

        ema_fast = last[f'EMA_{config.get("strategy.ema_fast", 50)}']
        ema_slow = last[f'EMA_{config.get("strategy.ema_slow", 200)}']
        rsi = last['RSI']
        macd = last['MACD']
        macd_signal = last['MACD_Signal']

        trend = "📈 ВИСХІДНИЙ" if ema_fast > ema_slow else "📉 НИЗХІДНИЙ"
        trend_strength = abs((ema_fast - ema_slow) / ema_slow * 100)

        rsi_signal = "neutral"
        if rsi > 70:
            rsi_signal = "overbought"
        elif rsi < 30:
            rsi_signal = "oversold"

        macd_cross = "neutral"
        if macd > macd_signal and prev['MACD'] <= prev['MACD_Signal']:
            macd_cross = "bullish"
        elif macd < macd_signal and prev['MACD'] >= prev['MACD_Signal']:
            macd_cross = "bearish"

        forecast = None
        confidence = 0
        target_price = None

        if ema_fast > ema_slow and 30 <= rsi <= 70 and macd_cross == "bullish":
            forecast = "LONG"
            confidence = 70 + (70 - rsi) / 70 * 20 if rsi < 50 else 70
            target_price = last['close'] * (1 + config.get('strategy.take_profit_percent', 2.0) / 100)

            logger.info(f"📊 [{pair}] АНАЛІЗ → Прогноз {forecast} (впевн.{confidence:.0f}%)")
            self._create_auto_forecast(pair, "LONG", last['close'], target_price, confidence)

        elif ema_fast < ema_slow and 30 <= rsi <= 70 and macd_cross == "bearish":
            forecast = "SHORT"
            confidence = 70 + (rsi - 30) / 70 * 20 if rsi > 50 else 70
            target_price = last['close'] * (1 - config.get('strategy.take_profit_percent', 2.0) / 100)

            logger.info(f"📊 [{pair}] АНАЛІЗ → Прогноз {forecast} (впевн.{confidence:.0f}%)")
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
            "target_price": target_price,
            "timestamp": datetime.now().isoformat()
        }

    def stop(self):
        self.running = False
        logger.info("🛑 OrderManager зупинено")

    def _check_signals_sync(self, pair: str):
        if not self.can_trade_now():
            logger.debug(f"⏰ [{pair}] Поза робочими годинами")
            return

        # ПЕРЕВІРКА: чи вже є відкрита позиція по цій парі
        existing_trades = self.db.get_open_trades(pair=pair, is_paper=True)
        if existing_trades:
            logger.debug(f"[{pair}] Вже є відкрита позиція, пропускаємо новий сигнал")
            return

        can_open, reason = self.risk_manager.can_open_trade(pair)
        if not can_open:
            logger.info(f"❌ [{pair}] Угода не створена: {reason}")
            return

        df_base = self.cache[pair].get(self.base_timeframe)
        if df_base is None or len(df_base) < 50:
            logger.debug(f"⚠️ [{pair}] Недостатньо даних: {len(df_base) if df_base is not None else 0}")
            return

        last = df_base.iloc[-1]
        prev = df_base.iloc[-2]

        ema_fast_val = last[f'EMA_{config.get("strategy.ema_fast", 50)}']
        ema_slow_val = last[f'EMA_{config.get("strategy.ema_slow", 200)}']
        rsi_val = last['RSI']
        macd_val = last['MACD']
        macd_signal_val = last['MACD_Signal']
        prev_macd = prev['MACD']
        prev_signal = prev['MACD_Signal']

        macd_bullish = macd_val > macd_signal_val and prev_macd <= prev_signal
        macd_bearish = macd_val < macd_signal_val and prev_macd >= prev_signal

        volume_ratio = last['Volume_Ratio'] if 'Volume_Ratio' in last else 1.0
        min_volume = config.get('strategy.min_volume_ratio', 1.2)

        ema_ok = ema_fast_val > ema_slow_val
        rsi_ok = 40 <= rsi_val <= 60
        macd_ok = macd_bullish
        volume_ok = volume_ratio > min_volume

        is_long = (ema_ok and rsi_ok and macd_bullish and volume_ok)

        if is_long:
            logger.info(f"🔍 [{pair}] Умови LONG: EMA:{ema_ok} RSI:{rsi_ok} MACD:{macd_ok} VOL:{volume_ok}")

            df_15m = self.cache[pair].get('15m')
            df_1h = self.cache[pair].get('1h')
            confirmed = self.strategies[pair].confirm_with_timeframe(df_15m, df_1h, "LONG")

            if confirmed:
                tp_price = last['close'] * (1 + config.get('strategy.take_profit_percent', 2.0) / 100)
                logger.info(f"🎯 [{pair}] LONG СИГНАЛ! Ціна={last['close']:.2f}, TP={tp_price:.2f}")
                self._create_auto_forecast(pair, "LONG", last['close'], tp_price, 85)
                self._execute_trade_sync(pair, "LONG", last['close'])
            else:
                logger.info(f"⚠️ [{pair}] LONG сигнал, але НЕМАЄ підтвердження на 15m/1h")

        else:
            ema_ok_short = ema_fast_val < ema_slow_val
            macd_ok_short = macd_bearish
            is_short = (ema_ok_short and rsi_ok and macd_bearish and volume_ok)

            if is_short:
                logger.info(f"🔍 [{pair}] Умови SHORT: EMA:{ema_ok_short} RSI:{rsi_ok} MACD:{macd_ok_short} VOL:{volume_ok}")

                df_15m = self.cache[pair].get('15m')
                df_1h = self.cache[pair].get('1h')
                confirmed = self.strategies[pair].confirm_with_timeframe(df_15m, df_1h, "SHORT")

                if confirmed:
                    tp_price = last['close'] * (1 - config.get('strategy.take_profit_percent', 2.0) / 100)
                    logger.info(f"🎯 [{pair}] SHORT СИГНАЛ! Ціна={last['close']:.2f}, TP={tp_price:.2f}")
                    self._create_auto_forecast(pair, "SHORT", last['close'], tp_price, 85)
                    self._execute_trade_sync(pair, "SHORT", last['close'])
                else:
                    logger.info(f"⚠️ [{pair}] SHORT сигнал, але НЕМАЄ підтвердження на 15m/1h")

    def _execute_trade_sync(self, pair: str, signal_type: str, current_price: float):
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
                logger.info(f"✅ [{pair}] LONG угода відкрита: {quantity} @ {result['execution_price']:.2f}")

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
                logger.info(f"✅ [{pair}] SHORT угода відкрита: {quantity} @ {result['execution_price']:.2f}")

                if self.telegram:
                    asyncio.create_task(self.telegram.send_trade_notification({
                        'pair': pair, 'side': 'SHORT', 'quantity': quantity,
                        'entry_price': result['execution_price'], 'tp': tp_price, 'sl': sl_price,
                        'balance': balance
                    }))

    def _create_auto_forecast(self, pair: str, signal_type: str, entry_price: float, target_price: float,
                              confidence: float):
        import threading

        balance = self.db.get_balance("USDT", is_paper=True)
        forecast_percent = config.get('testing.forecast_position_percent', 50.0)
        position_usdt = balance * (forecast_percent / 100)

        min_trade_usdt = 10
        if position_usdt < min_trade_usdt:
            logger.warning(f"⚠️ [{pair}] Недостатньо балансу для прогнозу: {position_usdt:.2f} USDT")
            position_usdt = min_trade_usdt if balance > min_trade_usdt else balance

        position_quantity = position_usdt / entry_price
        position_quantity = round(position_quantity, 6)
        position_usdt = position_quantity * entry_price

        logger.info(f"🔮 [{pair}] Прогноз: {signal_type} | {entry_price:.2f} → {target_price:.2f} | "
                    f"Розмір: {position_quantity} (${position_usdt:.2f})")

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
                        await create_fc(
                            pair, signal_type, entry_price, target_price, confidence,
                            position_quantity=position_quantity, position_usdt=position_usdt
                        )
                    except Exception as e:
                        logger.error(f"❌ [{pair}] Помилка створення прогнозу: {e}")

                if loop.is_running():
                    asyncio.create_task(create_forecast_internal())
                else:
                    loop.run_until_complete(create_forecast_internal())
            except Exception as e:
                logger.error(f"❌ [{pair}] Помилка в потоці: {e}")

        thread = threading.Thread(target=send_forecast, daemon=True)
        thread.start()

    def _execute_trade_sync_with_size(self, pair: str, signal_type: str, current_price: float, quantity: float):
        tp_percent = config.get('strategy.take_profit_percent', 2.0)
        sl_percent = config.get('strategy.stop_loss_percent', 1.5)

        if signal_type == "LONG":
            tp_price = current_price * (1 + tp_percent / 100)
            sl_price = current_price * (1 - sl_percent / 100)
            result = self.paper_engine.execute_buy(pair, quantity, current_price)

            if result:
                self.db.update_trade(result['trade_id'], {
                    'take_profit': tp_price,
                    'stop_loss': sl_price
                })
                logger.info(f"✅ [{pair}] LONG угода відкрита: {quantity} @ {result['execution_price']:.2f}")

        elif signal_type == "SHORT":
            tp_price = current_price * (1 - tp_percent / 100)
            sl_price = current_price * (1 + sl_percent / 100)
            result = self.paper_engine.execute_short(pair, quantity, current_price)

            if result:
                self.db.update_trade(result['trade_id'], {
                    'take_profit': tp_price,
                    'stop_loss': sl_price
                })
                logger.info(f"✅ [{pair}] SHORT угода відкрита: {quantity} @ {result['execution_price']:.2f}")

    async def close_trade_manually(self, trade_id: int, current_price: float = None):
        trade = self.db.get_trade_by_id(trade_id)
        if not trade:
            return {"error": "Угоду не знайдено"}

        if current_price is None:
            current_price = self.exchange.get_current_price(trade.pair)

        result = self.paper_engine.execute_sell(trade.id, current_price)

        if result and self.telegram:
            await self.telegram.send_message(f"🔴 ПРИМУСОВЕ ЗАКРИТТЯ\n{trade.pair} | PnL: {result['pnl']:.2f} USDT")

        return result

    def _check_trailing_stop(self, pair: str):
        """Перевірка трейлінг стопу для захисту прибутку"""
        open_trades = self.db.get_open_trades(pair=pair, is_paper=True)

        if not open_trades:
            return

        current_price = self.exchange.get_current_price(pair)
        if not current_price:
            return

        trailing_enabled = config.get('strategy.trailing_stop', True)
        if not trailing_enabled:
            return

        activation_percent = config.get('strategy.trailing_activation_percent', 1.0)
        trailing_distance = config.get('strategy.trailing_distance_percent', 0.5)

        for trade in open_trades:
            if trade.side == OrderSide.BUY:  # LONG
                current_profit = ((current_price - trade.entry_price) / trade.entry_price) * 100
            else:  # SHORT
                current_profit = ((trade.entry_price - current_price) / trade.entry_price) * 100

            if current_profit >= activation_percent:
                if trade.side == OrderSide.BUY:
                    new_sl = current_price * (1 - trailing_distance / 100)
                    if new_sl > (trade.stop_loss or 0):
                        old_sl = trade.stop_loss
                        self.db.update_trade(trade.id, {'stop_loss': new_sl})
                        logger.info(
                            f"📈 [{pair}] Трейлінг стоп LONG: {old_sl:.2f} → {new_sl:.2f} | Профіт: {current_profit:.1f}%")

                        # Сповіщення в Telegram
                        if self.telegram:
                            asyncio.create_task(self.telegram.send_message(
                                f"📈 *Trailing Stop Updated*\n"
                                f"Pair: {pair}\n"
                                f"Side: LONG\n"
                                f"Profit: +{current_profit:.1f}%\n"
                                f"Old SL: ${old_sl:.2f}\n"
                                f"New SL: ${new_sl:.2f}",
                                parse_mode="Markdown"
                            ))
                else:  # SHORT
                    new_sl = current_price * (1 + trailing_distance / 100)
                    if new_sl < (trade.stop_loss or float('inf')):
                        old_sl = trade.stop_loss
                        self.db.update_trade(trade.id, {'stop_loss': new_sl})
                        logger.info(
                            f"📉 [{pair}] Трейлінг стоп SHORT: {old_sl:.2f} → {new_sl:.2f} | Профіт: {current_profit:.1f}%")

                        if self.telegram:
                            asyncio.create_task(self.telegram.send_message(
                                f"📉 *Trailing Stop Updated*\n"
                                f"Pair: {pair}\n"
                                f"Side: SHORT\n"
                                f"Profit: +{current_profit:.1f}%\n"
                                f"Old SL: ${old_sl:.2f}\n"
                                f"New SL: ${new_sl:.2f}",
                                parse_mode="Markdown"
                            ))

    def _check_exits_sync(self, pair: str):
        open_trades = self.db.get_open_trades(pair=pair, is_paper=True)
        self._check_trailing_stop(pair)  # ← ДОДАТИ ЦЕЙ РЯДОК

        if not open_trades:
            return

        current_price = self.exchange.get_current_price(pair)
        if not current_price:
            return

        for trade in open_trades:
            should_exit = False
            exit_reason = ""

            if trade.side == OrderSide.BUY:
                if current_price >= trade.take_profit:
                    should_exit = True
                    exit_reason = "TAKE_PROFIT"
                elif current_price <= trade.stop_loss:
                    should_exit = True
                    exit_reason = "STOP_LOSS"
            else:
                if current_price <= trade.take_profit:
                    should_exit = True
                    exit_reason = "TAKE_PROFIT"
                elif current_price >= trade.stop_loss:
                    should_exit = True
                    exit_reason = "STOP_LOSS"

            if should_exit:
                result = self.paper_engine.execute_sell(trade.id, current_price)

                if result:
                    logger.info(f"💰 [{pair}] Угода закрита: {exit_reason} | PnL={result['pnl']:.2f} USDT")

                    if self.telegram:
                        new_balance = self.db.get_balance("USDT", is_paper=True)
                        asyncio.create_task(self.telegram.send_close_notification({
                            'pair': trade.pair, 'side': trade.side.value,
                            'entry_price': trade.entry_price, 'exit_price': result['execution_price'],
                            'pnl': result['pnl'], 'pnl_percent': result['pnl_percent'],
                            'reason': exit_reason, 'balance': new_balance
                        }))

    def can_trade_now(self) -> bool:
        hours_config = config.get('trading.trading_hours', {})

        if not hours_config.get('enabled', False):
            return True

        import pytz
        now = datetime.now(pytz.timezone(hours_config.get('timezone', 'Europe/Kiev')))
        start = datetime.strptime(hours_config.get('start', '09:00'), '%H:%M').time()
        end = datetime.strptime(hours_config.get('end', '21:00'), '%H:%M').time()

        return start <= now.time() <= end

    async def daily_reset_check(self):
        self.risk_manager.update_daily_check()

        if self.telegram and datetime.now().hour == 0 and datetime.now().minute < 5:
            stats = self.db.get_stats(is_paper=True)
            stats['balance'] = self.db.get_balance("USDT", is_paper=True)
            stats['stopped'] = not self.running
            await self.telegram.send_daily_report(stats)

            retention_days = config.get('database.log_retention_days', 7)
            self.db.cleanup_old_logs(retention_days)

    async def run(self):
        self.start_time = datetime.now()
        logger.info("🔄 OrderManager основний цикл запущено")

        last_price_update = datetime.now()
        last_analysis_minute = -1

        while self.running:
            try:
                await self.daily_reset_check()

                if (datetime.now() - last_price_update).seconds >= 10:
                    from web.app import update_forecast_prices
                    await update_forecast_prices()
                    last_price_update = datetime.now()

                current_minute = datetime.now().minute
                if current_minute != last_analysis_minute and current_minute % 5 == 0:
                    last_analysis_minute = current_minute
                    for pair in self.pairs:
                        try:
                            analysis = self.analyze_market(pair)
                            if analysis.get('forecast') and analysis.get('confidence', 0) > 70:
                                logger.info(f"📈 АКТИВНИЙ ПРОГНОЗ: {pair} {analysis['forecast']} з впевненістю {analysis['confidence']}%")
                        except Exception as e:
                            logger.debug(f"Помилка аналізу {pair}: {e}")

                await asyncio.sleep(self.signal_check_interval)

            except Exception as e:
                logger.error(f"❌ Помилка циклу OrderManager: {e}")
                await asyncio.sleep(5)