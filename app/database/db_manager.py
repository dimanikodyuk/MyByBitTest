from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import select, update, delete
from app.config import config
from app.database.models import Base, WalletSim, WalletReal, OrderSim, OrderReal, Prediction, PriceHistory, TradeLog, \
    StrategyState
from loguru import logger
from datetime import datetime
from typing import List, Optional


class DatabaseManager:
    def __init__(self):
        self.engine = create_async_engine(config.DATABASE_URL, echo=False)
        self.async_session = sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)

    async def init_db(self):
        """Initialize database tables"""
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.success(f"Database initialized: {config.DB_PATH}")

        # Initialize simulation wallet if not exists
        await self.init_simulation_wallet()
        await self.init_strategy_state()

    async def init_simulation_wallet(self):
        """Initialize simulation wallet with start balance"""
        async with self.async_session() as session:
            result = await session.execute(select(WalletSim).where(WalletSim.user_id == 1))
            wallet = result.scalar_one_or_none()

            if not wallet:
                wallet = WalletSim(user_id=1, balance=config.SIMULATION_START_BALANCE)
                session.add(wallet)
                await session.commit()
                logger.info(f"Simulation wallet initialized with ${config.SIMULATION_START_BALANCE}")

    async def init_strategy_state(self):
        """Initialize strategy state"""
        async with self.async_session() as session:
            result = await session.execute(select(StrategyState))
            state = result.scalar_one_or_none()

            if not state:
                state = StrategyState(is_running=False, mode='simulation')
                session.add(state)
                await session.commit()

    # Wallet operations
    async def get_simulation_balance(self) -> float:
        async with self.async_session() as session:
            result = await session.execute(select(WalletSim).where(WalletSim.user_id == 1))
            wallet = result.scalar_one()
            return wallet.balance

    async def update_simulation_balance(self, new_balance: float):
        async with self.async_session() as session:
            await session.execute(
                update(WalletSim).where(WalletSim.user_id == 1).values(balance=new_balance)
            )
            await session.commit()

    async def reset_simulation(self):
        """Reset simulation wallet and orders"""
        async with self.async_session() as session:
            # Reset wallet
            await session.execute(
                update(WalletSim).where(WalletSim.user_id == 1).values(balance=config.SIMULATION_START_BALANCE)
            )
            # Delete all simulation orders
            await session.execute(delete(OrderSim))
            await session.commit()
            logger.info("Simulation reset to $100")
            return True

    # Order operations
    async def create_order_sim(self, order_data: dict) -> int:
        async with self.async_session() as session:
            order = OrderSim(**order_data)
            session.add(order)
            await session.commit()
            await session.refresh(order)
            return order.id

    async def get_open_orders_sim(self) -> List[OrderSim]:
        async with self.async_session() as session:
            result = await session.execute(
                select(OrderSim).where(OrderSim.status == 'open')
            )
            return result.scalars().all()

    async def get_all_orders_sim(self) -> List[OrderSim]:
        async with self.async_session() as session:
            result = await session.execute(select(OrderSim))
            return result.scalars().all()

    async def close_order_sim(self, order_id: int, close_price: float, pnl: float, fee: float):
        async with self.async_session() as session:
            await session.execute(
                update(OrderSim)
                .where(OrderSim.id == order_id)
                .values(status='closed', closed_at=datetime.now(), pnl=pnl, fee=fee)
            )
            await session.commit()

    # Prediction operations
    async def create_prediction(self, pred_data: dict) -> int:
        async with self.async_session() as session:
            pred = Prediction(**pred_data)
            session.add(pred)
            await session.commit()
            await session.refresh(pred)
            return pred.id

    async def get_pending_predictions(self) -> List[Prediction]:
        async with self.async_session() as session:
            result = await session.execute(
                select(Prediction).where(Prediction.status == 'pending')
            )
            return result.scalars().all()

    async def get_all_predictions(self) -> List[Prediction]:
        async with self.async_session() as session:
            result = await session.execute(select(Prediction))
            return result.scalars().all()

    async def update_prediction_status(self, pred_id: int, status: str, actual_price: float = None):
        async with self.async_session() as session:
            await session.execute(
                update(Prediction)
                .where(Prediction.id == pred_id)
                .values(status=status, validated_at=datetime.now(), actual_price=actual_price)
            )
            await session.commit()

    # Price history
    async def save_price(self, symbol: str, price_data: dict):
        async with self.async_session() as session:
            price = PriceHistory(
                symbol=symbol,
                timestamp=datetime.fromtimestamp(price_data['timestamp'] / 1000),
                open=price_data['open'],
                high=price_data['high'],
                low=price_data['low'],
                close=price_data['close'],
                volume=price_data.get('volume')
            )
            session.add(price)
            await session.commit()

    # Logging
    async def add_log(self, action: str, details: str, status: str = 'success'):
        async with self.async_session() as session:
            log = TradeLog(action=action, details=details, status=status)
            session.add(log)
            await session.commit()

    async def get_logs(self, limit: int = 100) -> List[TradeLog]:
        async with self.async_session() as session:
            result = await session.execute(
                select(TradeLog).order_by(TradeLog.timestamp.desc()).limit(limit)
            )
            return result.scalars().all()

    # Strategy control
    async def get_strategy_state(self):
        async with self.async_session() as session:
            result = await session.execute(select(StrategyState))
            return result.scalar_one()

    async def set_strategy_running(self, is_running: bool):
        async with self.async_session() as session:
            await session.execute(
                update(StrategyState).values(is_running=is_running, last_run=datetime.now() if is_running else None)
            )
            await session.commit()

    async def set_strategy_mode(self, mode: str):
        async with self.async_session() as session:
            await session.execute(update(StrategyState).values(mode=mode))
            await session.commit()


db = DatabaseManager()