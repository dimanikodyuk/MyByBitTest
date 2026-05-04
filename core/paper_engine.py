from typing import Dict, Optional, List
from datetime import datetime
from db.operations import DatabaseOperations
from db.models import OrderSide, OrderStatus, Trade, OrderType
from utils.config_loader import config
from utils.logger import logger
import random


class PaperEngine:
    """Віртуальне виконання угод (Paper Trading)"""

    def __init__(self, db_ops: DatabaseOperations):
        self.db = db_ops
        self.commission = config.get('paper_trading.commission_percent', 0.1)
        self.slippage = config.get('paper_trading.slippage_percent', 0.1)
        self.spread = config.get('paper_trading.spread_percent', 0.05)

    def execute_buy(self, pair: str, quantity: float, current_price: float) -> Optional[Dict]:
        """Виконання buy ордера (paper)"""

        # Перевірка балансу
        balance = self.db.get_balance("USDT", is_paper=True)
        required = quantity * current_price

        if balance < required:
            logger.warning(f"Insufficient balance: {balance} < {required}")
            return None

        # Розрахунок slippage та spread
        slippage_amount = current_price * (self.slippage / 100)
        spread_amount = current_price * (self.spread / 100)

        # Фактична ціна виконання (для buy - трохи вище)
        execution_price = current_price + slippage_amount + spread_amount

        # Комісія
        commission_amount = required * (self.commission / 100)

        # Блокуємо кошти
        new_balance = balance - required - commission_amount
        self.db.update_balance("USDT", new_balance, is_paper=True)

        # Створюємо угоду
        trade_data = {
            "pair": pair,
            "side": OrderSide.BUY,
            "entry_price": execution_price,
            "quantity": quantity,
            "commission": commission_amount,
            "slippage": slippage_amount,
            "is_paper": 1,
            "status": OrderStatus.PENDING
        }

        trade = self.db.create_trade(trade_data)

        logger.info(
            f"[PAPER] BUY {quantity} {pair} @ {execution_price:.2f} | Commission: {commission_amount:.4f} | Balance: {new_balance:.2f}")

        return {
            "trade_id": trade.id,
            "execution_price": execution_price,
            "quantity": quantity,
            "commission": commission_amount
        }

    def execute_sell(self, trade_id: int, current_price: float) -> Optional[Dict]:
        """Закриття угоди (sell)"""

        trade = self.db.get_trade_by_id(trade_id)
        if not trade or trade.status != OrderStatus.PENDING:
            logger.error(f"Trade {trade_id} not found or already closed")
            return None

        # Розрахунок slippage для sell (ціна трохи нижче)
        slippage_amount = current_price * (self.slippage / 100)
        spread_amount = current_price * (self.spread / 100)
        execution_price = current_price - slippage_amount - spread_amount

        # Розрахунок PnL
        if trade.side == OrderSide.BUY:
            pnl = (execution_price - trade.entry_price) * trade.quantity
            pnl_percent = ((execution_price - trade.entry_price) / trade.entry_price) * 100
        else:  # SELL (short)
            pnl = (trade.entry_price - execution_price) * trade.quantity
            pnl_percent = ((trade.entry_price - execution_price) / trade.entry_price) * 100

        # Комісія на закриття
        close_commission = (execution_price * trade.quantity) * (self.commission / 100)

        # Повертаємо кошти + прибуток
        balance = self.db.get_balance("USDT", is_paper=True)
        return_amount = (execution_price * trade.quantity) - close_commission

        if trade.side == OrderSide.BUY:
            new_balance = balance + return_amount
        else:
            # Для short - повертаємо заставу + прибуток
            new_balance = balance + return_amount

        self.db.update_balance("USDT", new_balance, is_paper=True)

        # Оновлюємо угоду
        updates = {
            "exit_price": execution_price,
            "pnl": pnl,
            "pnl_percent": pnl_percent,
            "commission": trade.commission + close_commission,
            "status": OrderStatus.CLOSED,
            "closed_at": datetime.now()
        }
        self.db.update_trade(trade_id, updates)

        logger.info(
            f"[PAPER] SELL {trade.quantity} {trade.pair} @ {execution_price:.2f} | PnL: {pnl:.2f} ({pnl_percent:.2f}%) | Balance: {new_balance:.2f}")

        return {
            "trade_id": trade_id,
            "execution_price": execution_price,
            "pnl": pnl,
            "pnl_percent": pnl_percent
        }

    def execute_short(self, pair: str, quantity: float, current_price: float) -> Optional[Dict]:
        """Виконання short ордера (paper)"""

        # Для short - перевірка балансу (потрібна маржа)
        balance = self.db.get_balance("USDT", is_paper=True)
        margin_required = quantity * current_price * 0.1  # 10% маржа для прикладу

        if balance < margin_required:
            logger.warning(f"Insufficient margin for short: {balance} < {margin_required}")
            return None

        # Slippage та spread
        slippage_amount = current_price * (self.slippage / 100)
        spread_amount = current_price * (self.spread / 100)
        execution_price = current_price - slippage_amount - spread_amount  # Для short продаємо дешевше

        # Блокуємо маржу
        new_balance = balance - margin_required
        self.db.update_balance("USDT", new_balance, is_paper=True)

        trade_data = {
            "pair": pair,
            "side": OrderSide.SELL,
            "entry_price": execution_price,
            "quantity": quantity,
            "commission": 0,
            "slippage": slippage_amount,
            "is_paper": 1,
            "status": OrderStatus.PENDING
        }

        trade = self.db.create_trade(trade_data)

        logger.info(
            f"[PAPER] SHORT {quantity} {pair} @ {execution_price:.2f} | Margin: {margin_required:.2f} | Balance: {new_balance:.2f}")

        return {
            "trade_id": trade.id,
            "execution_price": execution_price,
            "quantity": quantity,
            "margin": margin_required
        }

    def reset(self):
        """Скидання paper trading"""
        initial_balance = config.get('paper_trading.initial_balance', 100.0)
        self.db.reset_paper_balance(initial_balance)
        logger.info(f"Paper trading reset to {initial_balance} USDT")

    def get_open_positions(self) -> List[Trade]:
        """Отримати відкриті позиції"""
        return self.db.get_open_trades(is_paper=True)