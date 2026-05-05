#!/usr/bin/env python3
import sys
from pathlib import Path

# Додаємо кореневу директорію
sys.path.insert(0, str(Path(__file__).parent))

from utils.config_loader import config
from exchange.bybit_client import BybitClient
import pandas as pd
import pandas_ta as ta

exchange = BybitClient()

for pair in config.get('trading.pairs', ['BTCUSDT', 'ETHUSDT', 'SOLUSDT']):
    print(f"\n📊 Аналіз {pair}:")

    df = exchange.get_klines(pair, '5m', limit=200)

    if df is None or df.empty:
        print(f"  ❌ Немає даних для {pair}")
        continue

    print(f"  Останній об'єм: {df['volume'].iloc[-1]:.0f}")
    print(f"  Середній об'єм: {df['volume'].mean():.0f}")
    print(f"  Volume Ratio: {df['volume'].iloc[-1] / df['volume'].mean():.2f}")

    ema_fast = ta.ema(df['close'], length=50)
    ema_slow = ta.ema(df['close'], length=200)
    rsi = ta.rsi(df['close'], length=14)

    last = df.iloc[-1]

    ema_condition = ema_fast.iloc[-1] > ema_slow.iloc[-1]
    rsi_condition = 40 <= rsi.iloc[-1] <= 60

    print(f"\n  Умови для LONG:")
    print(f"    EMA: {'✅' if ema_condition else '❌'}")
    print(f"    RSI: {'✅' if rsi_condition else '❌'}")