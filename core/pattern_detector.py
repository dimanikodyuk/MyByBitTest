"""
Детектор фігур технічного аналізу
Клин, Прапор, Вимпел, Ромб, Трендові лінії
"""

import pandas as pd
import numpy as np
from typing import List, Dict, Tuple, Optional
from dataclasses import dataclass
from datetime import datetime


@dataclass
class Pattern:
    """Клас для зберігання інформації про фігуру"""
    name: str
    type: str  # 'bullish', 'bearish', 'neutral'
    start_idx: int
    end_idx: int
    points: List[Tuple]
    description: str


class PatternDetector:
    """Детектор фігур технічного аналізу"""

    def __init__(self, df: pd.DataFrame):
        self.df = df.reset_index(drop=True)
        self.patterns: List[Pattern] = []

    def detect_all(self) -> List[Pattern]:
        """Запуск всіх детекторів"""
        self.detect_wedge()
        self.detect_flag_pennant()
        self.detect_diamond()
        self.detect_trendlines()
        return self.patterns

    def detect_wedge(self):
        """Детектор клина (сужающиеся максимуми/мінімуми)"""
        if len(self.df) < 20:
            return

        highs = self.df['high'].values
        lows = self.df['low'].values
        n = len(highs)

        pivot_highs = []
        pivot_lows = []

        for i in range(2, n - 2):
            if highs[i] > highs[i-1] and highs[i] > highs[i-2] and highs[i] > highs[i+1] and highs[i] > highs[i+2]:
                pivot_highs.append((i, float(highs[i])))
            if lows[i] < lows[i-1] and lows[i] < lows[i-2] and lows[i] < lows[i+1] and lows[i] < lows[i+2]:
                pivot_lows.append((i, float(lows[i])))

        if len(pivot_highs) < 2 or len(pivot_lows) < 2:
            return

        # Кут нахилу по двох останніх точках
        x1, y1 = pivot_highs[-2]
        x2, y2 = pivot_highs[-1]
        high_slope = (y2 - y1) / (x2 - x1) if x2 != x1 else 0

        x1, y1 = pivot_lows[-2]
        x2, y2 = pivot_lows[-1]
        low_slope = (y2 - y1) / (x2 - x1) if x2 != x1 else 0

        # Клин: обидві лінії збігаються
        if high_slope < 0 and low_slope > 0:
            pattern_type = 'bullish' if self.df['close'].iloc[-1] > self.df['close'].iloc[-10] else 'bearish'
            pts = pivot_highs[-2:] + pivot_lows[-2:]
            start = min(p[0] for p in pts)
            end = max(p[0] for p in pts)
            self.patterns.append(Pattern(
                name='Wedge (Клин)',
                type=pattern_type,
                start_idx=start,
                end_idx=end,
                points=pts,
                description='Сходящиеся линии - ожидается пробой'
            ))

    def detect_flag_pennant(self):
        """Детектор прапора та вимпела"""
        if len(self.df) < 15:
            return

        # Шукаємо імпульс (різкий рух)
        prices = self.df['close'].values
        flagpole_start = 0
        flagpole_end = 0
        max_move = 0

        for i in range(5, len(prices) - 5):
            move = abs(prices[i] - prices[i - 5]) / prices[i - 5] * 100
            if move > max_move:
                max_move = move
                flagpole_start = i - 5
                flagpole_end = i

        if max_move < 2:  # Мінімальний рух 2%
            return

        # Перевіряємо консолідацію після імпульсу (наступні 5-10 свічок)
        consolidation_start = flagpole_end
        consolidation_end = min(len(prices), consolidation_start + 10)

        if consolidation_end - consolidation_start < 3:
            return

        # Розраховуємо волатильність під час консолідації
        consolidation_highs = self.df['high'].iloc[consolidation_start:consolidation_end].max()
        consolidation_lows = self.df['low'].iloc[consolidation_start:consolidation_end].min()
        consolidation_range = (consolidation_highs - consolidation_lows) / consolidation_lows * 100

        if consolidation_range < 2:  # Мала волатильність - прапор
            pattern_type = 'bullish' if prices[flagpole_end] > prices[flagpole_start] else 'bearish'
            self.patterns.append(Pattern(
                name='Flag (Прапор)',
                type=pattern_type,
                start_idx=flagpole_start,
                end_idx=consolidation_end - 1,
                points=[(flagpole_start, prices[flagpole_start]), (flagpole_end, prices[flagpole_end]),
                        (consolidation_end - 1, prices[consolidation_end - 1])],
                description='Різкий рух + консолідація'
            ))

    def detect_diamond(self):
        """Детектор ромба/діаманта (розширення + звуження)"""
        if len(self.df) < 20:
            return

        highs = self.df['high'].values
        lows = self.df['low'].values
        n = len(highs)

        # Шукаємо розширення
        expansion_start = 0
        expansion_end = 0
        max_width = 0

        window = 10
        for i in range(window, n - window):
            left_width = (highs[i - window:i + 1].max() - lows[i - window:i + 1].min()) / (
                        lows[i - window:i + 1].min() + 1e-10)
            right_width = (highs[i:i + window + 1].max() - lows[i:i + window + 1].min()) / (
                        lows[i:i + window + 1].min() + 1e-10)

            if left_width < right_width and right_width > max_width:
                max_width = right_width
                expansion_start = max(0, i - window // 2)
                expansion_end = min(n - 1, i + window // 2)

        if expansion_start == 0 or expansion_start >= expansion_end:
            return

        # Перевіряємо звуження після розширення
        contraction_start = expansion_end
        contraction_end = min(n, contraction_start + 10)

        if contraction_end - contraction_start < 3:
            return

        contraction_width = (highs[contraction_start:contraction_end].max() - lows[
            contraction_start:contraction_end].min()) / (lows[contraction_start:contraction_end].min() + 1e-10)

        if contraction_width < max_width * 0.5:  # Звуження на 50%
            self.patterns.append(Pattern(
                name='Diamond (Ромб)',
                type='neutral',
                start_idx=expansion_start,
                end_idx=contraction_end - 1,
                points=[(expansion_start, highs[expansion_start]), (expansion_end, highs[expansion_end]),
                        (contraction_end - 1, lows[contraction_end - 1])],
                description='Розширення → звуження'
            ))

    def detect_trendlines(self):
        """Детектор трендових ліній (підтримка/опір)"""
        if len(self.df) < 10:
            return

        highs = self.df['high'].values
        lows = self.df['low'].values
        n = len(highs)

        # Шукаємо піки для лінії опору
        resistance_points = []
        support_points = []

        for i in range(2, n - 2):
            if highs[i] > highs[i - 1] and highs[i] > highs[i - 2] and highs[i] > highs[i + 1] and highs[i] > highs[
                i + 2]:
                resistance_points.append((i, highs[i]))
            if lows[i] < lows[i - 1] and lows[i] < lows[i - 2] and lows[i] < lows[i + 1] and lows[i] < lows[i + 2]:
                support_points.append((i, lows[i]))

        # Додаємо трендові лінії (якщо є хоча б 2 точки)
        if len(resistance_points) >= 2:
            self.patterns.append(Pattern(
                name='Resistance (Опір)',
                type='bearish',
                start_idx=resistance_points[0][0],
                end_idx=resistance_points[-1][0],
                points=resistance_points,
                description='Лінія опору'
            ))

        if len(support_points) >= 2:
            self.patterns.append(Pattern(
                name='Support (Підтримка)',
                type='bullish',
                start_idx=support_points[0][0],
                end_idx=support_points[-1][0],
                points=support_points,
                description='Лінія підтримки'
            ))