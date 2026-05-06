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

    def _get_min_quantity(self, pair: str) -> float:
        """Мінімальна кількість для пари на Bybit Spot"""
        min_qty_map = {
            'BTCUSDT': 0.0001,
            'ETHUSDT': 0.001,
            'SOLUSDT': 0.01,
            'BNBUSDT': 0.001,
            'XRPUSDT': 0.1,
            'DOGEUSDT': 1.0,
            'ADAUSDT': 1.0,
            'AVAXUSDT': 0.01,
            'DOTUSDT': 0.01,
            'LINKUSDT': 0.01,
            'MATICUSDT': 1.0,
            'UNIUSDT': 0.01,
            'ATOMUSDT': 0.01,
            'LTCUSDT': 0.001,
            'ETCUSDT': 0.01,
        }
        return min_qty_map.get(pair, 0.001)

    def _normalize_quantity(self, pair: str, quantity: float) -> float:
        """Нормалізація кількості відповідно до мінімуму"""
        min_qty = self._get_min_quantity(pair)
        if quantity < min_qty:
            quantity = min_qty
        return round(quantity, 6)

    def execute_buy(self, pair: str, quantity: float, current_price: float) -> Optional[Dict]:
        """Виконання реальної buy угоди з підтвердженням виконання"""
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

            # Нормалізація кількості
            quantity = self._normalize_quantity(pair, quantity)

            # Розміщення ордера
            order = self.exchange.place_order(
                symbol=pair,
                side="Buy",
                order_type="MARKET",
                qty=quantity
            )

            if not order:
                logger.error(f"Не вдалося розмістити ордер для {pair}")
                return None

            order_id = order.get('orderId')

            # Очікуємо підтвердження виконання
            if not self.exchange.wait_for_order_fill(pair, order_id, timeout=10):
                logger.error(f"Ордер {order_id} не виконався за 10 секунд")
                return None

            # Отримуємо реальну ціну виконання
            order_status = self.exchange.get_order_status(pair, order_id)
            executed_price = float(order_status.get('avgPrice', current_price)) if order_status else current_price

            trade_data = {
                "pair": pair,
                "side": OrderSide.BUY,
                "entry_price": executed_price,
                "quantity": quantity,
                "is_paper": 0,
                "status": OrderStatus.FILLED,
                "exchange_order_id": order_id
            }

            trade = self.db.create_trade(trade_data)
            logger.info(f"[REAL] BUY {quantity} {pair} @ {executed_price} (order {order_id})")

            return {"trade_id": trade.id, "execution_price": executed_price, "quantity": quantity}

        except Exception as e:
            logger.error(f"Помилка REAL BUY: {e}")
            return None

    def execute_sell(self, trade_id: int, current_price: float) -> Optional[Dict]:
        """Закриття реальної угоди з підтвердженням виконання"""
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

            if not order:
                logger.error(f"Не вдалося розмістити ордер на закриття для {trade_id}")
                return None

            order_id = order.get('orderId')

            # Очікуємо підтвердження виконання
            if not self.exchange.wait_for_order_fill(trade.pair, order_id, timeout=10):
                logger.error(f"Ордер на закриття {order_id} не виконався")
                return None

            # Отримуємо реальну ціну виконання
            order_status = self.exchange.get_order_status(trade.pair, order_id)
            executed_price = float(order_status.get('avgPrice', current_price)) if order_status else current_price

            if trade.side == OrderSide.BUY:
                pnl = (executed_price - trade.entry_price) * trade.quantity
                pnl_percent = ((executed_price - trade.entry_price) / trade.entry_price) * 100
            else:
                pnl = (trade.entry_price - executed_price) * trade.quantity
                pnl_percent = ((trade.entry_price - executed_price) / trade.entry_price) * 100

            updates = {
                "exit_price": executed_price,
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