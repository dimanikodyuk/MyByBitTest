import asyncio
import threading
import pandas as pd
import numpy as np
from typing import Dict, Optional, List, Any
from datetime import datetime, date
from db.operations import DatabaseOperations
from db.database import SessionLocal
from db.models import SignalType, OrderSide, Trade, ForecastDB
from core.strategy import TradingStrategy
from core.paper_engine import PaperEngine
from core.risk_manager import RiskManager
from exchange.bybit_client import BybitClient
from utils.config_loader import config
from utils.logger import logger
from sqlalchemy import func
from core.news_strategy import NewsTradingEngine
from core.listing_strategy import ListingTradingEngine


class OrderManager:
    """Головний менеджер ордерів — приймає сигнали та виконує угоди"""

    def __init__(self, db_ops: DatabaseOperations, telegram_bot=None):
        self.db = db_ops
        self.telegram = telegram_bot
        self.exchange = BybitClient()
        self.last_forecast_time = {}
        self.timeframes = ['1m', '5m', '15m', '1h', '4h', '1d']
        self.start_time = datetime.now()

        self.last_trade_close_time = {}
        self.cooldown_candles = config.get('trading.cooldown_candles', 5)  # збільшено з 3 до 5

        self.paper_engine = PaperEngine(db_ops)
        self.risk_manager = RiskManager(db_ops, is_paper=True)

        self.strategies: Dict[str, TradingStrategy] = {}
        self.cache: Dict[str, Dict[str, pd.DataFrame]] = {}
        self.cache_locks: Dict[str, threading.Lock] = {}

        self.running = True
        self.pairs = config.get('trading.pairs', ['BTCUSDT'])

        self.base_timeframe = config.get('trading.base_timeframe', '15m')
        self.confirm_timeframes = config.get('trading.confirmation_timeframes', ['15m', '1h'])
        self.signal_check_interval = config.get('trading.signal_check_interval', 30)

        self._init_strategies()
        self._load_initial_data()

        # Ініціалізація додаткових стратегій
        self.news_strategy = None
        self.listing_strategy = None

        if config.get('news_strategy.enabled', True):
            self.news_strategy = NewsTradingEngine(self)

        if config.get('listing_strategy.enabled', True):
            self.listing_strategy = ListingTradingEngine(self)

        logger.info(f"✅ OrderManager ініціалізовано для {len(self.pairs)} пар, базовий TF: {self.base_timeframe}")

    def _init_strategies(self):
        for pair in self.pairs:
            self.strategies[pair] = TradingStrategy(pair)
            self.cache[pair] = {}
            self.cache_locks[pair] = threading.Lock()
            logger.info(f"📊 Стратегія ініціалізована для {pair}")

    def _load_initial_data(self):
        """Завантаження початкових даних для всіх пар і таймфреймів"""
        needed_tfs = list(set([self.base_timeframe] + self.confirm_timeframes + ['1h', '4h']))
        for pair in self.pairs:
            for tf in needed_tfs:
                try:
                    df = self.exchange.get_klines(pair, tf, limit=300)
                    if df is not None and len(df) > 0:
                        df = self.strategies[pair].calculate_indicators(df)
                        with self.cache_locks[pair]:
                            self.cache[pair][tf] = df
                        logger.debug(f"📥 Завантажено {len(df)} свічок для {pair} {tf}")
                except Exception as e:
                    logger.error(f"Помилка завантаження {pair} {tf}: {e}")

    def on_new_candle(self, pair: str, candle: Dict):
        """Обробка нової закритої свічки"""
        if not self.running:
            return

        try:
            timeframe = self.base_timeframe
            logger.debug(f"🕯️ Нова свічка {pair} @ {candle.get('timestamp', 'unknown')}")

            with self.cache_locks.get(pair, threading.Lock()):
                if pair not in self.cache:
                    self.cache[pair] = {}

                new_df = pd.DataFrame([candle])

                if timeframe in self.cache[pair] and self.cache[pair][timeframe] is not None:
                    self.cache[pair][timeframe] = pd.concat(
                        [self.cache[pair][timeframe], new_df], ignore_index=True
                    ).tail(300)
                else:
                    self.cache[pair][timeframe] = new_df

                df = self.strategies[pair].calculate_indicators(self.cache[pair][timeframe])
                self.cache[pair][timeframe] = df

            self._check_signals_sync(pair)
            self._check_exits_sync(pair)

        except Exception as e:
            logger.error(f"Помилка обробки свічки {pair}: {e}")
            import traceback
            logger.error(traceback.format_exc())

    def _get_forecasts_count_today(self) -> int:
        """Кількість прогнозів створених сьогодні"""
        try:
            db = SessionLocal()
            today = date.today()
            count = db.query(ForecastDB).filter(
                func.date(ForecastDB.created_at) == today
            ).count()
            db.close()
            return count
        except Exception as e:
            logger.error(f"Помилка підрахунку прогнозів: {e}")
            return 0

    def _validate_tp_sl(self, signal_type: str, current_price: float, tp_price: float, sl_price: float) -> tuple:
        """Валідація та корекція TP/SL"""
        min_distance_pct = 0.3  # мінімальна відстань 0.3%
        min_distance = current_price * min_distance_pct / 100

        if signal_type == "LONG":
            if tp_price <= current_price:
                tp_price = current_price * 1.01
                logger.warning(f"TP скориговано до {tp_price:.4f}")
            if sl_price >= current_price:
                sl_price = current_price * 0.99
                logger.warning(f"SL скориговано до {sl_price:.4f}")

            if tp_price - current_price < min_distance:
                tp_price = current_price + min_distance
            if current_price - sl_price < min_distance:
                sl_price = current_price - min_distance

        else:  # SHORT
            if tp_price >= current_price:
                tp_price = current_price * 0.99
                logger.warning(f"TP скориговано до {tp_price:.4f}")
            if sl_price <= current_price:
                sl_price = current_price * 1.01
                logger.warning(f"SL скориговано до {sl_price:.4f}")

            if current_price - tp_price < min_distance:
                tp_price = current_price - min_distance
            if sl_price - current_price < min_distance:
                sl_price = current_price + min_distance

        return tp_price, sl_price

    def analyze_market(self, pair: str) -> Dict[str, Any]:
        """Аналіз ринку для пари з покращеним confidence"""
        with self.cache_locks.get(pair, threading.Lock()):
            df = self.cache[pair].get(self.base_timeframe)

        if df is None or len(df) < 50:
            return {"error": "Недостатньо даних"}

        try:
            last = df.iloc[-1]
            prev = df.iloc[-2]
        except IndexError:
            return {"error": "Недостатньо даних"}

        ema_fast = last.get(f'EMA_{config.get("strategy.ema_fast", 21)}', 0)
        ema_slow = last.get(f'EMA_{config.get("strategy.ema_slow", 200)}', 0)
        rsi = last.get('RSI', 50)
        macd = last.get('MACD', 0)
        macd_signal_val = last.get('MACD_Signal', 0)
        volume_ratio = last.get('Volume_Ratio', 1.0)
        adx_val = last.get('ADX', 0)

        trend = "📈 ВИСХІДНИЙ" if ema_fast > ema_slow else "📉 НИЗХІДНИЙ"
        trend_strength = abs((ema_fast - ema_slow) / ema_slow * 100) if ema_slow != 0 else 0

        rsi_signal = "neutral"
        if rsi > 70:
            rsi_signal = "overbought"
        elif rsi < 30:
            rsi_signal = "oversold"

        macd_cross = "neutral"
        try:
            prev_macd = prev.get('MACD', 0)
            prev_sig = prev.get('MACD_Signal', 0)
            if macd > macd_signal_val and prev_macd <= prev_sig:
                macd_cross = "bullish"
            elif macd < macd_signal_val and prev_macd >= prev_sig:
                macd_cross = "bearish"
        except:
            pass

        forecast = None
        confidence = 0
        target_price = None

        # Перевірка ліміту прогнозів на день
        max_forecasts = config.get('strategy.max_forecasts_per_day', 10)
        forecasts_today = self._get_forecasts_count_today()
        if forecasts_today >= max_forecasts:
            logger.debug(f"Ліміт прогнозів на день ({max_forecasts}) досягнуто")
            return {"error": "Ліміт прогнозів на день"}

        # Використовуємо ATR для розрахунку target
        atr = float(last.get('ATR', last['close'] * 0.01))
        tp_multiplier = config.get('strategy.atr_tp_multiplier', 2.5)

        if ema_fast > ema_slow and 40 <= rsi <= 65 and macd_cross == "bullish":
            forecast = "LONG"

            # Розрахунок confidence
            confidence = 70
            # RSI score (чим ближче до 40, тим краще)
            rsi_score = min(20, max(0, (rsi - 40) / 25 * 20))
            confidence += rsi_score
            # Volume score
            if volume_ratio > 1.2:
                volume_score = min(15, (volume_ratio - 1.2) / 2 * 15)
                confidence += volume_score
            # ADX score
            if adx_val > 20:
                adx_score = min(10, (adx_val - 20) / 30 * 10)
                confidence += adx_score

            confidence = min(95, max(60, confidence))
            target_price = last['close'] + atr * tp_multiplier
            logger.info(f"📊 [{pair}] Прогноз {forecast} (впевн.{confidence:.0f}%)")
            self._create_auto_forecast(pair, "LONG", last['close'], target_price, confidence)

        elif ema_fast < ema_slow and 35 <= rsi <= 60 and macd_cross == "bearish":
            forecast = "SHORT"

            confidence = 70
            rsi_score = min(20, max(0, (60 - rsi) / 25 * 20))
            confidence += rsi_score
            if volume_ratio > 1.2:
                volume_score = min(15, (volume_ratio - 1.2) / 2 * 15)
                confidence += volume_score
            if adx_val > 20:
                adx_score = min(10, (adx_val - 20) / 30 * 10)
                confidence += adx_score

            confidence = min(95, max(60, confidence))
            target_price = last['close'] - atr * tp_multiplier
            logger.info(f"📊 [{pair}] Прогноз {forecast} (впевн.{confidence:.0f}%)")
            self._create_auto_forecast(pair, "SHORT", last['close'], target_price, confidence)

        return {
            "pair": pair,
            "current_price": float(last['close']),
            "trend": trend,
            "trend_strength": round(trend_strength, 2),
            "ema_fast": round(ema_fast, 2),
            "ema_slow": round(ema_slow, 2),
            "rsi": round(rsi, 2),
            "rsi_signal": rsi_signal,
            "macd": round(macd, 6),
            "macd_signal": round(macd_signal_val, 6),
            "macd_cross": macd_cross,
            "forecast": forecast,
            "confidence": round(confidence, 2),
            "target_price": target_price,
            "atr": round(atr, 4),
            "timestamp": datetime.now().isoformat()
        }

    def stop(self):
        self.running = False

        # Зупинка додаткових стратегій
        if hasattr(self, 'news_trader') and self.news_trader:
            try:
                self.news_trader.stop()
                logger.info("📰 Новиний трейдер зупинено")
            except Exception as e:
                logger.error(f"Помилка зупинки новинного трейдера: {e}")

        if hasattr(self, 'listing_monitor') and self.listing_monitor:
            try:
                self.listing_monitor.stop()
                logger.info("🆕 Монітор нових лістингів зупинено")
            except Exception as e:
                logger.error(f"Помилка зупинки монітора лістингів: {e}")

        logger.info("🛑 OrderManager зупинено")

    def _check_signals_sync(self, pair: str):
        """Перевірка сигналів для входу з покращеними фільтрами"""

        if not self.can_trade_now():
            logger.debug(f"⏰ [{pair}] Поза робочими годинами")
            return

        # Cooldown після збиткової угоди
        if self.is_in_cooldown(pair):
            logger.debug(f"⏰ [{pair}] Cooldown активний")
            return

        # Перевірка ризик-менеджера
        can_open, reason = self.risk_manager.can_open_trade(pair)
        if not can_open:
            logger.info(f"❌ [{pair}] Угода не створена: {reason}")
            return

        # Перевірка ліміту прогнозів на день
        max_forecasts = config.get('strategy.max_forecasts_per_day', 10)
        forecasts_today = self._get_forecasts_count_today()
        if forecasts_today >= max_forecasts:
            logger.debug(f"Ліміт прогнозів на день ({max_forecasts}) досягнуто")
            return

        # Беремо дані базового таймфрейму
        with self.cache_locks.get(pair, threading.Lock()):
            df_base = self.cache[pair].get(self.base_timeframe)

        if df_base is None or len(df_base) < self.strategies[pair].ema_slow:
            logger.debug(f"⚠️ [{pair}] Недостатньо даних: {len(df_base) if df_base is not None else 0}")
            return

        last = df_base.iloc[-1]
        prev = df_base.iloc[-2]

        ema_fast_val = last.get(f'EMA_{config.get("strategy.ema_fast", 21)}', np.nan)
        ema_slow_val = last.get(f'EMA_{config.get("strategy.ema_slow", 200)}', np.nan)
        rsi_val = last.get('RSI', np.nan)
        macd_val = last.get('MACD', np.nan)
        macd_signal_val = last.get('MACD_Signal', np.nan)
        prev_macd = prev.get('MACD', np.nan)
        prev_signal = prev.get('MACD_Signal', np.nan)
        adx_val = last.get('ADX', 0)
        volume_ratio = last.get('Volume_Ratio', 1.0)

        # Якщо індикатори ще не розраховані — пропускаємо
        if any(pd.isna(x) for x in [ema_fast_val, ema_slow_val, rsi_val, macd_val]):
            logger.debug(f"⚠️ [{pair}] Індикатори ще не готові (NaN)")
            return

        # ========== НОВІ ФІЛЬТРИ ==========

        # 1. ADX фільтр (сила тренду)
        min_adx = config.get('strategy.min_adx', 20)
        if adx_val < min_adx:
            logger.debug(f"[{pair}] ADX={adx_val:.1f} < {min_adx}, сигнал пропущено")
            return

        # 2. Volume фільтр (збільшено поріг)
        if pd.isna(volume_ratio):
            volume_ratio = 1.0
        min_volume = config.get('strategy.min_volume_ratio', 1.2)
        volume_ok = volume_ratio >= min_volume
        if not volume_ok:
            logger.debug(f"[{pair}] Volume ratio {volume_ratio:.2f} < {min_volume}, сигнал пропущено")
            return

        # 3. MACD тільки крос
        macd_cross_only = config.get('strategy.macd_cross_only', True)
        if macd_cross_only:
            macd_bullish = macd_val > macd_signal_val and prev_macd <= prev_signal
            macd_bearish = macd_val < macd_signal_val and prev_macd >= prev_signal
        else:
            macd_bullish = macd_val > macd_signal_val
            macd_bearish = macd_val < macd_signal_val

        # LONG сигнал
        rsi_min_long = config.get('strategy.rsi_min_long', 40)
        rsi_max_long = config.get('strategy.rsi_max_long', 65)
        ema_ok_long = ema_fast_val > ema_slow_val
        rsi_ok_long = rsi_min_long <= rsi_val <= rsi_max_long

        is_long = ema_ok_long and rsi_ok_long and macd_bullish

        if is_long:
            logger.info(
                f"🔍 [{pair}] Умови LONG: EMA✅ RSI={rsi_val:.1f}✅ MACD✅ VOL={volume_ratio:.2f}✅ ADX={adx_val:.1f}✅")

            with self.cache_locks.get(pair, threading.Lock()):
                df_15m = self.cache[pair].get('15m')
                df_1h = self.cache[pair].get('1h')

            confirmed = self.strategies[pair].confirm_with_timeframe(df_15m, df_1h, "LONG")

            if confirmed:
                tp_price, sl_price = self.strategies[pair].calculate_atr_tp_sl(df_base, "LONG")
                # Валідація TP/SL
                tp_price, sl_price = self._validate_tp_sl("LONG", last['close'], tp_price, sl_price)
                logger.info(f"🎯 [{pair}] LONG СИГНАЛ! Ціна={last['close']:.4f} TP={tp_price:.4f} SL={sl_price:.4f}")
                self._create_auto_forecast(pair, "LONG", last['close'], tp_price, 85)
                self._execute_trade_sync(pair, "LONG", last['close'], tp_price, sl_price)
            else:
                logger.info(f"⚠️ [{pair}] LONG сигнал без підтвердження 15m/1h")
            return

        # SHORT сигнал
        rsi_min_short = config.get('strategy.rsi_min_short', 35)
        rsi_max_short = config.get('strategy.rsi_max_short', 60)
        ema_ok_short = ema_fast_val < ema_slow_val
        rsi_ok_short = rsi_min_short <= rsi_val <= rsi_max_short

        is_short = ema_ok_short and rsi_ok_short and macd_bearish

        if is_short:
            logger.info(
                f"🔍 [{pair}] Умови SHORT: EMA✅ RSI={rsi_val:.1f}✅ MACD✅ VOL={volume_ratio:.2f}✅ ADX={adx_val:.1f}✅")

            with self.cache_locks.get(pair, threading.Lock()):
                df_15m = self.cache[pair].get('15m')
                df_1h = self.cache[pair].get('1h')

            confirmed = self.strategies[pair].confirm_with_timeframe(df_15m, df_1h, "SHORT")

            if confirmed:
                tp_price, sl_price = self.strategies[pair].calculate_atr_tp_sl(df_base, "SHORT")
                tp_price, sl_price = self._validate_tp_sl("SHORT", last['close'], tp_price, sl_price)
                logger.info(f"🎯 [{pair}] SHORT СИГНАЛ! Ціна={last['close']:.4f} TP={tp_price:.4f} SL={sl_price:.4f}")
                self._create_auto_forecast(pair, "SHORT", last['close'], tp_price, 85)
                self._execute_trade_sync(pair, "SHORT", last['close'], tp_price, sl_price)
            else:
                logger.info(f"⚠️ [{pair}] SHORT сигнал без підтвердження 15m/1h")

    def _execute_trade_sync(self, pair: str, signal_type: str, current_price: float,
                            tp_price: float = None, sl_price: float = None):
        """Виконання угоди з валідацією TP/SL"""

        balance = self.db.get_balance("USDT", is_paper=True)

        # Якщо TP/SL не передані — рахуємо з ATR
        if tp_price is None or sl_price is None:
            with self.cache_locks.get(pair, threading.Lock()):
                df_base = self.cache[pair].get(self.base_timeframe)
            if df_base is not None:
                tp_price, sl_price = self.strategies[pair].calculate_atr_tp_sl(df_base, signal_type)
            else:
                tp_pct = config.get('strategy.take_profit_percent', 2.0)
                sl_pct = config.get('strategy.stop_loss_percent', 1.5)
                if signal_type == "LONG":
                    tp_price = current_price * (1 + tp_pct / 100)
                    sl_price = current_price * (1 - sl_pct / 100)
                else:
                    tp_price = current_price * (1 - tp_pct / 100)
                    sl_price = current_price * (1 + sl_pct / 100)

        # Валідація TP/SL
        tp_price, sl_price = self._validate_tp_sl(signal_type, current_price, tp_price, sl_price)

        # Перевірка RR ratio
        if signal_type == "LONG":
            reward = tp_price - current_price
            risk = current_price - sl_price
        else:
            reward = current_price - tp_price
            risk = sl_price - current_price

        if risk <= 0:
            logger.warning(f"⚠️ [{pair}] Некоректний SL (risk={risk:.4f})")
            return

        rr_ratio = reward / risk
        min_rr = config.get('strategy.min_reward_risk_ratio', 2.0)  # збільшено з 1.5 до 2.0

        if rr_ratio < min_rr:
            logger.warning(f"⚠️ [{pair}] RR={rr_ratio:.2f} < мін {min_rr}, пропускаємо")
            return

        quantity = self.risk_manager.calculate_position_size(balance, current_price, sl_price, pair)

        if quantity <= 0:
            logger.warning(f"❌ [{pair}] Невірний об'єм: {quantity}")
            return

        if signal_type == "LONG":
            result = self.paper_engine.execute_buy(pair, quantity, current_price)
        else:
            result = self.paper_engine.execute_short(pair, quantity, current_price)

        if result:
            self.db.update_trade(result['trade_id'], {
                'take_profit': tp_price,
                'stop_loss': sl_price
            })

            logger.info(f"✅ [{pair}] {signal_type} відкрито: {quantity:.6f} @ {result['execution_price']:.4f} | "
                        f"TP={tp_price:.4f} SL={sl_price:.4f} | RR={rr_ratio:.2f}")

            if self.telegram:
                self._safe_telegram_send({
                    'pair': pair, 'side': signal_type, 'quantity': quantity,
                    'entry_price': result['execution_price'], 'tp': tp_price, 'sl': sl_price,
                    'balance': self.db.get_balance("USDT", is_paper=True)
                }, 'trade')

    def _safe_telegram_send(self, data, type: str):
        """Безпечна відправка Telegram"""
        if not self.telegram:
            return
        try:
            loop = asyncio.get_running_loop()
            if type == 'trade':
                loop.create_task(self.telegram.send_trade_notification(data))
            elif type == 'close':
                loop.create_task(self.telegram.send_close_notification(data))
            else:
                loop.create_task(self.telegram.send_message(data))
        except RuntimeError:
            try:
                if type == 'trade':
                    asyncio.run(self.telegram.send_trade_notification(data))
                elif type == 'close':
                    asyncio.run(self.telegram.send_close_notification(data))
                else:
                    asyncio.run(self.telegram.send_message(data))
            except Exception as e:
                logger.error(f"Telegram помилка: {e}")
        except Exception as e:
            logger.error(f"Telegram помилка: {e}")

    def is_in_cooldown(self, pair: str) -> bool:
        """Cooldown після збиткової угоди"""
        if pair not in self.last_trade_close_time:
            return False
        last_close = self.last_trade_close_time[pair]
        tf_seconds = {'1m': 60, '5m': 300, '15m': 900, '1h': 3600, '4h': 14400, '1d': 86400}
        candle_sec = tf_seconds.get(self.base_timeframe, 900)
        cooldown_sec = self.cooldown_candles * candle_sec
        return (datetime.now() - last_close).total_seconds() < cooldown_sec

    def record_trade_close(self, pair: str, pnl: float):
        """Запис часу закриття для cooldown"""
        if pnl <= 0:
            self.last_trade_close_time[pair] = datetime.now()
            logger.info(f"📝 [{pair}] Cooldown після збитку (PnL={pnl:.2f})")
        else:
            self.last_trade_close_time.pop(pair, None)

    def _create_auto_forecast(self, pair: str, signal_type: str, entry_price: float,
                              target_price: float, confidence: float):
        """Створення прогнозу з індикаторами"""
        import threading as _threading

        balance = self.db.get_balance("USDT", is_paper=True)
        forecast_percent = config.get('testing.forecast_position_percent', 8)
        position_usdt = max(balance * (forecast_percent / 100), 10.0)
        if position_usdt > balance:
            position_usdt = balance * 0.9

        position_quantity = round(position_usdt / entry_price, 6) if entry_price > 0 else 0
        position_usdt = position_quantity * entry_price

        # Збираємо індикатори для опису
        with self.cache_locks.get(pair, threading.Lock()):
            df = self.cache[pair].get(self.base_timeframe)

        indicators = {}
        if df is not None and len(df) > 0:
            last = df.iloc[-1]
            indicators = {
                'ema_fast_period': config.get('strategy.ema_fast', 21),
                'ema_slow_period': config.get('strategy.ema_slow', 200),
                'ema_fast': last.get(f'EMA_{config.get("strategy.ema_fast", 21)}', 0),
                'ema_slow': last.get(f'EMA_{config.get("strategy.ema_slow", 200)}', 0),
                'rsi': last.get('RSI', 50),
                'macd': last.get('MACD', 0),
                'macd_signal': last.get('MACD_Signal', 0),
                'volume_ratio': last.get('Volume_Ratio', 1.0),
                'adx': last.get('ADX', 0),
                'atr_percent': last.get('ATR_Percent', 0),
                'entry_price': entry_price,
                'target_price': target_price,
            }

        logger.info(f"🔮 [{pair}] Прогноз: {signal_type} | {entry_price:.4f} → {target_price:.4f} | "
                    f"${position_usdt:.2f}")

        def send_forecast():
            try:
                import asyncio as _asyncio
                try:
                    loop = _asyncio.get_event_loop()
                    if loop.is_closed():
                        loop = _asyncio.new_event_loop()
                        _asyncio.set_event_loop(loop)
                except RuntimeError:
                    loop = _asyncio.new_event_loop()
                    _asyncio.set_event_loop(loop)

                async def _create():
                    try:
                        from web.app import create_forecast_internal as create_fc
                        await create_fc(
                            pair, signal_type, entry_price, target_price, confidence,
                            position_quantity=position_quantity, position_usdt=position_usdt,
                            indicators_snapshot=indicators,
                            description=None  # буде згенеровано автоматично
                        )
                    except Exception as e:
                        logger.error(f"❌ [{pair}] Помилка прогнозу: {e}")

                if loop.is_running():
                    _asyncio.create_task(_create())
                else:
                    loop.run_until_complete(_create())
            except Exception as e:
                logger.error(f"❌ [{pair}] Помилка в потоці прогнозу: {e}")

        t = _threading.Thread(target=send_forecast, daemon=True)
        t.start()

    def _execute_trade_sync_with_size(self, pair: str, signal_type: str,
                                      current_price: float, quantity: float):
        """Виконання угоди з явно вказаним розміром (для ручного виклику)"""
        with self.cache_locks.get(pair, threading.Lock()):
            df_base = self.cache[pair].get(self.base_timeframe)

        tp_price, sl_price = (None, None)
        if df_base is not None:
            tp_price, sl_price = self.strategies[pair].calculate_atr_tp_sl(df_base, signal_type)
            tp_price, sl_price = self._validate_tp_sl(signal_type, current_price, tp_price, sl_price)
        else:
            tp_pct = config.get('strategy.take_profit_percent', 2.0)
            sl_pct = config.get('strategy.stop_loss_percent', 1.5)
            if signal_type == "LONG":
                tp_price = current_price * (1 + tp_pct / 100)
                sl_price = current_price * (1 - sl_pct / 100)
            else:
                tp_price = current_price * (1 - tp_pct / 100)
                sl_price = current_price * (1 + sl_pct / 100)
            tp_price, sl_price = self._validate_tp_sl(signal_type, current_price, tp_price, sl_price)

        if signal_type == "LONG":
            result = self.paper_engine.execute_buy(pair, quantity, current_price)
        else:
            result = self.paper_engine.execute_short(pair, quantity, current_price)

        if result:
            self.db.update_trade(result['trade_id'], {
                'take_profit': tp_price,
                'stop_loss': sl_price
            })
            logger.info(f"✅ [{pair}] {signal_type} (ручний розмір) {quantity} @ {result['execution_price']:.4f}")

    async def close_trade_manually(self, trade_id: int, current_price: float = None):
        trade = self.db.get_trade_by_id(trade_id)
        if not trade:
            return {"error": "Угоду не знайдено"}

        if current_price is None:
            current_price = self.exchange.get_current_price(trade.pair)

        result = self.paper_engine.execute_sell(trade.id, current_price)

        if result and self.telegram:
            await self.telegram.send_message(
                f"🔴 ПРИМУСОВЕ ЗАКРИТТЯ\n{trade.pair} | PnL: {result['pnl']:.2f} USDT"
            )

        return result

    def _check_trailing_stop(self, pair: str):
        """Трейлінг стоп для захисту прибутку"""
        if not config.get('strategy.trailing_stop', True):
            return

        open_trades = self.db.get_open_trades(pair=pair, is_paper=True)
        if not open_trades:
            return

        current_price = self.exchange.get_current_price(pair)
        if not current_price:
            return

        activation_pct = config.get('strategy.trailing_activation_percent', 1.0)
        trailing_dist = config.get('strategy.trailing_distance_percent', 0.5)

        for trade in open_trades:
            if trade.side == OrderSide.BUY:
                current_profit = ((current_price - trade.entry_price) / trade.entry_price) * 100
            else:
                current_profit = ((trade.entry_price - current_price) / trade.entry_price) * 100

            if current_profit >= activation_pct:
                if trade.side == OrderSide.BUY:
                    new_sl = current_price * (1 - trailing_dist / 100)
                    if new_sl > (trade.stop_loss or 0):
                        self.db.update_trade(trade.id, {'stop_loss': new_sl})
                        logger.info(f"📈 [{pair}] Trailing LONG SL → {new_sl:.4f} (profit={current_profit:.1f}%)")
                        if self.telegram:
                            self._safe_telegram_send(
                                f"📈 *Trailing Stop*\n{pair} LONG\nProfit: +{current_profit:.1f}%\nNew SL: ${new_sl:.4f}",
                                'message'
                            )
                else:
                    new_sl = current_price * (1 + trailing_dist / 100)
                    if new_sl < (trade.stop_loss or float('inf')):
                        self.db.update_trade(trade.id, {'stop_loss': new_sl})
                        logger.info(f"📉 [{pair}] Trailing SHORT SL → {new_sl:.4f} (profit={current_profit:.1f}%)")
                        if self.telegram:
                            self._safe_telegram_send(
                                f"📉 *Trailing Stop*\n{pair} SHORT\nProfit: +{current_profit:.1f}%\nNew SL: ${new_sl:.4f}",
                                'message'
                            )

    def _check_exits_sync(self, pair: str):
        """Перевірка виходів за TP/SL"""
        self._check_trailing_stop(pair)

        open_trades = self.db.get_open_trades(pair=pair, is_paper=True)
        if not open_trades:
            return

        current_price = self.exchange.get_current_price(pair)
        if not current_price:
            return

        for trade in open_trades:
            if trade.take_profit is None or trade.stop_loss is None:
                logger.warning(f"[{pair}] Trade {trade.id} без TP/SL, пропускаємо")
                continue

            should_exit = False
            exit_reason = ""

            if trade.side == OrderSide.BUY:
                if current_price >= trade.take_profit:
                    should_exit, exit_reason = True, "TAKE_PROFIT"
                elif current_price <= trade.stop_loss:
                    should_exit, exit_reason = True, "STOP_LOSS"
            else:
                if current_price <= trade.take_profit:
                    should_exit, exit_reason = True, "TAKE_PROFIT"
                elif current_price >= trade.stop_loss:
                    should_exit, exit_reason = True, "STOP_LOSS"

            if should_exit:
                result = self.paper_engine.execute_sell(trade.id, current_price)
                if result:
                    logger.info(f"💰 [{pair}] Закрито: {exit_reason} | PnL={result['pnl']:.4f} USDT")
                    self.record_trade_close(pair, result['pnl'])

                    if self.telegram:
                        new_balance = self.db.get_balance("USDT", is_paper=True)
                        self._safe_telegram_send({
                            'pair': trade.pair, 'side': trade.side.value,
                            'entry_price': trade.entry_price, 'exit_price': result['execution_price'],
                            'pnl': result['pnl'], 'pnl_percent': result['pnl_percent'],
                            'reason': exit_reason, 'balance': new_balance
                        }, 'close')

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

    # У файлі core/order_manager.py, в методі run (приблизно після рядка з logger.info("🔄 OrderManager основний цикл запущено"))

    async def run(self):
        """Головний цикл"""
        self.start_time = datetime.now()
        logger.info("🔄 OrderManager основний цикл запущено")

        # ========== ДОДАТИ ЦЕ ==========
        # Запуск новинного трейдера
        if config.get('news_trading.enabled', True):
            try:
                from core.news_trader import NewsTrader
                self.news_trader = NewsTrader()
                asyncio.create_task(self.news_trader.run())
                logger.info("📰 Новиний трейдер запущено")
            except Exception as e:
                logger.error(f"Помилка запуску новинного трейдера: {e}")

        # Запуск монітора нових лістингів
        if config.get('listing_trading.enabled', True):
            try:
                from core.listing_monitor import ListingMonitor
                self.listing_monitor = ListingMonitor()
                asyncio.create_task(self.listing_monitor.run())
                logger.info("🆕 Монітор нових лістингів запущено")
            except Exception as e:
                logger.error(f"Помилка запуску монітора лістингів: {e}")
        # ===============================

        last_price_update = datetime.now()
        last_analysis_minute = -1

        while self.running:
            try:
                await self.daily_reset_check()

                # Оновлення цін прогнозів кожні 10 сек
                if (datetime.now() - last_price_update).seconds >= 10:
                    try:
                        from web.app import update_forecast_prices
                        await update_forecast_prices()
                    except Exception as e:
                        logger.debug(f"Помилка оновлення цін: {e}")
                    last_price_update = datetime.now()

                # Аналіз кожні 5 хвилин
                current_minute = datetime.now().minute
                if current_minute != last_analysis_minute and current_minute % 5 == 0:
                    last_analysis_minute = current_minute
                    for pair in self.pairs:
                        try:
                            df = self.exchange.get_klines(pair, self.base_timeframe, limit=300)
                            if df is not None and len(df) > 0:
                                df = self.strategies[pair].calculate_indicators(df)
                                with self.cache_locks[pair]:
                                    self.cache[pair][self.base_timeframe] = df

                            analysis = self.analyze_market(pair)
                            if analysis.get('forecast') and analysis.get('confidence', 0) > 65:
                                logger.info(
                                    f"📈 ПРОГНОЗ: {pair} {analysis['forecast']} впевн.{analysis['confidence']:.0f}%")
                        except Exception as e:
                            logger.debug(f"Помилка аналізу {pair}: {e}")

                await asyncio.sleep(self.signal_check_interval)

            except Exception as e:
                logger.error(f"❌ Помилка циклу: {e}")
                await asyncio.sleep(5)