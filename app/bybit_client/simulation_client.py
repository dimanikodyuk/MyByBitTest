from app.bybit_client.real_client import ByBitRealClient
from app.database.db_manager import db
from app.config import config
from loguru import logger
from typing import Dict, Optional
from datetime import datetime


class SimulationClient:
    def __init__(self):
        self.real_client = ByBitRealClient()  # For price data only
        logger.info("Simulation client initialized")

    async def get_klines(self, symbol: str, interval: str, limit: int = 100):
        """Get real klines for simulation"""
        return await self.real_client.get_klines(symbol, interval, limit)

    async def get_current_price(self, symbol: str) -> Optional[float]:
        """Get real current price for simulation"""
        return await self.real_client.get_current_price(symbol)

    async def place_order(self, symbol: str, side: str, order_type: str, quantity: float, price: float = None,
                          prediction_id: int = None) -> Optional[Dict]:
        """Place virtual order"""
        try:
            current_price = await self.get_current_price(symbol)
            if not current_price:
                logger.error("Cannot get current price")
                return None

            order_price = price if order_type.lower() == 'limit' and price else current_price

            # Check balance
            balance = await db.get_simulation_balance()
            cost = order_price * quantity

            if side.lower() == 'buy' and cost > balance:
                logger.warning(f"Insufficient balance: need ${cost}, have ${balance}")
                return None

            # Create order in database
            order_data = {
                'symbol': symbol,
                'side': side.lower(),
                'order_type': order_type.lower(),
                'quantity': quantity,
                'price': order_price,
                'status': 'open',
                'prediction_id': prediction_id
            }

            order_id = await db.create_order_sim(order_data)

            # Update balance for buy orders
            if side.lower() == 'buy':
                new_balance = balance - cost
                await db.update_simulation_balance(new_balance)

            # Calculate fee (taker fee for market, maker fee for limit)
            fee_rate = config.FEE_TAKER if order_type.lower() == 'market' else config.FEE_MAKER
            fee = cost * fee_rate

            await db.add_log(
                action=f"SIM_{side.upper()}",
                details=f"{quantity} {symbol} at ${order_price}, fee: ${fee:.4f}",
                status="success"
            )

            logger.success(f"Simulation order #{order_id}: {side} {quantity} {symbol} @ ${order_price}")

            return {'orderId': str(order_id), 'price': order_price}

        except Exception as e:
            logger.error(f"Simulation order failed: {e}")
            await db.add_log("SIM_ORDER_FAILED", str(e), "error")
            return None

    async def close_order(self, order_id: int, current_price: float):
        """Close virtual order and calculate PnL"""
        try:
            orders = await db.get_open_orders_sim()
            order = next((o for o in orders if o.id == order_id), None)

            if not order:
                logger.error(f"Order {order_id} not found")
                return False

            # Calculate PnL
            if order.side.value == 'buy':
                pnl = (current_price - order.price) * order.quantity
            else:  # sell
                pnl = (order.price - current_price) * order.quantity

            # Calculate fee for closing (taker fee)
            close_value = current_price * order.quantity
            fee = close_value * config.FEE_TAKER

            pnl_after_fee = pnl - fee

            # Update balance (only for buy orders, sell orders would have increased balance on open)
            balance = await db.get_simulation_balance()
            if order.side.value == 'buy':
                new_balance = balance + (current_price * order.quantity) - fee
            else:
                # For sell orders, we already have the money from the sale
                # We need to track this properly - simplified for now
                new_balance = balance + pnl - fee

            await db.update_simulation_balance(new_balance)
            await db.close_order_sim(order_id, current_price, pnl_after_fee, fee)

            await db.add_log(
                action=f"SIM_CLOSE_{order.side.value.upper()}",
                details=f"Order #{order_id}: PnL = ${pnl_after_fee:.2f}",
                status="success"
            )

            logger.success(f"Simulation order #{order_id} closed. PnL: ${pnl_after_fee:.2f}")
            return True

        except Exception as e:
            logger.error(f"Error closing order: {e}")
            return False


simulation_client = SimulationClient()