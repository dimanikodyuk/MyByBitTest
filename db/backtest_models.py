from sqlalchemy import Column, Integer, String, Float, DateTime, Text, JSON, Index
from sqlalchemy.sql import func
from db.base import Base


class Backtest(Base):
    """Модель для збереження результатів бектесту"""
    __tablename__ = "backtests"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False)
    description = Column(Text, nullable=True)

    # Параметри бектесту
    symbol = Column(String(20), nullable=False)
    timeframe = Column(String(10), nullable=False)
    start_date = Column(DateTime, nullable=False)
    end_date = Column(DateTime, nullable=False)
    initial_balance = Column(Float, default=1000.0)
    risk_percent = Column(Float, default=2.0)

    # Результати
    total_return_pct = Column(Float, default=0.0)
    final_balance = Column(Float, default=0.0)
    total_trades = Column(Integer, default=0)
    win_trades = Column(Integer, default=0)
    loss_trades = Column(Integer, default=0)
    win_rate = Column(Float, default=0.0)
    total_pnl = Column(Float, default=0.0)
    gross_profit = Column(Float, default=0.0)
    gross_loss = Column(Float, default=0.0)
    profit_factor = Column(Float, default=0.0)
    max_drawdown = Column(Float, default=0.0)
    sharpe_ratio = Column(Float, default=0.0)
    expectancy = Column(Float, default=0.0)

    # Деталі
    trades = Column(JSON, default=[])  # Список угод
    equity_values = Column(JSON, default=[])  # Крива балансу
    equity_timestamps = Column(JSON, default=[])  # Часова мітка для кривої

    created_at = Column(DateTime, server_default=func.now())

    __table_args__ = (
        Index('idx_backtests_symbol', 'symbol'),
        Index('idx_backtests_created', 'created_at'),
    )