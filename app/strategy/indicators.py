import pandas as pd
import numpy as np
from typing import Dict, List, Tuple


class TechnicalIndicators:
    """Розрахунок технічних індикаторів"""

    @staticmethod
    def calculate_rsi(prices: List[float], period: int = 14) -> float:
        """RSI (Relative Strength Index)"""
        if len(prices) < period + 1:
            return 50.0

        df = pd.DataFrame({'close': prices})
        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))

        return float(rsi.iloc[-1])

    @staticmethod
    def calculate_macd(prices: List[float]) -> Tuple[float, float, float]:
        """MACD (Moving Average Convergence Divergence)"""
        if len(prices) < 26:
            return 0, 0, 0

        df = pd.DataFrame({'close': prices})
        exp1 = df['close'].ewm(span=12, adjust=False).mean()
        exp2 = df['close'].ewm(span=26, adjust=False).mean()
        macd = exp1 - exp2
        signal = macd.ewm(span=9, adjust=False).mean()
        histogram = macd - signal

        return float(macd.iloc[-1]), float(signal.iloc[-1]), float(histogram.iloc[-1])

    @staticmethod
    def calculate_sma(prices: List[float], period: int = 20) -> float:
        """Simple Moving Average"""
        if len(prices) < period:
            return prices[-1] if prices else 0

        return float(pd.Series(prices[-period:]).mean())

    @staticmethod
    def calculate_ema(prices: List[float], period: int = 20) -> float:
        """Exponential Moving Average"""
        if len(prices) < period:
            return prices[-1] if prices else 0

        return float(pd.Series(prices).ewm(span=period, adjust=False).mean().iloc[-1])

    @staticmethod
    def calculate_bollinger_bands(prices: List[float], period: int = 20, std_dev: int = 2) -> Tuple[
        float, float, float]:
        """Bollinger Bands (Upper, Middle, Lower)"""
        if len(prices) < period:
            return prices[-1], prices[-1], prices[-1]

        df = pd.DataFrame({'close': prices})
        middle = df['close'].rolling(window=period).mean()
        std = df['close'].rolling(window=period).std()
        upper = middle + (std * std_dev)
        lower = middle - (std * std_dev)

        return float(upper.iloc[-1]), float(middle.iloc[-1]), float(lower.iloc[-1])

    @staticmethod
    def calculate_volume_sma(volumes: List[float], period: int = 20) -> float:
        """Volume Simple Moving Average"""
        if len(volumes) < period:
            return sum(volumes) / len(volumes) if volumes else 0

        return float(pd.Series(volumes[-period:]).mean())


ta = TechnicalIndicators()