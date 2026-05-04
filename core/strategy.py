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
            return True

        # Перевірка 15m
        try:
            last_15m = df_15m.iloc[-1]
            if signal == "LONG":
                ema_condition = last_15m[f'EMA_{self.ema_fast}'] > last_15m[f'EMA_{self.ema_slow}']
                rsi_condition = last_15m['RSI'] > 40
                if not (ema_condition and rsi_condition):
                    return False
            else:
                ema_condition = last_15m[f'EMA_{self.ema_fast}'] < last_15m[f'EMA_{self.ema_slow}']
                rsi_condition = last_15m['RSI'] < 60
                if not (ema_condition and rsi_condition):
                    return False
        except Exception as e:
            logger.debug(f"Помилка підтвердження на 15m: {e}")
            return False

        # Перевірка 1h
        if df_1h is not None and len(df_1h) > 10:
            try:
                last_1h = df_1h.iloc[-1]
                if signal == "LONG":
                    ema_condition = last_1h[f'EMA_{self.ema_fast}'] > last_1h[f'EMA_{self.ema_slow}']
                    if not ema_condition:
                        return False
                else:
                    ema_condition = last_1h[f'EMA_{self.ema_fast}'] < last_1h[f'EMA_{self.ema_slow}']
                    if not ema_condition:
                        return False
            except Exception as e:
                logger.debug(f"Помилка підтвердження на 1h: {e}")

        return True
    
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

        # Основні умови
        ema_condition = last[f'EMA_{self.ema_fast}'] > last[f'EMA_{self.ema_slow}']
        rsi_condition = self.rsi_min <= last['RSI'] <= self.rsi_max
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
            'ema_fast': float(last[f'EMA_{self.ema_fast}']),
            'ema_slow': float(last[f'EMA_{self.ema_slow}']),
            'rsi': float(last['RSI']),
            'macd': float(last['MACD']),
            'macd_signal': float(last['MACD_Signal']),
            'volume_ratio': float(last['Volume_Ratio']) if not pd.isna(last['Volume_Ratio']) else 1.0,
            'price': float(last['close']),
            'bb_position': float(last['BB_Position']) if 'BB_Position' in df.columns else 0.5,
            'adx': float(last['ADX']) if 'ADX' in df.columns else 0,
            'stoch_k': float(last['StochRSI_K']) if 'StochRSI_K' in df.columns else 50
        }

        return is_long, indicators

    def check_short_signal(self, df: pd.DataFrame) -> Tuple[bool, Dict]:
        """Перевірка сигналу SHORT з розширеними індикаторами"""
        if df is None or len(df) < 2:
            return False, {}

        last = df.iloc[-1]
        prev = df.iloc[-2]

        ema_condition = last[f'EMA_{self.ema_fast}'] < last[f'EMA_{self.ema_slow}']
        rsi_condition = self.rsi_min <= last['RSI'] <= self.rsi_max
        macd_condition = last['MACD'] < last['MACD_Signal'] and prev['MACD'] >= prev['MACD_Signal']
        volume_condition = last['Volume_Ratio'] > self.min_volume_ratio if not pd.isna(last['Volume_Ratio']) else True

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
            'ema_fast': float(last[f'EMA_{self.ema_fast}']),
            'ema_slow': float(last[f'EMA_{self.ema_slow}']),
            'rsi': float(last['RSI']),
            'macd': float(last['MACD']),
            'macd_signal': float(last['MACD_Signal']),
            'volume_ratio': float(last['Volume_Ratio']) if not pd.isna(last['Volume_Ratio']) else 1.0,
            'price': float(last['close']),
            'bb_position': float(last['BB_Position']) if 'BB_Position' in df.columns else 0.5,
            'adx': float(last['ADX']) if 'ADX' in df.columns else 0
        }

        return is_short, indicators