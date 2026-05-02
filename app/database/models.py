from sqlalchemy import Column, Integer, String, Float, DateTime, Boolean, Text, Enum, ForeignKey
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.sql import func
import enum

Base = declarative_base()


class OrderStatus(enum.Enum):
    OPEN = "open"
    CLOSED = "closed"
    CANCELLED = "cancelled"


class OrderType(enum.Enum):
    MARKET = "market"
    LIMIT = "limit"


class Side(enum.Enum):
    BUY = "buy"
    SELL = "sell"


class PredictionStatus(enum.Enum):
    PENDING = "pending"
    SUCCESS = "success"
    FAILED = "failed"


class WalletSim(Base):
    __tablename__ = 'wallets_sim'

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, default=1)
    balance = Column(Float, default=0.0)
    last_updated = Column(DateTime, server_default=func.now(), onupdate=func.now())


class WalletReal(Base):
    __tablename__ = 'wallets_real'

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, default=1)
    balance = Column(Float, default=0.0)
    last_updated = Column(DateTime, server_default=func.now(), onupdate=func.now())


class OrderSim(Base):
    __tablename__ = 'orders_sim'

    id = Column(Integer, primary_key=True)
    symbol = Column(String(20), nullable=False)
    side = Column(Enum(Side), nullable=False)
    order_type = Column(Enum(OrderType), nullable=False)
    quantity = Column(Float, nullable=False)
    price = Column(Float, nullable=False)
    status = Column(Enum(OrderStatus), default=OrderStatus.OPEN)
    opened_at = Column(DateTime, server_default=func.now())
    closed_at = Column(DateTime, nullable=True)
    pnl = Column(Float, default=0.0)
    fee = Column(Float, default=0.0)
    prediction_id = Column(Integer, ForeignKey('predictions.id'), nullable=True)


class OrderReal(Base):
    __tablename__ = 'orders_real'

    id = Column(Integer, primary_key=True)
    real_order_id = Column(String(50), nullable=True)
    symbol = Column(String(20), nullable=False)
    side = Column(Enum(Side), nullable=False)
    order_type = Column(Enum(OrderType), nullable=False)
    quantity = Column(Float, nullable=False)
    price = Column(Float, nullable=False)
    status = Column(Enum(OrderStatus), default=OrderStatus.OPEN)
    opened_at = Column(DateTime, server_default=func.now())
    closed_at = Column(DateTime, nullable=True)
    pnl = Column(Float, default=0.0)
    fee = Column(Float, default=0.0)
    prediction_id = Column(Integer, ForeignKey('predictions.id'), nullable=True)


class Prediction(Base):
    __tablename__ = 'predictions'

    id = Column(Integer, primary_key=True)
    symbol = Column(String(20), nullable=False)
    timeframe = Column(String(10), nullable=False)
    direction = Column(Enum(Side), nullable=False)  # buy=long, sell=short
    predicted_at = Column(DateTime, server_default=func.now())
    target_price = Column(Float, nullable=False)
    stop_loss = Column(Float, nullable=True)
    check_at = Column(DateTime, nullable=False)
    validated_at = Column(DateTime, nullable=True)
    status = Column(Enum(PredictionStatus), default=PredictionStatus.PENDING)
    actual_price = Column(Float, nullable=True)


class PriceHistory(Base):
    __tablename__ = 'price_history'

    id = Column(Integer, primary_key=True)
    symbol = Column(String(20), nullable=False)
    timestamp = Column(DateTime, nullable=False)
    open = Column(Float, nullable=False)
    high = Column(Float, nullable=False)
    low = Column(Float, nullable=False)
    close = Column(Float, nullable=False)
    volume = Column(Float, nullable=True)


class TradeLog(Base):
    __tablename__ = 'trades_log'

    id = Column(Integer, primary_key=True)
    timestamp = Column(DateTime, server_default=func.now())
    action = Column(String(50), nullable=False)
    details = Column(Text, nullable=True)
    status = Column(String(20), nullable=False)


class StrategyState(Base):
    __tablename__ = 'strategy_state'

    id = Column(Integer, primary_key=True)
    is_running = Column(Boolean, default=False)
    mode = Column(String(20), default='simulation')  # 'simulation' or 'real'
    last_run = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())