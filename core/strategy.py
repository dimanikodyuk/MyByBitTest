import pandas as pd
import pandas_ta as ta
import numpy as np
from typing import Dict, Tuple, Optional
from datetime import datetime
from utils.logger import logger
from utils.config_loader import config


class TradingStrategy:
    """Розширена торгова стратегія з додатковими індикаторами"""

    def __init__(self, pair: str):
        self.pair = pair
        self.ema_fast = config.get('strategy.ema_fast', 50)
        self.ema_slow = config.get('strategy.ema_slow', 200)
        self.rsi_period = config.get('strategy.rsi_period', 14)
        self.rsi_min = config.get('strategy.rsi_min', 40)
        self.rsi_max = config.get('strategy.rsi_max', 60)
        self.macd_fast = config.get('strategy.macd_fast', 12)
        self.macd_slow = config.get('strategy.macd_slow', 26)
        self.macd_signal = config.get('strategy.macd_signal', 9)
        self.min_volume_ratio = config.get('strategy.min_volume_ratio', 1.2)

        # Додаткові індикатори
        self.use_bollinger = config.get('strategy.use_bollinger', True)
        self.use_ichimoku = config.get('strategy.use_ichimoku', False)
        self.use_fibonacci = config.get('strategy.use_fibonacci', True)

        self.cache = {}

    def calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """Розрахунок всіх індикаторів (розширений)"""
        if df is None or len(df) < self.ema_slow:
            return df

        # EMA
        df[f'EMA_{self.ema_fast}'] = ta.ema(df['close'], length=self.ema_fast)
        df[f'EMA_{self.ema_slow}'] = ta.ema(df['close'], length=self.ema_slow)

        # RSI
        df['RSI'] = ta.rsi(df['close'], length=self.rsi_period)

        # MACD
        macd = ta.macd(df['close'], fast=self.macd_fast, slow=self.macd_slow, signal=self.macd_signal)
        df['MACD'] = macd[f'MACD_{self.macd_fast}_{self.macd_slow}_{self.macd_signal}']
        df['MACD_Signal'] = macd[f'MACDs_{self.macd_fast}_{self.macd_slow}_{self.macd_signal}']
        df['MACD_Histogram'] = macd[f'MACDh_{self.macd_fast}_{self.macd_slow}_{self.macd_signal}']

        # Volume
        df['Volume_MA'] = ta.sma(df['volume'], length=20)
        df['Volume_Ratio'] = df['volume'] / df['Volume_MA']

        # ========== ДОДАТКОВІ ІНДИКАТОРИ ==========

        # Bollinger Bands
        if self.use_bollinger:
            try:
                bb = ta.bbands(df['close'], length=20, std=2)
                # Перевіряємо правильні назви колонок
                if 'BBU_20_2.0' in bb.columns:
                    df['BB_Upper'] = bb['BBU_20_2.0']
                    df['BB_Middle'] = bb['BBM_20_2.0']
                    df['BB_Lower'] = bb['BBL_20_2.0']
                elif 'BBU_20_2.0' in bb.columns:
                    df['BB_Upper'] = bb['BBU_20_2.0']
                    df['BB_Middle'] = bb['BBM_20_2.0']
                    df['BB_Lower'] = bb['BBL_20_2.0']
                else:
                    # Альтернативні назви
                    df['BB_Upper'] = bb.iloc[:, 0] if len(bb.columns) > 0 else df['close']
                    df['BB_Middle'] = bb.iloc[:, 1] if len(bb.columns) > 1 else df['close']
                    df['BB_Lower'] = bb.iloc[:, 2] if len(bb.columns) > 2 else df['close']

                df['BB_Width'] = (df['BB_Upper'] - df['BB_Lower']) / df['BB_Middle']
                df['BB_Position'] = (df['close'] - df['BB_Lower']) / (df['BB_Upper'] - df['BB_Lower'])
            except Exception as e:
                logger.warning(f"Помилка розрахунку Bollinger Bands: {e}")

        # ATR (Average True Range) для волатильності
        try:
            df['ATR'] = ta.atr(df['high'], df['low'], df['close'], length=14)
        except:
            df['ATR'] = 0

        # Stochastic RSI
        try:
            stochrsi = ta.stochrsi(df['close'], length=14)
            if stochrsi is not None:
                if 'STOCHRSIk_14_14_3_3' in stochrsi.columns:
                    df['StochRSI_K'] = stochrsi['STOCHRSIk_14_14_3_3']
                    df['StochRSI_D'] = stochrsi['STOCHRSId_14_14_3_3']
                else:
                    df['StochRSI_K'] = 50
                    df['StochRSI_D'] = 50
            else:
                df['StochRSI_K'] = 50
                df['StochRSI_D'] = 50
        except:
            df['StochRSI_K'] = 50
            df['StochRSI_D'] = 50

        # ADX (Average Directional Index)
        try:
            adx = ta.adx(df['high'], df['low'], df['close'], length=14)
            if adx is not None:
                df['ADX'] = adx['ADX_14'] if 'ADX_14' in adx.columns else 0
                df['DMP'] = adx['DMP_14'] if 'DMP_14' in adx.columns else 0
                df['DMN'] = adx['DMN_14'] if 'DMN_14' in adx.columns else 0
            else:
                df['ADX'] = 0
        except:
            df['ADX'] = 0

        return df

    def confirm_with_timeframe(self, df_15m: pd.DataFrame, df_1h: pd.DataFrame, signal: str) -> bool:
        """Підтвердження сигналу на більших таймфреймах"""
        if signal not in ["LONG", "SHORT"]:
            return False

        # Якщо немає даних для підтвердження - пропускаємо перевірку
        if df_15m is None or len(df_15m) < 10:
            logger.debug("Немає даних 15m - пропускаємо перевірку")
            return True

        try:
            # Отримуємо назви колонок
            ema_fast_col = f'EMA_{self.ema_fast}'
            ema_slow_col = f'EMA_{self.ema_slow}'

            # Перевіряємо чи є колонки в даних
            if ema_fast_col not in df_15m.columns or 'RSI' not in df_15m.columns:
                logger.debug(f"Відсутні колонки в 15m: {ema_fast_col} або RSI")
                return True

            last_15m = df_15m.iloc[-1]

            if signal == "LONG":
                ema_condition = last_15m[ema_fast_col] > last_15m[ema_slow_col]
                rsi_condition = 30 <= last_15m['RSI'] <= 75
                logger.debug(f"15m LONG: EMA={ema_condition}, RSI={last_15m['RSI']:.1f} ({rsi_condition})")
                if not (ema_condition and rsi_condition):
                    return False
            else:  # SHORT
                ema_condition = last_15m[ema_fast_col] < last_15m[ema_slow_col]
                rsi_condition = 25 <= last_15m['RSI'] <= 70
                logger.debug(f"15m SHORT: EMA={ema_condition}, RSI={last_15m['RSI']:.1f} ({rsi_condition})")
                if not (ema_condition and rsi_condition):
                    return False
        except Exception as e:
            logger.error(f"Помилка підтвердження на 15m: {e}")
            return False

        # Перевірка 1h
        if df_1h is not None and len(df_1h) > 10:
            try:
                ema_fast_col = f'EMA_{self.ema_fast}'
                ema_slow_col = f'EMA_{self.ema_slow}'

                if ema_fast_col not in df_1h.columns:
                    logger.debug(f"Відсутні колонки в 1h: {ema_fast_col}")
                    return True

                last_1h = df_1h.iloc[-1]
                if signal == "LONG":
                    if last_1h[ema_fast_col] <= last_1h[ema_slow_col]:
                        logger.debug(
                            f"1h LONG: EMA не підтверджує ({last_1h[ema_fast_col]:.2f} <= {last_1h[ema_slow_col]:.2f})")
                        return False
                    else:
                        logger.debug(f"1h LONG: EMA підтверджує")
                else:  # SHORT
                    if last_1h[ema_fast_col] >= last_1h[ema_slow_col]:
                        logger.debug(f"1h SHORT: EMA не підтверджує")
                        return False
                    else:
                        logger.debug(f"1h SHORT: EMA підтверджує")
            except Exception as e:
                logger.debug(f"Помилка підтвердження на 1h: {e}")

        logger.info(f"✅ Сигнал {signal} ПІДТВЕРДЖЕНО на 15m/1h")
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

        # Розрахунок тіл та тіней
        body = abs(last['close'] - last['open'])
        upper_shadow = last['high'] - max(last['close'], last['open'])
        lower_shadow = min(last['close'], last['open']) - last['low']
        total_range = last['high'] - last['low'] if last['high'] != last['low'] else 1

        prev_body = abs(prev['close'] - prev['open'])
        prev_total_range = prev['high'] - prev['low'] if prev['high'] != prev['low'] else 1

        # 1. Молот (Hammer) - довга нижня тінь, маленьке тіло
        if (lower_shadow > body * 2 and upper_shadow < body * 0.5 and
                body > 0 and total_range > 0 and lower_shadow / total_range > 0.6):
            patterns.append({
                "name": "hammer",
                "type": "bullish",
                "strength": "strong",
                "description": "Молот - потенційний розворот вгору"
            })
            signal = "bullish"

        # 2. Висячий чоловічок (Hanging Man) - на вершині
        if (lower_shadow > body * 2 and upper_shadow < body * 0.5 and
                last['close'] < last['open'] and last['high'] > prev['high']):
            patterns.append({
                "name": "hanging_man",
                "type": "bearish",
                "strength": "strong",
                "description": "Висячий чоловічок - потенційний розворот вниз"
            })
            signal = "bearish"

        # 3. Поглинання биче (Bullish Engulfing)
        if (last['close'] > last['open'] and prev['close'] < prev['open'] and
                last['close'] > prev['open'] and last['open'] < prev['close']):
            patterns.append({
                "name": "bullish_engulfing",
                "type": "bullish",
                "strength": "very_strong",
                "description": "Биче поглинання - сильний розворот вгору"
            })
            signal = "bullish"

        # 4. Поглинання ведмеже (Bearish Engulfing)
        if (last['close'] < last['open'] and prev['close'] > prev['open'] and
                last['close'] < prev['open'] and last['open'] > prev['close']):
            patterns.append({
                "name": "bearish_engulfing",
                "type": "bearish",
                "strength": "very_strong",
                "description": "Ведмеже поглинання - сильний розворот вниз"
            })
            signal = "bearish"

        # 5. Доджі (Doji)
        if total_range > 0 and body / total_range < 0.1:
            patterns.append({
                "name": "doji",
                "type": "neutral",
                "strength": "weak",
                "description": "Доджі - невизначеність, можливий розворот"
            })

        # 6. Ранкова зірка (Morning Star)
        if (prev2['close'] < prev2['open'] and  # перша ведмежа
                abs(prev['close'] - prev['open']) < abs(prev2['close'] - prev2['open']) * 0.3 and
                last['close'] > last['open'] and
                last['close'] > (prev2['high'] + prev2['low']) / 2):
            patterns.append({
                "name": "morning_star",
                "type": "bullish",
                "strength": "very_strong",
                "description": "Ранкова зірка - сильний розворот вгору"
            })
            signal = "bullish"

        # 7. Вечірня зірка (Evening Star)
        if (prev2['close'] > prev2['open'] and
                abs(prev['close'] - prev['open']) < abs(prev2['close'] - prev2['open']) * 0.3 and
                last['close'] < last['open'] and
                last['close'] < (prev2['high'] + prev2['low']) / 2):
            patterns.append({
                "name": "evening_star",
                "type": "bearish",
                "strength": "very_strong",
                "description": "Вечірня зірка - сильний розворот вниз"
            })
            signal = "bearish"

        # 8. Молот на ведмежому тренді (підтверджений)
        if (signal == "bullish" and last['close'] < prev['close'] and
                lower_shadow > body * 2.5):
            patterns.append({
                "name": "hammer_confirmed",
                "type": "bullish",
                "strength": "strong",
                "description": "Підтверджений молот - хороший сигнал на купівлю"
            })

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
        bb_pos = last['BB_Position']

        if bb_pos < 0.1:
            return "oversold"  # Можливий розворот вгору
        elif bb_pos > 0.9:
            return "overbought"  # Можливий розворот вниз
        elif bb_pos > 0.3 and bb_pos < 0.7:
            return "neutral"
        return "neutral"

    def get_kelly_criterion(self, win_rate: float, avg_win: float, avg_loss: float) -> float:
        """
        Kelly Criterion для оптимального розміру позиції
        f = (p * b - q) / b
        де p = win_rate, q = 1-p, b = avg_win/avg_loss
        """
        if avg_loss == 0:
            return 0.1

        b = avg_win / avg_loss
        p = win_rate / 100
        q = 1 - p

        kelly = (p * b - q) / b

        # Обмежуємо від 0 до 0.25 (максимум 25% від балансу)
        return max(0, min(kelly, 0.25))

    def check_long_signal(self, df: pd.DataFrame) -> Tuple[bool, Dict]:
        """Перевірка сигналу LONG з розширеними індикаторами"""
        if df is None or len(df) < 2:
            return False, {}

        last = df.iloc[-1]
        prev = df.iloc[-2]

        # Основні умови (використовуємо self.ema_fast який має бути 21 в config)
        ema_fast_col = f'EMA_{self.ema_fast}'
        ema_slow_col = f'EMA_{self.ema_slow}'

        if ema_fast_col not in last or ema_slow_col not in last:
            return False, {}

        ema_condition = last[ema_fast_col] > last[ema_slow_col]
        # LONG: RSI в межах 35-70 (нейтральна зона без перекупленості)
        rsi_condition = 35 <= last['RSI'] <= 70
        macd_condition = last['MACD'] > last['MACD_Signal'] and prev['MACD'] <= prev['MACD_Signal']
        volume_condition = last['Volume_Ratio'] > self.min_volume_ratio if not pd.isna(last['Volume_Ratio']) else True

        # Додаткові підтвердження
        bb_signal = self.get_bollinger_signal(df)
        bb_condition = bb_signal != "overbought"

        # ADX підтвердження (тренд сильний)
        adx_condition = last['ADX'] > 20 if 'ADX' in df.columns else True

        # StochRSI підтвердження
        stoch_condition = last['StochRSI_K'] < 80 if 'StochRSI_K' in df.columns else True

        conditions = {
            'EMA_condition': ema_condition,
            'RSI_condition': rsi_condition,
            'MACD_condition': macd_condition,
            'Volume_condition': volume_condition,
            'BB_condition': bb_condition,
            'ADX_condition': adx_condition,
            'Stoch_condition': stoch_condition
        }

        is_long = all(conditions.values())

        indicators = {
            'ema_fast': float(last[ema_fast_col]),
            'ema_slow': float(last[ema_slow_col]),
            'rsi': float(last['RSI']),
            'macd': float(last['MACD']),
            'macd_signal': float(last['MACD_Signal']),
            'volume_ratio': float(last['Volume_Ratio']) if not pd.isna(last['Volume_Ratio']) else 1.0,
            'price': float(last['close']),
            'bb_position': float(last['BB_Position']) if 'BB_Position' in df.columns else 0.5,
            'adx': float(last['ADX']) if 'ADX' in df.columns else 0,
            'stoch_k': float(last['StochRSI_K']) if 'StochRSI_K' in df.columns else 50
        }

        logger.debug(f"LONG сигнал: EMA={ema_condition}, RSI={rsi_condition}, MACD={macd_condition}, "
                     f"VOL={volume_condition}, BB={bb_condition}, ADX={adx_condition}, Stoch={stoch_condition}")

        return is_long, indicators

    def check_short_signal(self, df: pd.DataFrame) -> Tuple[bool, Dict]:
        """Перевірка сигналу SHORT з розширеними індикаторами"""
        if df is None or len(df) < 2:
            return False, {}

        last = df.iloc[-1]
        prev = df.iloc[-2]

        ema_fast_col = f'EMA_{self.ema_fast}'
        ema_slow_col = f'EMA_{self.ema_slow}'

        if ema_fast_col not in last or ema_slow_col not in last:
            return False, {}

        ema_condition = last[ema_fast_col] < last[ema_slow_col]
        # SHORT: RSI в межах 30-65 (нейтральна зона без перепродяності)
        rsi_condition = 30 <= last['RSI'] <= 65
        macd_condition = last['MACD'] < last['MACD_Signal'] and prev['MACD'] >= prev['MACD_Signal']
        volume_condition = last['Volume_Ratio'] > self.min_volume_ratio if not pd.isna(last['Volume_Ratio']) else True

        # Додаткові підтвердження
        bb_signal = self.get_bollinger_signal(df)
        bb_condition = bb_signal != "oversold"
        adx_condition = last['ADX'] > 20 if 'ADX' in df.columns else True
        stoch_condition = last['StochRSI_K'] > 20 if 'StochRSI_K' in df.columns else True

        conditions = {
            'EMA_condition': ema_condition,
            'RSI_condition': rsi_condition,
            'MACD_condition': macd_condition,
            'Volume_condition': volume_condition,
            'BB_condition': bb_condition,
            'ADX_condition': adx_condition,
            'Stoch_condition': stoch_condition
        }

        is_short = all(conditions.values())

        indicators = {
            'ema_fast': float(last[ema_fast_col]),
            'ema_slow': float(last[ema_slow_col]),
            'rsi': float(last['RSI']),
            'macd': float(last['MACD']),
            'macd_signal': float(last['MACD_Signal']),
            'volume_ratio': float(last['Volume_Ratio']) if not pd.isna(last['Volume_Ratio']) else 1.0,
            'price': float(last['close']),
            'bb_position': float(last['BB_Position']) if 'BB_Position' in df.columns else 0.5,
            'adx': float(last['ADX']) if 'ADX' in df.columns else 0,
            'stoch_k': float(last['StochRSI_K']) if 'StochRSI_K' in df.columns else 50
        }

        logger.debug(f"SHORT сигнал: EMA={ema_condition}, RSI={rsi_condition}, MACD={macd_condition}, "
                     f"VOL={volume_condition}, BB={bb_condition}, ADX={adx_condition}, Stoch={stoch_condition}")

        return is_short, indicators