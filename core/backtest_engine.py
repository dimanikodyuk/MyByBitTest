import pandas as pd
import numpy as np
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
    entry_time: datetime
    exit_time: Optional[datetime]
    side: str  # LONG / SHORT
    entry_price: float
    exit_price: Optional[float]
    quantity: float
    pnl: float
    pnl_percent: float


class BacktestEngine:
    """Двигун для бектестування стратегій"""

    def __init__(self, initial_balance: float = 1000.0, commission: float = 0.001):
        self.initial_balance = initial_balance
        self.balance = initial_balance
        self.commission = commission
        self.trades: List[BacktestTrade] = []
        self.equity_curve: List[float] = [initial_balance]
        self.timestamps: List[datetime] = []
        self.current_position = None  # {'side', 'entry_price', 'quantity', 'entry_time'}

    def reset(self):
        """Скидання стану"""
        self.balance = self.initial_balance
        self.trades = []
        self.equity_curve = [self.initial_balance]
        self.timestamps = []
        self.current_position = None

    def calculate_position_size(self, price: float, risk_percent: float = 2.0) -> float:
        """Розрахунок розміру позиції"""
        risk_amount = self.balance * (risk_percent / 100)
        # Для бектесту використовуємо фіксований розмір 20% балансу
        position_value = self.balance * 0.2
        quantity = position_value / price
        return round(quantity, 6)

    def execute_buy(self, timestamp: datetime, price: float, quantity: float = None) -> bool:
        """Виконання buy угоди"""
        if self.current_position is not None:
            logger.warning(f"Cannot buy - position already open at {timestamp}")
            return False

        if quantity is None:
            quantity = self.calculate_position_size(price)

        required = quantity * price
        if required > self.balance:
            quantity = self.balance / price * 0.95
            required = quantity * price

        self.current_position = {
            'side': 'LONG',
            'entry_price': price,
            'quantity': quantity,
            'entry_time': timestamp,
            'balance_before': self.balance
        }

        # Блокуємо кошти
        self.balance -= required
        return True

    def execute_short(self, timestamp: datetime, price: float, quantity: float = None) -> bool:
        """Виконання short угоди"""
        if self.current_position is not None:
            logger.warning(f"Cannot short - position already open at {timestamp}")
            return False

        if quantity is None:
            quantity = self.calculate_position_size(price)

        position_value = quantity * price
        if position_value > self.balance:
            quantity = self.balance / price * 0.95

        self.current_position = {
            'side': 'SHORT',
            'entry_price': price,
            'quantity': quantity,
            'entry_time': timestamp,
            'balance_before': self.balance
        }

        # Блокуємо заставу
        self.balance -= quantity * price
        return True

    def execute_sell(self, timestamp: datetime, price: float) -> Optional[BacktestTrade]:
        """Закриття угоди"""
        if self.current_position is None:
            return None

        position = self.current_position
        quantity = position['quantity']
        entry_price = position['entry_price']

        if position['side'] == 'LONG':
            pnl = (price - entry_price) * quantity
            pnl_percent = ((price - entry_price) / entry_price) * 100
            # Повертаємо кошти + прибуток
            self.balance += quantity * price
        else:  # SHORT
            pnl = (entry_price - price) * quantity
            pnl_percent = ((entry_price - price) / entry_price) * 100
            self.balance += quantity * price  # Повертаємо заставу + прибуток

        # Віднімаємо комісію
        commission_amount = (quantity * price) * self.commission
        self.balance -= commission_amount
        pnl -= commission_amount

        trade = BacktestTrade(
            entry_time=position['entry_time'],
            exit_time=timestamp,
            side=position['side'],
            entry_price=entry_price,
            exit_price=price,
            quantity=quantity,
            pnl=pnl,
            pnl_percent=pnl_percent
        )
        self.trades.append(trade)
        self.current_position = None
        self.equity_curve.append(self.balance)
        self.timestamps.append(timestamp)

        return trade

    def get_current_pnl(self, current_price: float) -> float:
        """Поточний PnL відкритої позиції"""
        if self.current_position is None:
            return 0

        if self.current_position['side'] == 'LONG':
            return (current_price - self.current_position['entry_price']) * self.current_position['quantity']
        else:
            return (self.current_position['entry_price'] - current_price) * self.current_position['quantity']

    def get_stats(self) -> Dict:
        """Отримання статистики бектесту"""
        if not self.trades:
            return {
                'total_trades': 0,
                'win_rate': 0,
                'total_pnl': 0,
                'profit_factor': 0,
                'max_drawdown': 0,
                'sharpe_ratio': 0,
                'expectancy': 0,
                'final_balance': self.balance,
                'total_return_pct': ((self.balance - self.initial_balance) / self.initial_balance) * 100
            }

        pnls = [t.pnl for t in self.trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]

        win_rate = len(wins) / len(self.trades) * 100
        total_pnl = sum(pnls)
        gross_profit = sum(wins) if wins else 0
        gross_loss = abs(sum(losses)) if losses else 1
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else 0

        # Max Drawdown
        peak = self.initial_balance
        max_drawdown = 0
        for equity in self.equity_curve:
            if equity > peak:
                peak = equity
            drawdown = (peak - equity) / peak * 100
            if drawdown > max_drawdown:
                max_drawdown = drawdown

        # Sharpe Ratio (приблизний, без ризикової ставки)
        returns = [self.equity_curve[i] / self.equity_curve[i - 1] - 1 for i in range(1, len(self.equity_curve))]
        if returns and len(returns) > 1:
            avg_return = np.mean(returns)
            std_return = np.std(returns)
            sharpe_ratio = (avg_return / std_return) * np.sqrt(252) if std_return > 0 else 0
        else:
            sharpe_ratio = 0

        expectancy = total_pnl / len(self.trades) if self.trades else 0

        return {
            'total_trades': len(self.trades),
            'win_trades': len(wins),
            'loss_trades': len(losses),
            'win_rate': round(win_rate, 2),
            'total_pnl': round(total_pnl, 2),
            'gross_profit': round(gross_profit, 2),
            'gross_loss': round(gross_loss, 2),
            'profit_factor': round(profit_factor, 2),
            'max_drawdown': round(max_drawdown, 2),
            'sharpe_ratio': round(sharpe_ratio, 3),
            'expectancy': round(expectancy, 2),
            'final_balance': round(self.balance, 2),
            'total_return_pct': round(((self.balance - self.initial_balance) / self.initial_balance) * 100, 2)
        }


