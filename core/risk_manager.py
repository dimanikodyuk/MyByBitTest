from typing import Dict, List, Optional
from datetime import datetime, date
from db.operations import DatabaseOperations
from utils.config_loader import config
from utils.logger import logger


class RiskManager:
    """Управління ризиками"""

    def __init__(self, db_ops: DatabaseOperations, is_paper: bool = True):
        self.db = db_ops
        self.is_paper = is_paper
        self.risk_per_trade = config.get('risk.risk_per_trade', 2.0)
        self.max_open_trades = config.get('risk.max_open_trades', 3)
        self.max_daily_loss = config.get('risk.max_daily_loss', 5.0)
        self.min_balance = config.get('risk.min_balance_usdt', 10.0)

        self.daily_loss_reached = False
        self.today = date.today()

    def can_open_trade(self, pair: str) -> tuple[bool, str]:
        """Перевірка, чи можна відкрити нову угоду"""

        # Перевірка денного ліміту
        if self.daily_loss_reached:
            return False, "Daily loss limit reached"

        # ПЕРЕВІРКА: чи вже є відкрита позиція по цій парі
        existing_trades = self.db.get_open_trades(pair=pair, is_paper=self.is_paper)
        if existing_trades:
            return False, f"Вже є відкрита позиція по {pair}"

        # Перевірка кількості відкритих угод
        open_trades = self.db.get_open_trades(pair=None, is_paper=self.is_paper)
        if len(open_trades) >= self.max_open_trades:
            return False, f"Max open trades reached ({self.max_open_trades})"

        # Перевірка балансу
        balance = self.db.get_balance("USDT", self.is_paper)
        if balance < self.min_balance:
            return False, f"Balance below minimum ({self.min_balance} USDT)"

        # Перевірка денного збитку
        daily_pnl = self.db.get_daily_pnl(self.is_paper)
        if abs(daily_pnl) > (balance * self.max_daily_loss / 100):
            self.daily_loss_reached = True
            logger.warning(f"Daily loss limit reached: {daily_pnl} USDT")
            return False, f"Daily loss limit reached ({self.max_daily_loss}%)"

        return True, "OK"

    def calculate_position_size(self, balance: float, entry_price: float, stop_loss_price: float,
                                pair: str = "") -> float:
        """Розрахунок розміру позиції на основі ризику"""
        if entry_price <= 0 or stop_loss_price <= 0:
            return 0

        # Ризик в USDT
        risk_amount = balance * (self.risk_per_trade / 100)

        # Ризик на одиницю
        risk_per_unit = abs(entry_price - stop_loss_price)

        if risk_per_unit <= 0:
            return 0

        # Кількість одиниць
        quantity = risk_amount / risk_per_unit

        # Мінімальний об'єм для Bybit
        min_qty = 0.001
        if quantity < min_qty:
            quantity = min_qty

        quantity = round(quantity, 6)  # Збільшимо точність

        # Перевірка балансу
        required_balance = quantity * entry_price
        if required_balance > balance:
            quantity = balance / entry_price
            quantity = round(quantity, 6)

        # Максимальний розмір позиції - не більше 25% балансу
        max_position_value = balance * 0.25
        max_quantity = max_position_value / entry_price
        if quantity > max_quantity:
            quantity = max_quantity
            quantity = round(quantity, 6)

        logger.debug(f"📐 Розмір позиції: {quantity} (ризик={self.risk_per_trade}%, баланс={balance:.2f})")
        return quantity

    def update_daily_check(self):
        """Оновлення денної перевірки (при зміні дня)"""
        today = date.today()
        if today != self.today:
            self.today = today
            self.daily_loss_reached = False
            logger.info("Daily limit reset for new day")

    def get_daily_stats(self) -> Dict:
        """Отримати денну статистику"""
        daily_pnl = self.db.get_daily_pnl(self.is_paper)
        balance = self.db.get_balance("USDT", self.is_paper)

        return {
            "daily_pnl": daily_pnl,
            "daily_pnl_percent": (daily_pnl / balance * 100) if balance > 0 else 0,
            "daily_limit": self.max_daily_loss,
            "limit_reached": self.daily_loss_reached,
            "open_trades": len(self.db.get_open_trades(is_paper=self.is_paper))
        }

    def calculate_kelly_position(self, balance: float, entry_price: float, stop_loss_price: float) -> float:
        """Розрахунок розміру позиції за Kelly Criterion"""
        stats = self.db.get_stats(is_paper=self.is_paper)

        if stats['total_trades'] < config.get('risk.kelly_window', 50):
            return self.calculate_position_size(balance, entry_price, stop_loss_price)

        trades = self.db.get_trades_history(limit=config.get('risk.kelly_window', 50), is_paper=self.is_paper)

        wins = [t.pnl for t in trades if t.pnl > 0]
        losses = [abs(t.pnl) for t in trades if t.pnl < 0]

        if not wins or not losses:
            return self.calculate_position_size(balance, entry_price, stop_loss_price)

        win_rate = len(wins) / len(trades) * 100
        avg_win = sum(wins) / len(wins)
        avg_loss = sum(losses) / len(losses)

        if avg_loss == 0:
            return 0.1 * balance / entry_price

        b = avg_win / avg_loss
        p = win_rate / 100
        q = 1 - p

        kelly = (p * b - q) / b
        kelly = max(0, min(kelly, 0.25))

        position_value = balance * kelly
        quantity = position_value / entry_price

        return round(quantity, 6)