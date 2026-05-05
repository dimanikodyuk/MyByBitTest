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

        if current_price <= 0:
            logger.error(f"❌ Некорректная цена для {pair}: {current_price}")
            return None

        balance = self.db.get_balance("USDT", is_paper=True)
        required = quantity * current_price

        if balance < required:
            logger.warning(f"Insufficient balance: {balance} < {required}")
            return None

        # Розрахунок slippage та spread
        slippage_amount = current_price * (self.slippage / 100)
        spread_amount = current_price * (self.spread / 100)
        execution_price = current_price + slippage_amount + spread_amount

        # Комісія
        commission_amount = required * (self.commission / 100)

        # Віднімаємо вартість позиції + комісію
        new_balance = balance - required - commission_amount
        self.db.update_balance("USDT", new_balance, is_paper=True)

        # Створюємо угоду
        trade_data = {
            "pair": pair,
            "side": OrderSide.BUY,
            "entry_price": execution_price,  # ← має бути execution_price, а не 0
            "quantity": quantity,
            "commission": commission_amount,
            "slippage": slippage_amount,
            "is_paper": 1,
            "status": OrderStatus.PENDING
        }

        logger.info(f"📝 Створення угоди: {trade_data}")

        trade = self.db.create_trade(trade_data)

        logger.info(f"[PAPER] BUY {quantity} {pair} @ {execution_price:.2f} | "
                    f"Commission: {commission_amount:.4f} | Free Balance: {new_balance:.2f}")

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

        # Розрахунок slippage для sell
        slippage_amount = current_price * (self.slippage / 100)
        spread_amount = current_price * (self.spread / 100)
        execution_price = current_price - slippage_amount - spread_amount

        # Комісія на закриття
        close_commission = (execution_price * trade.quantity) * (self.commission / 100)

        # Розрахунок PnL (без комісій — вони окремо)
        if trade.side == OrderSide.BUY:
            pnl = (execution_price - trade.entry_price) * trade.quantity - close_commission
            pnl_percent = ((execution_price - trade.entry_price) / trade.entry_price) * 100
            # Повертаємо: вартість продажу мінус комісія закриття
            return_amount = execution_price * trade.quantity - close_commission
        else:  # SHORT
            pnl = (trade.entry_price - execution_price) * trade.quantity - close_commission
            pnl_percent = ((trade.entry_price - execution_price) / trade.entry_price) * 100
            # Для SHORT повертаємо: заблокована сума + прибуток (або - збиток) мінус комісія
            return_amount = trade.entry_price * trade.quantity + pnl

        # Оновлюємо баланс
        balance = self.db.get_balance("USDT", is_paper=True)
        new_balance = balance + return_amount
        self.db.update_balance("USDT", new_balance, is_paper=True)

        updates = {
            "exit_price": execution_price,
            "pnl": round(pnl, 6),
            "pnl_percent": round(pnl_percent, 4),
            "commission": round(trade.commission + close_commission, 6),
            "status": OrderStatus.CLOSED,
            "closed_at": datetime.now()
        }
        self.db.update_trade(trade_id, updates)

        logger.info(f"[PAPER] CLOSE {trade.quantity} {trade.pair} @ {execution_price:.2f} | "
                    f"PnL: {pnl:.4f} ({pnl_percent:.2f}%) | Commission: {close_commission:.4f} | Balance: {new_balance:.2f}")

        return {
            "trade_id": trade_id,
            "execution_price": execution_price,
            "pnl": pnl,
            "pnl_percent": pnl_percent
        }

    def execute_short(self, pair: str, quantity: float, current_price: float) -> Optional[Dict]:
        """Виконання short ордера (paper)"""

        if current_price <= 0:
            logger.error(f"❌ Некорректная цена для {pair}: {current_price}")
            return None

        balance = self.db.get_balance("USDT", is_paper=True)
        position_value = quantity * current_price

        if balance < position_value:
            logger.warning(f"Insufficient balance for short: {balance} < {position_value}")
            return None

        # Slippage та spread
        slippage_amount = current_price * (self.slippage / 100)
        spread_amount = current_price * (self.spread / 100)
        execution_price = current_price - slippage_amount - spread_amount

        # Комісія
        commission_amount = position_value * (self.commission / 100)

        # Блокуємо повну вартість позиції
        new_balance = balance - position_value - commission_amount
        self.db.update_balance("USDT", new_balance, is_paper=True)

        trade_data = {
            "pair": pair,
            "side": OrderSide.SELL,
            "entry_price": execution_price,  # ← має бути execution_price, а не 0
            "quantity": quantity,
            "commission": commission_amount,
            "slippage": slippage_amount,
            "is_paper": 1,
            "status": OrderStatus.PENDING
        }

        logger.info(f"📝 Створення SHORT угоди: {trade_data}")

        trade = self.db.create_trade(trade_data)

        logger.info(f"[PAPER] SHORT {quantity} {pair} @ {execution_price:.2f} | "
                    f"Position: ${position_value:.2f} | Commission: {commission_amount:.4f}")

        return {
            "trade_id": trade.id,
            "execution_price": execution_price,
            "quantity": quantity,
            "position_value": position_value,
            "commission": commission_amount
        }

    def reset(self):
        """Скидання paper trading"""
        initial_balance = config.get('paper_trading.initial_balance', 100.0)
        self.db.reset_paper_balance(initial_balance)
        logger.info(f"Paper trading reset to {initial_balance} USDT")

    def get_open_positions(self) -> List[Trade]:
        """Отримати відкриті позиції"""
        return self.db.get_open_trades(is_paper=True)