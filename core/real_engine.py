"""
Real Trading Engine - реальне виконання угод через Bybit API
"""

from typing import Dict, Optional
from datetime import datetime
from db.operations import DatabaseOperations
from db.models import OrderSide, OrderStatus, Trade, OrderType
from exchange.bybit_client import BybitClient
from utils.config_loader import config
from utils.logger import logger


class RealEngine:
    """Реальне виконання угод через API Bybit"""

    def __init__(self, db_ops: DatabaseOperations):
        self.db = db_ops
        self.exchange = BybitClient()
        self.max_position_usdt = config.get('real_trading.max_position_usdt', 500)
        self.use_testnet = config.get('real_trading.use_testnet', False)

    def execute_buy(self, pair: str, quantity: float, current_price: float) -> Optional[Dict]:
        """Виконання реальної buy угоди"""
        try:
            # Перевірка балансу
            balance = self.exchange.get_balance("USDT")
            required = quantity * current_price

            if balance < required:
                logger.error(f"Недостатньо балансу: {balance} < {required}")
                return None

            if required > self.max_position_usdt:
                logger.warning(f"Позиція перевищує ліміт: {required} > {self.max_position_usdt}")
                quantity = self.max_position_usdt / current_price

            # Розміщення ордера
            order = self.exchange.place_order(
                symbol=pair,
                side="Buy",
                order_type="MARKET",
                qty=quantity
            )

            if order:
                trade_data = {
                    "pair": pair,
                    "side": OrderSide.BUY,
                    "entry_price": current_price,
                    "quantity": quantity,
                    "is_paper": 0,
                    "status": OrderStatus.PENDING,
                    "exchange_order_id": order.get('orderId')
                }

                trade = self.db.create_trade(trade_data)
                logger.info(f"[REAL] BUY {quantity} {pair} @ {current_price}")

                return {"trade_id": trade.id, "execution_price": current_price, "quantity": quantity}

        except Exception as e:
            logger.error(f"Помилка REAL BUY: {e}")

        return None

    def execute_sell(self, trade_id: int, current_price: float) -> Optional[Dict]:
        """Закриття реальної угоди"""
        try:
            trade = self.db.db.query(Trade).filter(Trade.id == trade_id).first()
            if not trade:
                return None

            order = self.exchange.place_order(
                symbol=trade.pair,
                side="Sell",
                order_type="MARKET",
                qty=trade.quantity
            )

            if order:
                if trade.side == OrderSide.BUY:
                    pnl = (current_price - trade.entry_price) * trade.quantity
                    pnl_percent = ((current_price - trade.entry_price) / trade.entry_price) * 100
                else:
                    pnl = (trade.entry_price - current_price) * trade.quantity
                    pnl_percent = ((trade.entry_price - current_price) / trade.entry_price) * 100

                updates = {
                    "exit_price": current_price,
                    "pnl": pnl,
                    "pnl_percent": pnl_percent,
                    "status": OrderStatus.CLOSED,
                    "closed_at": datetime.now()
                }
                self.db.update_trade(trade_id, updates)

                logger.info(f"[REAL] SELL {trade.quantity} {trade.pair} | PnL: {pnl:.2f}")

                return {"trade_id": trade_id, "pnl": pnl, "pnl_percent": pnl_percent}

        except Exception as e:
            logger.error(f"Помилка REAL SELL: {e}")

        return None