"""
Бектестер для перевірки стратегій на історичних даних
"""

import pandas as pd
import numpy as np
import pandas_ta as ta
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, asdict
from utils.logger import logger
from utils.config_loader import config
from exchange.bybit_client import BybitClient
from core.strategy import TradingStrategy


@dataclass
class BacktestTrade:
    """Угода в бектесті"""
    entry_time: str
    exit_time: str
    side: str
    entry_price: float
    exit_price: float
    quantity: float
    pnl: float
    pnl_percent: float


class BacktestEngine:
    """Двигун для бектестування стратегій"""

    def __init__(self):
        self.exchange = BybitClient()

    def get_historical_data(self, symbol: str, timeframe: str, start_date: datetime,
                            end_date: datetime) -> pd.DataFrame:
        """Отримання історичних даних з Bybit"""
        logger.info(f"Завантаження даних для {symbol} {timeframe} з {start_date} по {end_date}")

        # Bybit обмежує дані останніми 30-90 днями
        max_days = 60
        if timeframe in ['1m', '3m', '5m']:
            max_days = 30
        elif timeframe in ['15m', '30m', '1h']:
            max_days = 60
        else:
            max_days = 90

        now = datetime.now()
        earliest_allowed = now - timedelta(days=max_days)

        if start_date < earliest_allowed:
            start_date = earliest_allowed

        if end_date > now:
            end_date = now

        all_data = []
        current_start = start_date
        limit = 200

        while current_start < end_date:
            df = self.exchange.get_klines(symbol, timeframe, limit=limit)
            if df is None or df.empty:
                break

            df['timestamp'] = pd.to_datetime(df['timestamp'])
            df = df[df['timestamp'] >= current_start]

            if not df.empty:
                all_data.append(df)
                current_start = df['timestamp'].iloc[-1] + pd.Timedelta(minutes=1)
            else:
                break

            import time
            time.sleep(0.1)

        if not all_data:
            logger.error(f"Не вдалося завантажити дані для {symbol} {timeframe}")
            return pd.DataFrame()

        result = pd.concat(all_data, ignore_index=True)
        result = result[result['timestamp'] <= end_date]
        result = result.sort_values('timestamp')
        result = result.drop_duplicates(subset=['timestamp'])

        logger.info(
            f"Завантажено {len(result)} свічок за період {result['timestamp'].min()} - {result['timestamp'].max()}")

        return result

    def run_backtest(self, symbol: str, timeframe: str, start_date: datetime, end_date: datetime,
                     initial_balance: float = 1000.0, risk_percent: float = 2.0) -> Dict:
        """
        Запуск бектесту стратегії на історичних даних
        """
        logger.info(f"Запуск бектесту для {symbol} {timeframe} з {start_date} по {end_date}")

        # Коригуємо дати
        now = datetime.now()
        max_days = 60
        if timeframe in ['1m', '3m', '5m']:
            max_days = 30
        elif timeframe in ['15m', '30m', '1h']:
            max_days = 60
        else:
            max_days = 90

        earliest_allowed = now - timedelta(days=max_days)

        if start_date < earliest_allowed:
            logger.warning(f"Дата початку {start_date.date()} старіша за дозволену, коригую")
            start_date = earliest_allowed

        if end_date > now:
            end_date = now

        logger.info(f"Скориговані дати: {start_date.date()} - {end_date.date()}")

        # Отримуємо дані
        df = self.get_historical_data(symbol, timeframe, start_date, end_date)

        if df.empty:
            return {'error': f'Немає даних для вказаного періоду'}

        if len(df) < 50:
            return {'error': f'Недостатньо даних: отримано {len(df)} свічок'}

        logger.info(f"Отримано {len(df)} свічок для аналізу")

        # ========== ПРИМУСОВИЙ РОЗРАХУНОК ІНДИКАТОРІВ ==========
        logger.info("Розрахунок індикаторів...")

        # Конвертуємо ціни
        df['close'] = df['close'].astype(float)
        df['high'] = df['high'].astype(float)
        df['low'] = df['low'].astype(float)
        df['open'] = df['open'].astype(float)
        df['volume'] = df['volume'].astype(float)

        # EMA
        ema_fast_period = config.get('strategy.ema_fast', 21)
        ema_slow_period = config.get('strategy.ema_slow', 200)

        df[f'EMA_{ema_fast_period}'] = ta.ema(df['close'], length=ema_fast_period)
        df[f'EMA_{ema_slow_period}'] = ta.ema(df['close'], length=ema_slow_period)
        df['EMA_50'] = ta.ema(df['close'], length=50)

        # RSI
        rsi_period = config.get('strategy.rsi_period', 14)
        df['RSI'] = ta.rsi(df['close'], length=rsi_period)

        # MACD
        macd_fast = config.get('strategy.macd_fast', 12)
        macd_slow = config.get('strategy.macd_slow', 26)
        macd_signal_period = config.get('strategy.macd_signal', 9)

        macd_result = ta.macd(df['close'], fast=macd_fast, slow=macd_slow, signal=macd_signal_period)
        if macd_result is not None:
            # Шукаємо правильні назви колонок
            macd_col = [c for c in macd_result.columns if 'MACD_' in c and 'signal' not in c.lower()]
            signal_col = [c for c in macd_result.columns if 'signal' in c.lower()]
            if macd_col:
                df['MACD'] = macd_result[macd_col[0]]
            if signal_col:
                df['MACD_Signal'] = macd_result[signal_col[0]]

        # ATR
        df['ATR'] = ta.atr(df['high'], df['low'], df['close'], length=14)
        df['ATR_Percent'] = (df['ATR'] / df['close']) * 100

        # ADX
        adx_result = ta.adx(df['high'], df['low'], df['close'], length=14)
        if adx_result is not None:
            adx_col = [c for c in adx_result.columns if 'ADX_' in c]
            if adx_col:
                df['ADX'] = adx_result[adx_col[0]]

        # Volume
        df['Volume_MA'] = ta.sma(df['volume'], length=20)
        df['Volume_Ratio'] = df['volume'] / df['Volume_MA'].replace(0, np.nan)

        # Bollinger Bands (універсальний пошук)
        bb_result = ta.bbands(df['close'], length=20, std=2)
        if bb_result is not None:
            upper_col = [c for c in bb_result.columns if 'UPPER' in c or 'BBU' in c]
            middle_col = [c for c in bb_result.columns if 'MIDDLE' in c or 'BBM' in c]
            lower_col = [c for c in bb_result.columns if 'LOWER' in c or 'BBL' in c]

            if upper_col:
                df['BB_Upper'] = bb_result[upper_col[0]]
            if middle_col:
                df['BB_Middle'] = bb_result[middle_col[0]]
            if lower_col:
                df['BB_Lower'] = bb_result[lower_col[0]]

            if 'BB_Upper' in df.columns and 'BB_Lower' in df.columns:
                df['BB_Width'] = (df['BB_Upper'] - df['BB_Lower']) / df['BB_Middle']
                df['BB_Position'] = (df['close'] - df['BB_Lower']) / (df['BB_Upper'] - df['BB_Lower']).replace(0,
                                                                                                               np.nan)

        # Заповнюємо NaN значення
        df = df.bfill().ffill().fillna(0)

        # Діагностика
        logger.info("=" * 50)
        logger.info("ДІАГНОСТИКА ІНДИКАТОРІВ:")
        logger.info(f"Кількість свічок: {len(df)}")

        # Перевіряємо наявність колонок
        required_cols = [f'EMA_{ema_fast_period}', f'EMA_{ema_slow_period}', 'RSI', 'MACD', 'MACD_Signal', 'ADX',
                         'Volume_Ratio']
        for col in required_cols:
            if col in df.columns:
                non_null = df[col].notna().sum()
                logger.info(f"  {col}: присутня, не-NaN: {non_null}/{len(df)}")
            else:
                logger.warning(f"  {col}: ВІДСУТНЯ")

        # Якщо EMA_200 немає - використовуємо EMA_50
        if df[f'EMA_{ema_slow_period}'].notna().sum() < 50:
            logger.warning(f"EMA_{ema_slow_period} недоступна, використовую EMA_50")
            df[f'EMA_{ema_slow_period}'] = df['EMA_50']

        logger.info("=" * 50)

        # Ініціалізуємо стан
        balance = initial_balance
        trades = []
        equity_values = [initial_balance]
        equity_timestamps = [df['timestamp'].iloc[0]]
        current_position = None

        # Параметри
        position_size_pct = 0.2
        signal_count = {'long_signals': 0, 'short_signals': 0, 'exits': 0}

        # Параметри з конфігу
        rsi_min_long = config.get('strategy.rsi_min_long', 40)
        rsi_max_long = config.get('strategy.rsi_max_long', 65)
        rsi_min_short = config.get('strategy.rsi_min_short', 35)
        rsi_max_short = config.get('strategy.rsi_max_short', 60)
        min_adx = config.get('strategy.min_adx', 20)
        min_volume_ratio = config.get('strategy.min_volume_ratio', 1.2)
        sl_multiplier = config.get('strategy.atr_sl_multiplier', 1.5)
        tp_multiplier = config.get('strategy.atr_tp_multiplier', 2.5)

        # Симуляція
        total_candles = len(df)
        start_idx = 100

        for i in range(start_idx, total_candles):
            current = df.iloc[i]
            timestamp = current['timestamp']
            price = float(current['close'])

            # Отримуємо індикатори
            ema_fast = current.get(f'EMA_{ema_fast_period}', 0)
            ema_slow = current.get(f'EMA_{ema_slow_period}', 0)
            rsi = current.get('RSI', 50)
            macd = current.get('MACD', 0)
            macd_signal_val = current.get('MACD_Signal', 0)
            adx = current.get('ADX', 0)
            volume_ratio = current.get('Volume_Ratio', 1.0)

            # Пропускаємо NaN
            if pd.isna(ema_fast) or pd.isna(ema_slow) or pd.isna(rsi):
                continue

            # ========== ВХІД ==========
            if current_position is None:
                # LONG сигнал
                ema_ok_long = ema_fast > ema_slow
                rsi_ok_long = rsi_min_long <= rsi <= rsi_max_long
                macd_ok_long = macd > macd_signal_val
                adx_ok = adx > min_adx if not pd.isna(adx) else True
                volume_ok = volume_ratio > min_volume_ratio if not pd.isna(volume_ratio) else True

                is_long = ema_ok_long and rsi_ok_long and macd_ok_long and adx_ok and volume_ok

                if is_long:
                    signal_count['long_signals'] += 1
                    position_value = balance * position_size_pct
                    quantity = position_value / price

                    min_qty = 0.0001 if symbol == 'BTCUSDT' else 0.001
                    if quantity < min_qty:
                        quantity = min_qty
                        position_value = quantity * price

                    if position_value <= balance and quantity > 0:
                        current_position = {
                            'side': 'LONG',
                            'entry_price': price,
                            'quantity': quantity,
                            'entry_time': timestamp,
                            'balance_before': balance
                        }
                        balance -= position_value
                        logger.info(f"LONG відкрито при ${price:.2f} на {timestamp.strftime('%Y-%m-%d %H:%M')}")

                # SHORT сигнал
                if not is_long:
                    ema_ok_short = ema_fast < ema_slow
                    rsi_ok_short = rsi_min_short <= rsi <= rsi_max_short
                    macd_ok_short = macd < macd_signal_val

                    is_short = ema_ok_short and rsi_ok_short and macd_ok_short and adx_ok and volume_ok

                    if is_short:
                        signal_count['short_signals'] += 1
                        position_value = balance * position_size_pct
                        quantity = position_value / price

                        min_qty = 0.0001 if symbol == 'BTCUSDT' else 0.001
                        if quantity < min_qty:
                            quantity = min_qty
                            position_value = quantity * price

                        if position_value <= balance and quantity > 0:
                            current_position = {
                                'side': 'SHORT',
                                'entry_price': price,
                                'quantity': quantity,
                                'entry_time': timestamp,
                                'balance_before': balance
                            }
                            balance -= position_value
                            logger.info(f"SHORT відкрито при ${price:.2f} на {timestamp.strftime('%Y-%m-%d %H:%M')}")

            # ========== ВИХІД ==========
            elif current_position is not None:
                entry_price = current_position['entry_price']
                quantity = current_position['quantity']
                side = current_position['side']

                atr = float(current.get('ATR', price * 0.01))
                if atr <= 0 or pd.isna(atr):
                    atr = price * 0.01

                should_exit = False
                exit_reason = ""

                if side == 'LONG':
                    sl_price = entry_price - atr * sl_multiplier
                    tp_price = entry_price + atr * tp_multiplier

                    if price <= sl_price:
                        should_exit = True
                        exit_reason = "STOP_LOSS"
                    elif price >= tp_price:
                        should_exit = True
                        exit_reason = "TAKE_PROFIT"
                else:
                    sl_price = entry_price + atr * sl_multiplier
                    tp_price = entry_price - atr * tp_multiplier

                    if price >= sl_price:
                        should_exit = True
                        exit_reason = "STOP_LOSS"
                    elif price <= tp_price:
                        should_exit = True
                        exit_reason = "TAKE_PROFIT"

                # Протилежний сигнал
                if not should_exit:
                    if side == 'LONG':
                        ema_ok_short = ema_fast < ema_slow
                        rsi_ok_short = rsi_min_short <= rsi <= rsi_max_short
                        macd_ok_short = macd < macd_signal_val
                        if ema_ok_short and rsi_ok_short and macd_ok_short:
                            should_exit = True
                            exit_reason = "REVERSE_SIGNAL"
                    else:
                        ema_ok_long = ema_fast > ema_slow
                        rsi_ok_long = rsi_min_long <= rsi <= rsi_max_long
                        macd_ok_long = macd > macd_signal_val
                        if ema_ok_long and rsi_ok_long and macd_ok_long:
                            should_exit = True
                            exit_reason = "REVERSE_SIGNAL"

                if should_exit:
                    signal_count['exits'] += 1

                    if side == 'LONG':
                        pnl = (price - entry_price) * quantity
                        balance += quantity * price
                    else:
                        pnl = (entry_price - price) * quantity
                        balance += quantity * price

                    pnl_percent = (pnl / (entry_price * quantity)) * 100 if entry_price > 0 else 0

                    commission = (quantity * price) * 0.001
                    balance -= commission
                    pnl -= commission

                    trade = {
                        'entry_time': current_position['entry_time'].isoformat(),
                        'exit_time': timestamp.isoformat(),
                        'side': side,
                        'entry_price': round(entry_price, 2),
                        'exit_price': round(price, 2),
                        'quantity': round(quantity, 6),
                        'pnl': round(pnl, 2),
                        'pnl_percent': round(pnl_percent, 2),
                        'exit_reason': exit_reason
                    }
                    trades.append(trade)

                    logger.info(
                        f"{side} закрито при ${price:.2f}, PnL: ${pnl:.2f} ({pnl_percent:.1f}%) - {exit_reason}")

                    current_position = None
                    equity_values.append(balance)
                    equity_timestamps.append(timestamp)

            # Проміжні точки
            if i % 20 == 0 and current_position is None and equity_timestamps[-1] != timestamp:
                equity_values.append(balance)
                equity_timestamps.append(timestamp)

        # Закриваємо позицію в кінці
        if current_position is not None:
            price = df.iloc[-1]['close']
            timestamp = df.iloc[-1]['timestamp']

            if current_position['side'] == 'LONG':
                pnl = (price - current_position['entry_price']) * current_position['quantity']
                balance += current_position['quantity'] * price
            else:
                pnl = (current_position['entry_price'] - price) * current_position['quantity']
                balance += current_position['quantity'] * price

            pnl_percent = (pnl / (current_position['entry_price'] * current_position['quantity'])) * 100 if \
            current_position['entry_price'] > 0 else 0
            commission = (current_position['quantity'] * price) * 0.001
            balance -= commission
            pnl -= commission

            trade = {
                'entry_time': current_position['entry_time'].isoformat(),
                'exit_time': timestamp.isoformat(),
                'side': current_position['side'],
                'entry_price': round(current_position['entry_price'], 2),
                'exit_price': round(price, 2),
                'quantity': round(current_position['quantity'], 6),
                'pnl': round(pnl, 2),
                'pnl_percent': round(pnl_percent, 2),
                'exit_reason': 'END_OF_BACKTEST'
            }
            trades.append(trade)
            equity_values.append(balance)
            equity_timestamps.append(timestamp)

        # Підсумки
        logger.info("=" * 50)
        logger.info("ПІДСУМКИ БЕКТЕСТУ:")
        logger.info(f"  LONG сигналів: {signal_count['long_signals']}")
        logger.info(f"  SHORT сигналів: {signal_count['short_signals']}")
        logger.info(f"  Виконано угод: {len(trades)}")
        logger.info("=" * 50)

        # Статистика
        if not trades:
            total_return_pct = ((balance - initial_balance) / initial_balance) * 100 if initial_balance > 0 else 0
            return {
                'symbol': symbol,
                'timeframe': timeframe,
                'initial_balance': initial_balance,
                'final_balance': round(balance, 2),
                'total_return_pct': round(total_return_pct, 2),
                'total_trades': 0,
                'win_rate': 0,
                'profit_factor': 0,
                'max_drawdown': 0,
                'sharpe_ratio': 0,
                'expectancy': 0,
                'trades': [],
                'equity_values': equity_values,
                'equity_timestamps': [ts.isoformat() for ts in equity_timestamps],
                'signal_diagnostics': signal_count
            }

        pnls = [t['pnl'] for t in trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]

        win_rate = (len(wins) / len(trades)) * 100
        gross_profit = sum(wins) if wins else 0
        gross_loss = abs(sum(losses)) if losses else 1
        profit_factor = gross_profit / gross_loss

        # Max Drawdown
        peak = initial_balance
        max_drawdown = 0
        for equity in equity_values:
            if equity > peak:
                peak = equity
            drawdown = ((peak - equity) / peak) * 100 if peak > 0 else 0
            if drawdown > max_drawdown:
                max_drawdown = drawdown

        # Sharpe Ratio
        returns = []
        for i in range(1, len(equity_values)):
            if equity_values[i - 1] > 0:
                returns.append((equity_values[i] - equity_values[i - 1]) / equity_values[i - 1])

        if returns and len(returns) > 1:
            sharpe_ratio = (np.mean(returns) / np.std(returns)) * np.sqrt(252) if np.std(returns) > 0 else 0
        else:
            sharpe_ratio = 0

        total_return_pct = ((balance - initial_balance) / initial_balance) * 100 if initial_balance > 0 else 0
        expectancy = sum(pnls) / len(trades) if trades else 0

        logger.info(
            f"Бектест завершено: {len(trades)} угод, Win Rate: {win_rate:.1f}%, Return: {total_return_pct:.1f}%")

        return {
            'symbol': symbol,
            'timeframe': timeframe,
            'initial_balance': initial_balance,
            'final_balance': round(balance, 2),
            'total_return_pct': round(total_return_pct, 2),
            'total_trades': len(trades),
            'win_trades': len(wins),
            'loss_trades': len(losses),
            'win_rate': round(win_rate, 2),
            'total_pnl': round(sum(pnls), 2),
            'gross_profit': round(gross_profit, 2),
            'gross_loss': round(gross_loss, 2),
            'profit_factor': round(profit_factor, 2),
            'max_drawdown': round(max_drawdown, 2),
            'sharpe_ratio': round(sharpe_ratio, 3),
            'expectancy': round(expectancy, 2),
            'trades': trades,
            'equity_values': equity_values,
            'equity_timestamps': [ts.isoformat() for ts in equity_timestamps],
            'signal_diagnostics': signal_count
        }