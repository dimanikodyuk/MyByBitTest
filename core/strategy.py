import pandas as pd
import pandas_ta as ta
import numpy as np
from typing import Dict, Tuple, Optional
from datetime import datetime
from utils.logger import logger
from utils.config_loader import config


class TradingStrategy:
    """Розширена торгова стратегія з ATR-based TP/SL та покращеними сигналами"""

    def __init__(self, pair: str):
        self.pair = pair
        self.ema_fast = config.get('strategy.ema_fast', 21)
        self.ema_slow = config.get('strategy.ema_slow', 200)
        self.rsi_period = config.get('strategy.rsi_period', 14)
        self.rsi_min = config.get('strategy.rsi_min', 35)
        self.rsi_max = config.get('strategy.rsi_max', 70)
        self.macd_fast = config.get('strategy.macd_fast', 12)
        self.macd_slow = config.get('strategy.macd_slow', 26)
        self.macd_signal = config.get('strategy.macd_signal', 9)
        # Знижено поріг об'єму - 0.5 вже вказано в config, і це правильно
        self.min_volume_ratio = config.get('strategy.min_volume_ratio', 0.5)

        self.use_bollinger = config.get('strategy.use_bollinger', True)
        self.use_ichimoku = config.get('strategy.use_ichimoku', False)
        self.use_fibonacci = config.get('strategy.use_fibonacci', True)

        self.cache = {}

    def calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """Розрахунок всіх індикаторів включно з ATR та EMA50"""
        if df is None or len(df) < self.ema_slow:
            return df

        # EMA основні
        df[f'EMA_{self.ema_fast}'] = ta.ema(df['close'], length=self.ema_fast)
        df[f'EMA_{self.ema_slow}'] = ta.ema(df['close'], length=self.ema_slow)

        # EMA50 як проміжний тренд-фільтр
        df['EMA_50'] = ta.ema(df['close'], length=50)

        # RSI
        df['RSI'] = ta.rsi(df['close'], length=self.rsi_period)

        # MACD
        macd = ta.macd(df['close'], fast=self.macd_fast, slow=self.macd_slow, signal=self.macd_signal)
        if macd is not None:
            df['MACD'] = macd.get(f'MACD_{self.macd_fast}_{self.macd_slow}_{self.macd_signal}', 0)
            df['MACD_Signal'] = macd.get(f'MACDs_{self.macd_fast}_{self.macd_slow}_{self.macd_signal}', 0)
            df['MACD_Histogram'] = macd.get(f'MACDh_{self.macd_fast}_{self.macd_slow}_{self.macd_signal}', 0)

        # Volume
        df['Volume_MA'] = ta.sma(df['volume'], length=20)
        df['Volume_Ratio'] = df['volume'] / df['Volume_MA'].replace(0, np.nan)

        # ATR — ключовий для розрахунку TP/SL
        try:
            df['ATR'] = ta.atr(df['high'], df['low'], df['close'], length=14)
            df['ATR_Percent'] = (df['ATR'] / df['close']) * 100
        except Exception as e:
            logger.warning(f"ATR помилка: {e}")
            df['ATR'] = df['close'] * 0.01
            df['ATR_Percent'] = 1.0

        # Bollinger Bands
        if self.use_bollinger:
            try:
                bb = ta.bbands(df['close'], length=20, std=2)
                if bb is not None:
                    cols = bb.columns.tolist()
                    upper_col = next((c for c in cols if c.startswith('BBU')), None)
                    mid_col = next((c for c in cols if c.startswith('BBM')), None)
                    lower_col = next((c for c in cols if c.startswith('BBL')), None)
                    if upper_col:
                        df['BB_Upper'] = bb[upper_col]
                        df['BB_Middle'] = bb[mid_col]
                        df['BB_Lower'] = bb[lower_col]
                        df['BB_Width'] = (df['BB_Upper'] - df['BB_Lower']) / df['BB_Middle'].replace(0, np.nan)
                        df['BB_Position'] = (df['close'] - df['BB_Lower']) / (
                            (df['BB_Upper'] - df['BB_Lower']).replace(0, np.nan))
            except Exception as e:
                logger.warning(f"Bollinger Bands помилка: {e}")

        # Stochastic RSI
        try:
            stochrsi = ta.stochrsi(df['close'], length=14)
            if stochrsi is not None:
                k_col = next((c for c in stochrsi.columns if 'k' in c.lower()), None)
                d_col = next((c for c in stochrsi.columns if 'd' in c.lower()), None)
                df['StochRSI_K'] = stochrsi[k_col] if k_col else 50
                df['StochRSI_D'] = stochrsi[d_col] if d_col else 50
            else:
                df['StochRSI_K'] = 50
                df['StochRSI_D'] = 50
        except:
            df['StochRSI_K'] = 50
            df['StochRSI_D'] = 50

        # ADX
        try:
            adx = ta.adx(df['high'], df['low'], df['close'], length=14)
            if adx is not None:
                df['ADX'] = adx.get('ADX_14', 0)
                df['DMP'] = adx.get('DMP_14', 0)
                df['DMN'] = adx.get('DMN_14', 0)
            else:
                df['ADX'] = 0
                df['DMP'] = 0
                df['DMN'] = 0
        except:
            df['ADX'] = 0
            df['DMP'] = 0
            df['DMN'] = 0

        return df

    def calculate_atr_tp_sl(self, df: pd.DataFrame, signal_type: str) -> Tuple[float, float]:
        """
        Розрахунок TP/SL на основі ATR (волатильності).
        Повертає (tp_price, sl_price).
        ATR множники: SL = 1.5x ATR, TP = 2.5x ATR (RR = 1.67)
        """
        last = df.iloc[-1]
        price = float(last['close'])

        atr = float(last.get('ATR', price * 0.01))
        if atr <= 0 or pd.isna(atr):
            atr = price * 0.01

        # Мінімальний ATR 0.3%, максимальний 5%
        atr = max(price * 0.003, min(atr, price * 0.05))

        sl_multiplier = config.get('strategy.atr_sl_multiplier', 1.5)
        tp_multiplier = config.get('strategy.atr_tp_multiplier', 2.5)

        if signal_type == "LONG":
            sl_price = price - atr * sl_multiplier
            tp_price = price + atr * tp_multiplier
        else:  # SHORT
            sl_price = price + atr * sl_multiplier
            tp_price = price - atr * tp_multiplier

        logger.debug(f"ATR TP/SL для {signal_type}: ціна={price:.2f}, ATR={atr:.4f}, "
                     f"SL={sl_price:.2f}, TP={tp_price:.2f}")
        return tp_price, sl_price

    def confirm_with_timeframe(self, df_15m: pd.DataFrame, df_1h: pd.DataFrame, signal: str) -> bool:
        """
        Підтвердження сигналу на більших таймфреймах.
        Спрощено: лише EMA_50 напрямок + RSI зона.
        """
        if signal not in ["LONG", "SHORT"]:
            return False

        if df_15m is None or len(df_15m) < 10:
            logger.debug("Немає даних 15m — пропускаємо підтвердження")
            return True

        try:
            # На 15m перевіряємо EMA_50 (не EMA_200 — це занадто жорстко для 15m)
            if 'EMA_50' not in df_15m.columns:
                df_15m = self.calculate_indicators(df_15m)

            last_15m = df_15m.iloc[-1]
            ema50 = last_15m.get('EMA_50', None)
            price_15m = last_15m.get('close', 0)
            rsi_15m = last_15m.get('RSI', 50)

            if ema50 is not None and not pd.isna(ema50):
                if signal == "LONG":
                    # Ціна вище EMA_50 на 15m або RSI не перекуплений
                    ema_ok = price_15m > ema50 * 0.998  # допуск 0.2%
                    rsi_ok = 25 <= rsi_15m <= 75
                    if not (ema_ok and rsi_ok):
                        logger.debug(f"15m LONG не підтверджено: EMA_ok={ema_ok}, RSI={rsi_15m:.1f}")
                        return False
                else:  # SHORT
                    ema_ok = price_15m < ema50 * 1.002
                    rsi_ok = 25 <= rsi_15m <= 75
                    if not (ema_ok and rsi_ok):
                        logger.debug(f"15m SHORT не підтверджено: EMA_ok={ema_ok}, RSI={rsi_15m:.1f}")
                        return False
        except Exception as e:
            logger.error(f"Помилка підтвердження на 15m: {e}")
            return True  # при помилці — не блокуємо

        # 1h — лише м'яка перевірка тренду
        if df_1h is not None and len(df_1h) > 20:
            try:
                if 'EMA_50' not in df_1h.columns:
                    df_1h = self.calculate_indicators(df_1h)

                last_1h = df_1h.iloc[-1]
                price_1h = last_1h.get('close', 0)
                ema50_1h = last_1h.get('EMA_50', None)

                if ema50_1h is not None and not pd.isna(ema50_1h):
                    if signal == "LONG" and price_1h < ema50_1h * 0.99:
                        logger.debug(f"1h LONG: ціна суттєво нижче EMA50, ігноруємо")
                        return False
                    elif signal == "SHORT" and price_1h > ema50_1h * 1.01:
                        logger.debug(f"1h SHORT: ціна суттєво вище EMA50, ігноруємо")
                        return False
            except Exception as e:
                logger.debug(f"Помилка підтвердження на 1h: {e}")

        logger.info(f"✅ Сигнал {signal} підтверджено на 15m/1h")
        return True

    def detect_candle_patterns(self, df: pd.DataFrame) -> Dict:
        """Розпізнавання свічкових патернів на останній свічці"""
        if df is None or len(df) < 3:
            return {"patterns": [], "signal": "neutral"}

        last = df.iloc[-1]
        prev = df.iloc[-2]
        prev2 = df.iloc[-3]

        patterns = []
        signal = "neutral"

        body = abs(last['close'] - last['open'])
        upper_shadow = last['high'] - max(last['close'], last['open'])
        lower_shadow = min(last['close'], last['open']) - last['low']
        total_range = last['high'] - last['low'] if last['high'] != last['low'] else 1

        prev_body = abs(prev['close'] - prev['open'])

        # 1. Молот
        if (lower_shadow > body * 2 and upper_shadow < body * 0.5 and
                body > 0 and total_range > 0 and lower_shadow / total_range > 0.6):
            patterns.append({
                "name": "hammer", "type": "bullish", "strength": "strong",
                "description": "Молот - потенційний розворот вгору"
            })
            signal = "bullish"

        # 2. Висячий чоловічок
        if (lower_shadow > body * 2 and upper_shadow < body * 0.5 and
                last['close'] < last['open'] and last['high'] > prev['high']):
            patterns.append({
                "name": "hanging_man", "type": "bearish", "strength": "strong",
                "description": "Висячий чоловічок - потенційний розворот вниз"
            })
            signal = "bearish"

        # 3. Биче поглинання
        if (last['close'] > last['open'] and prev['close'] < prev['open'] and
                last['close'] > prev['open'] and last['open'] < prev['close']):
            patterns.append({
                "name": "bullish_engulfing", "type": "bullish", "strength": "very_strong",
                "description": "Биче поглинання - сильний розворот вгору"
            })
            signal = "bullish"

        # 4. Ведмеже поглинання
        if (last['close'] < last['open'] and prev['close'] > prev['open'] and
                last['close'] < prev['open'] and last['open'] > prev['close']):
            patterns.append({
                "name": "bearish_engulfing", "type": "bearish", "strength": "very_strong",
                "description": "Ведмеже поглинання - сильний розворот вниз"
            })
            signal = "bearish"

        # 5. Доджі
        if total_range > 0 and body / total_range < 0.1:
            patterns.append({
                "name": "doji", "type": "neutral", "strength": "weak",
                "description": "Доджі - невизначеність, можливий розворот"
            })

        # 6. Ранкова зірка
        if (prev2['close'] < prev2['open'] and
                abs(prev['close'] - prev['open']) < abs(prev2['close'] - prev2['open']) * 0.3 and
                last['close'] > last['open'] and
                last['close'] > (prev2['high'] + prev2['low']) / 2):
            patterns.append({
                "name": "morning_star", "type": "bullish", "strength": "very_strong",
                "description": "Ранкова зірка - сильний розворот вгору"
            })
            signal = "bullish"

        # 7. Вечірня зірка
        if (prev2['close'] > prev2['open'] and
                abs(prev['close'] - prev['open']) < abs(prev2['close'] - prev2['open']) * 0.3 and
                last['close'] < last['open'] and
                last['close'] < (prev2['high'] + prev2['low']) / 2):
            patterns.append({
                "name": "evening_star", "type": "bearish", "strength": "very_strong",
                "description": "Вечірня зірка - сильний розворот вниз"
            })
            signal = "bearish"

        return {
            "patterns": patterns,
            "signal": signal,
            "current_price": last['close'],
            "timestamp": datetime.now().isoformat()
        }

    def get_bollinger_signal(self, df: pd.DataFrame) -> str:
        """Сигнал на основі Bollinger Bands"""
        if 'BB_Position' not in df.columns:
            return "neutral"

        last = df.iloc[-1]
        bb_pos = last.get('BB_Position', 0.5)

        if pd.isna(bb_pos):
            return "neutral"

        if bb_pos < 0.1:
            return "oversold"
        elif bb_pos > 0.9:
            return "overbought"
        return "neutral"

    def get_kelly_criterion(self, win_rate: float, avg_win: float, avg_loss: float) -> float:
        """Kelly Criterion для оптимального розміру позиції"""
        if avg_loss == 0:
            return 0.1
        b = avg_win / avg_loss
        p = win_rate / 100
        q = 1 - p
        kelly = (p * b - q) / b
        return max(0, min(kelly, 0.25))

    def check_long_signal(self, df: pd.DataFrame) -> Tuple[bool, Dict]:
        """
        Перевірка сигналу LONG.
        Спрощені умови: EMA_fast > EMA_slow + RSI в зоні + MACD позитивний (не обов'язково крос)
        """
        if df is None or len(df) < 2:
            return False, {}

        last = df.iloc[-1]
        prev = df.iloc[-2]

        ema_fast_col = f'EMA_{self.ema_fast}'
        ema_slow_col = f'EMA_{self.ema_slow}'

        if ema_fast_col not in df.columns or ema_slow_col not in df.columns:
            return False, {}

        ema_fast_val = last.get(ema_fast_col, 0)
        ema_slow_val = last.get(ema_slow_col, 0)
        rsi_val = last.get('RSI', 50)
        macd_val = last.get('MACD', 0)
        macd_sig = last.get('MACD_Signal', 0)
        prev_macd = prev.get('MACD', 0)
        prev_sig = prev.get('MACD_Signal', 0)

        if any(pd.isna(x) for x in [ema_fast_val, ema_slow_val, rsi_val]):
            return False, {}

        ema_condition = ema_fast_val > ema_slow_val

        # RSI: 35-70 для LONG (не перекуплений, не дно)
        rsi_ok = config.get('strategy.rsi_min_long', 35) <= rsi_val <= config.get('strategy.rsi_max_long', 70)

        # MACD: або крос OR просто вище сигналу (менш жорстко)
        macd_cross = macd_val > macd_sig and prev_macd <= prev_sig
        macd_above = macd_val > macd_sig and macd_val > 0
        macd_condition = macd_cross or macd_above

        # Volume: знижений поріг
        volume_ratio = last.get('Volume_Ratio', 1.0)
        if pd.isna(volume_ratio):
            volume_ratio = 1.0
        volume_condition = volume_ratio >= self.min_volume_ratio

        # BB: не перекуплений
        bb_signal = self.get_bollinger_signal(df)
        bb_condition = bb_signal != "overbought"

        # ADX: тренд є (> 15, знижено з 20)
        adx_val = last.get('ADX', 25)
        adx_condition = adx_val > 15 if not pd.isna(adx_val) else True

        # StochRSI: не перекуплений
        stoch_k = last.get('StochRSI_K', 50)
        stoch_condition = stoch_k < 85 if not pd.isna(stoch_k) else True

        conditions = {
            'EMA_condition': ema_condition,
            'RSI_condition': rsi_ok,
            'MACD_condition': macd_condition,
            'Volume_condition': volume_condition,
            'BB_condition': bb_condition,
            'ADX_condition': adx_condition,
            'Stoch_condition': stoch_condition,
        }

        is_long = all(conditions.values())

        indicators = {
            'ema_fast': float(ema_fast_val),
            'ema_slow': float(ema_slow_val),
            'rsi': float(rsi_val),
            'macd': float(macd_val),
            'macd_signal': float(macd_sig),
            'volume_ratio': float(volume_ratio),
            'price': float(last['close']),
            'bb_position': float(last.get('BB_Position', 0.5)) if 'BB_Position' in df.columns else 0.5,
            'adx': float(adx_val) if not pd.isna(adx_val) else 0,
            'stoch_k': float(stoch_k) if not pd.isna(stoch_k) else 50,
            'atr_percent': float(last.get('ATR_Percent', 1.0)),
        }

        logger.debug(f"LONG умови {self.pair}: " + ", ".join(f"{k}={'✅' if v else '❌'}" for k, v in conditions.items()))

        return is_long, indicators

    def check_short_signal(self, df: pd.DataFrame) -> Tuple[bool, Dict]:
        """
        Перевірка сигналу SHORT.
        Спрощені умови з аналогічною логікою.
        """
        if df is None or len(df) < 2:
            return False, {}

        last = df.iloc[-1]
        prev = df.iloc[-2]

        ema_fast_col = f'EMA_{self.ema_fast}'
        ema_slow_col = f'EMA_{self.ema_slow}'

        if ema_fast_col not in df.columns or ema_slow_col not in df.columns:
            return False, {}

        ema_fast_val = last.get(ema_fast_col, 0)
        ema_slow_val = last.get(ema_slow_col, 0)
        rsi_val = last.get('RSI', 50)
        macd_val = last.get('MACD', 0)
        macd_sig = last.get('MACD_Signal', 0)
        prev_macd = prev.get('MACD', 0)
        prev_sig = prev.get('MACD_Signal', 0)

        if any(pd.isna(x) for x in [ema_fast_val, ema_slow_val, rsi_val]):
            return False, {}

        ema_condition = ema_fast_val < ema_slow_val

        # RSI: 30-65 для SHORT
        rsi_ok = config.get('strategy.rsi_min_short', 30) <= rsi_val <= config.get('strategy.rsi_max_short', 65)

        # MACD: або крос OR просто нижче сигналу
        macd_cross = macd_val < macd_sig and prev_macd >= prev_sig
        macd_below = macd_val < macd_sig and macd_val < 0
        macd_condition = macd_cross or macd_below

        volume_ratio = last.get('Volume_Ratio', 1.0)
        if pd.isna(volume_ratio):
            volume_ratio = 1.0
        volume_condition = volume_ratio >= self.min_volume_ratio

        bb_signal = self.get_bollinger_signal(df)
        bb_condition = bb_signal != "oversold"

        adx_val = last.get('ADX', 25)
        adx_condition = adx_val > 15 if not pd.isna(adx_val) else True

        stoch_k = last.get('StochRSI_K', 50)
        stoch_condition = stoch_k > 15 if not pd.isna(stoch_k) else True

        conditions = {
            'EMA_condition': ema_condition,
            'RSI_condition': rsi_ok,
            'MACD_condition': macd_condition,
            'Volume_condition': volume_condition,
            'BB_condition': bb_condition,
            'ADX_condition': adx_condition,
            'Stoch_condition': stoch_condition,
        }

        is_short = all(conditions.values())

        indicators = {
            'ema_fast': float(ema_fast_val),
            'ema_slow': float(ema_slow_val),
            'rsi': float(rsi_val),
            'macd': float(macd_val),
            'macd_signal': float(macd_sig),
            'volume_ratio': float(volume_ratio),
            'price': float(last['close']),
            'bb_position': float(last.get('BB_Position', 0.5)) if 'BB_Position' in df.columns else 0.5,
            'adx': float(adx_val) if not pd.isna(adx_val) else 0,
            'stoch_k': float(stoch_k) if not pd.isna(stoch_k) else 50,
            'atr_percent': float(last.get('ATR_Percent', 1.0)),
        }

        logger.debug(f"SHORT умови {self.pair}: " + ", ".join(f"{k}={'✅' if v else '❌'}" for k, v in conditions.items()))

        return is_short, indicators