class Backtester:
    """Головний клас бектестування стратегій"""

    def __init__(self):
        self.exchange = BybitClient()
        self.engine = BacktestEngine()

    def get_historical_data(self, symbol: str, timeframe: str, start_date: datetime,
                            end_date: datetime) -> pd.DataFrame:
        """Отримання історичних даних"""
        logger.info(f"Завантаження даних для {symbol} {timeframe} з {start_date} по {end_date}")

        # Bybit обмежує 200 свічок за раз, тому завантажуємо частинами
        all_data = []
        current_start = start_date
        limit = 200

        while current_start < end_date:
            df = self.exchange.get_klines(symbol, timeframe, limit=limit)
            if df is None or df.empty:
                break

            # Фільтруємо по даті
            df = df[df['timestamp'] >= current_start]
            if not df.empty:
                all_data.append(df)
                current_start = df['timestamp'].iloc[-1] + pd.Timedelta(minutes=1)
            else:
                break

            # Невелика затримка, щоб не перевантажувати API
            import time
            time.sleep(0.1)

        if not all_data:
            return pd.DataFrame()

        result = pd.concat(all_data, ignore_index=True)
        result = result[result['timestamp'] <= end_date]
        return result

    def run_backtest(self, symbol: str, timeframe: str, start_date: datetime, end_date: datetime,
                     strategy_func=None, **strategy_params) -> Dict:
        """
        Запуск бектесту

        Args:
            symbol: торгова пара
            timeframe: таймфрейм (1m, 5m, 15m, 1h)
            start_date: дата початку
            end_date: дата кінця
            strategy_func: функція стратегії (приймає df, повертає список сигналів)
            **strategy_params: параметри стратегії
        """
        self.engine.reset()

        # Отримуємо дані
        df = self.get_historical_data(symbol, timeframe, start_date, end_date)
        if df.empty:
            return {'error': 'Немає даних для вказаного періоду'}

        logger.info(f"Отримано {len(df)} свічок для бектесту")

        # Розраховуємо індикатори
        strategy = TradingStrategy(symbol)
        df = strategy.calculate_indicators(df)

        # Симуляція торгівлі
        for i in range(50, len(df)):  # Починаємо з 50 свічки, щоб індикатори сформувались
            current = df.iloc[i]
            timestamp = current['timestamp']
            price = current['close']

            # Перевіряємо сигнали
            if self.engine.current_position is None:
        # Н