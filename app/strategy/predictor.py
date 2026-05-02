from app.strategy.indicators import ta
from app.config import config
from loguru import logger
from typing import Dict, List, Optional
from datetime import datetime, timedelta


class Predictor:
    """Генерація прогнозів на основі технічного аналізу"""

    def __init__(self):
        self.min_signal_strength = 0.6

    def analyze(self, klines: List[Dict], symbol: str, timeframe: str) -> Optional[Dict]:
        """Аналіз ринку та генерація сигналу"""
        if len(klines) < 50:
            logger.debug(f"Not enough data for {symbol} {timeframe}: {len(klines)}")
            return None

        # Extract prices
        closes = [k['close'] for k in klines]
        volumes = [k.get('volume', 0) for k in klines]

        current_price = closes[-1]

        # Calculate indicators
        rsi = ta.calculate_rsi(closes)
        macd, signal, histogram = ta.calculate_macd(closes)
        sma_20 = ta.calculate_sma(closes, 20)
        sma_50 = ta.calculate_sma(closes, 50)
        ema_20 = ta.calculate_ema(closes, 20)
        upper_bb, middle_bb, lower_bb = ta.calculate_bollinger_bands(closes)
        avg_volume = ta.calculate_volume_sma(volumes)

        # Сигнальна система
        buy_signals = 0
        sell_signals = 0
        total_signals = 0

        # RSI
        if rsi < 30:
            buy_signals += 1
        elif rsi > 70:
            sell_signals += 1
        total_signals += 1

        # MACD
        if macd > signal and histogram > 0:
            buy_signals += 1
        elif macd < signal and histogram < 0:
            sell_signals += 1
        total_signals += 1

        # Moving averages
        if current_price > sma_20 and sma_20 > sma_50:
            buy_signals += 1
        elif current_price < sma_20 and sma_20 < sma_50:
            sell_signals += 1
        total_signals += 1

        # Bollinger Bands
        if current_price < lower_bb:
            buy_signals += 1
        elif current_price > upper_bb:
            sell_signals += 1
        total_signals += 1

        # EMA trend
        if ema_20 > sma_50:
            buy_signals += 0.5
        else:
            sell_signals += 0.5
        total_signals += 0.5

        # Volume confirmation
        current_volume = volumes[-1] if volumes else 0
        if current_volume > avg_volume * 1.2:
            if buy_signals > sell_signals:
                buy_signals += 0.5
            elif sell_signals > buy_signals:
                sell_signals += 0.5
        total_signals += 0.5

        # Розрахунок сили сигналу
        buy_strength = buy_signals / total_signals if total_signals > 0 else 0
        sell_strength = sell_signals / total_signals if total_signals > 0 else 0

        # Визначення напрямку
        if buy_strength >= self.min_signal_strength:
            direction = 'buy'
            target_pct = self._get_target_percentage(timeframe)
            target = current_price * (1 + target_pct)
            stop_loss = current_price * (1 - target_pct / 2)
            logger.info(f"🔵 BUY signal for {symbol} {timeframe}: RSI={rsi:.1f}, confidence={buy_strength:.2f}")

        elif sell_strength >= self.min_signal_strength:
            direction = 'sell'
            target_pct = self._get_target_percentage(timeframe)
            target = current_price * (1 - target_pct)
            stop_loss = current_price * (1 + target_pct / 2)
            logger.info(f"🔴 SELL signal for {symbol} {timeframe}: RSI={rsi:.1f}, confidence={sell_strength:.2f}")
        else:
            return None

        return {
            'direction': direction,
            'confidence': buy_strength if direction == 'buy' else sell_strength,
            'target': target,
            'stop_loss': stop_loss,
            'current_price': current_price,
            'indicators': {
                'rsi': rsi,
                'macd': macd,
                'signal': signal,
                'sma_20': sma_20,
                'sma_50': sma_50
            }
        }

    def _get_target_percentage(self, timeframe: str) -> float:
        """Ціль відсотка залежно від таймфрейму"""
        targets = {
            '1': 0.005,  # 0.5% (1m)
            '5': 0.01,  # 1% (5m)
            '15': 0.015,  # 1.5% (15m)
            '60': 0.02,  # 2% (1h)
            '240': 0.025,  # 2.5% (4h)
            'D': 0.03  # 3% (1d)
        }
        return targets.get(timeframe, 0.01)


predictor = Predictor()