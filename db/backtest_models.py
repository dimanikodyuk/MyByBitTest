from sqlalchemy import Column, Integer, String, Float, DateTime, Text, JSON
from sqlalchemy.sql import func
from db.base import Base


class Backtest(Base):
    __tablename__ = "backtests"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False)
    description = Column(Text, nullable=True)

    # Параметри бектесту
    symbol = Column(String(20), nullable=False)
    timeframe = Column(String(10), nullable=False)  # 1m, 5m, 15m, 1h
    start_date = Column(DateTime, nullable=False)
    end_date = Column(DateTime, nullable=False)
    initial_balance = Column(Float, default=1000.0)

    # Результати
    total_return = Column(Float, default=0.0)
    total_trades = Column(Integer, default=0)
    win_rate = Column(Float, default=0.0)
    profit_factor = Column(Float, default=0.0)
    max_drawdown = Column(Float, default=0.0)
    sharpe_ratio = Column(Float, default=0.0)
    expectancy = Column(Float, default=0.0)

    # Деталі
    trades = Column(JSON, default=[])  # Список угод
    equity_curve = Column(JSON, default=[])  # Крива балансу

    created_at = Column(DateTime, server_default=func.now())

    __table_args__ = (
        Index('idx_backtests_symbol', 'symbol'),
        Index('idx_backtests_created', 'created_at'),
    )


class BacktestTrade(Base):
    __tablename__ = "backtest_trades"

    id = Column(Integer, primary_key=True, autoincrement=True)
    backtest_id = Column(Integer, nullable=False)
    entry_time = Column(DateTime, nullable=False)
    exit_time = Column(DateTime, nullable=True)
    side = Column(String(10), nullable=False)  # LONG / SHORT
    entry_price = Column(Float, nullable=False)
    exit_price = Column(Float, nullable=True)
    quantity = Column(Float, nullable=False)
    pnl = Column(Float, default=0.0)
    pnl_percent = Column(Float, default=0.0)