# db/models.py
from sqlalchemy import Column, Integer, String, Float, DateTime, Enum, Text, Index
from sqlalchemy.sql import func
from db.base import Base
import enum


class OrderSide(str, enum.Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, enum.Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class OrderStatus(str, enum.Enum):
    PENDING = "PENDING"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    CLOSED = "CLOSED"


class SignalType(str, enum.Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    EXIT_LONG = "EXIT_LONG"
    EXIT_SHORT = "EXIT_SHORT"
    HOLD = "HOLD"


class Trade(Base):
    __tablename__ = "trades"

    id = Column(Integer, primary_key=True, autoincrement=True)
    pair = Column(String(20), nullable=False)
    side = Column(Enum(OrderSide), nullable=False)
    entry_price = Column(Float, nullable=False)
    exit_price = Column(Float, nullable=True)
    quantity = Column(Float, nullable=False)
    leverage = Column(Integer, default=1)
    order_id = Column(String(100), nullable=True)
    exchange_trade_id = Column(String(100), nullable=True)
    take_profit = Column(Float, nullable=True)
    stop_loss = Column(Float, nullable=True)
    commission = Column(Float, default=0.0)
    slippage = Column(Float, default=0.0)
    pnl = Column(Float, default=0.0)
    pnl_percent = Column(Float, default=0.0)
    is_paper = Column(Integer, default=1)
    status = Column(Enum(OrderStatus), default=OrderStatus.PENDING)
    opened_at = Column(DateTime, server_default=func.now())
    closed_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index('idx_trades_pair_status', 'pair', 'status'),
        Index('idx_trades_opened_at', 'opened_at'),
    )


class Order(Base):
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True, autoincrement=True)
    trade_id = Column(Integer, nullable=True)
    pair = Column(String(20), nullable=False)
    side = Column(Enum(OrderSide), nullable=False)
    order_type = Column(Enum(OrderType), nullable=False)
    price = Column(Float, nullable=True)
    quantity = Column(Float, nullable=False)
    filled_quantity = Column(Float, default=0.0)
    status = Column(Enum(OrderStatus), default=OrderStatus.PENDING)
    exchange_order_id = Column(String(100), nullable=True)
    is_paper = Column(Integer, default=1)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, onupdate=func.now())


class Signal(Base):
    __tablename__ = "signals"

    id = Column(Integer, primary_key=True, autoincrement=True)
    pair = Column(String(20), nullable=False)
    signal = Column(Enum(SignalType), nullable=False)
    price = Column(Float, nullable=False)
    confidence = Column(Float, default=0.0)
    indicators = Column(Text, nullable=True)
    timestamp = Column(DateTime, server_default=func.now())
    executed = Column(Integer, default=0)


class Balance(Base):
    __tablename__ = "balances"

    id = Column(Integer, primary_key=True, autoincrement=True)
    asset = Column(String(20), nullable=False)
    amount = Column(Float, default=0.0)
    is_paper = Column(Integer, default=1)  # 1 = paper, 0 = real
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index('idx_balances_asset_paper', 'asset', 'is_paper', unique=True),
    )


class Log(Base):
    __tablename__ = "logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    level = Column(String(10), nullable=False)
    module = Column(String(50), nullable=True)
    message = Column(Text, nullable=False)
    timestamp = Column(DateTime, server_default=func.now())

    __table_args__ = (
        Index('idx_logs_timestamp', 'timestamp'),
        Index('idx_logs_level', 'level'),
    )


class ForecastDB(Base):
    __tablename__ = "forecasts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    forecast_id = Column(Float, nullable=False, unique=True)
    pair = Column(String(20), nullable=False)
    signal_type = Column(String(10), nullable=False)
    entry_price = Column(Float, nullable=False)
    target_price = Column(Float, nullable=False)
    current_price = Column(Float, nullable=False)
    confidence = Column(Float, default=70)
    status = Column(String(20), default="active")
    created_at = Column(DateTime, server_default=func.now())
    expires_at = Column(DateTime, nullable=False)
    closed_at = Column(DateTime, nullable=True)
    result = Column(String(50), nullable=True)

    # Додаткові поля для PnL
    position_quantity = Column(Float, default=0.0)  # Кількість монет
    position_usdt = Column(Float, default=0.0)  # Розмір позиції в USDT
    current_pnl = Column(Float, default=0.0)  # Поточний PnL
    closed_pnl = Column(Float, default=0.0)  # Фінальний PnL

    __table_args__ = (
        Index('idx_forecasts_pair_status', 'pair', 'status'),
        Index('idx_forecasts_created_at', 'created_at'),
        Index('idx_forecasts_expires_at', 'expires_at'),
    )