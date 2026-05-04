from sqlalchemy.orm import Session
from sqlalchemy import func, and_
from datetime import datetime, timedelta
from typing import List, Optional, Dict
from db.models import Trade, Signal, Balance, Log, Order, OrderStatus, OrderSide, SignalType
from utils.logger import logger


class DatabaseOperations:
    """CRUD операції з БД"""

    def __init__(self, db_session: Session):
        self.db = db_session

    # === TRADES ===
    def create_trade(self, trade_data: dict) -> Trade:
        trade = Trade(**trade_data)
        self.db.add(trade)
        self.db.commit()
        self.db.refresh(trade)
        logger.info(f"Trade created: {trade.id} - {trade.pair} {trade.side}")
        return trade

    def update_trade(self, trade_id: int, updates: dict) -> Optional[Trade]:
        trade = self.db.query(Trade).filter(Trade.id == trade_id).first()
        if trade:
            for key, value in updates.items():
                setattr(trade, key, value)
            self.db.commit()
            self.db.refresh(trade)
        return trade

    def get_trade_by_id(self, trade_id: int):
        """Отримання угоди за ID"""
        try:
            return self.db.query(Trade).filter(Trade.id == trade_id).first()
        except Exception as e:
            logger.error(f"Помилка отримання угоди {trade_id}: {e}")
            return None

    def get_open_trades(self, pair: Optional[str] = None, is_paper: bool = True) -> List[Trade]:
        """Отримання відкритих угод"""
        try:
            query = self.db.query(Trade).filter(
                Trade.status == OrderStatus.PENDING,
                Trade.is_paper == int(is_paper)
            )
            if pair:
                query = query.filter(Trade.pair == pair)
            return query.all()
        except Exception as e:
            logger.error(f"Помилка отримання відкритих угод: {e}")
            return []

    def get_trades_history(self, limit: int = 100, is_paper: bool = True) -> List[Trade]:
        return self.db.query(Trade).filter(Trade.is_paper == is_paper).order_by(Trade.opened_at.desc()).limit(limit).all()

    # === BALANCE ===
    def get_balance(self, asset: str = "USDT", is_paper: bool = True) -> float:
        """Отримання балансу - через прямий SQL"""
        try:
            from sqlalchemy import text
            paper_int = 1 if is_paper else 0

            # Прямий SQL запит замість ORM
            result = self.db.execute(
                text("SELECT amount FROM balances WHERE asset = :asset AND is_paper = :paper"),
                {"asset": asset, "paper": paper_int}
            ).fetchone()

            if result is None:
                if is_paper:
                    # Створюємо запис через прямий SQL
                    self.db.execute(
                        text("INSERT INTO balances (asset, amount, is_paper) VALUES (:asset, :amount, :paper)"),
                        {"asset": asset, "amount": 100.0, "paper": paper_int}
                    )
                    self.db.commit()
                    return 100.0
                return 0.0

            return float(result[0])

        except Exception as e:
            logger.error(f"Помилка отримання балансу: {e}")
            # При помилці повертаємо безпечне значення
            return 100.0 if is_paper else 0.0

    def get_trade_by_id(self, trade_id: int) -> Optional[Trade]:
        """Отримання угоди за ID"""
        try:
            return self.db.query(Trade).filter(Trade.id == trade_id).first()
        except Exception as e:
            logger.error(f"Помилка отримання угоди {trade_id}: {e}")
            return None

    def update_balance(self, asset: str, amount: float, is_paper: bool = True):
        """Оновлення балансу - через прямий SQL"""
        try:
            from sqlalchemy import text
            paper_int = 1 if is_paper else 0

            # Перевіряємо чи існує запис
            result = self.db.execute(
                text("SELECT id FROM balances WHERE asset = :asset AND is_paper = :paper"),
                {"asset": asset, "paper": paper_int}
            ).fetchone()

            if result:
                # Оновлюємо існуючий
                self.db.execute(
                    text(
                        "UPDATE balances SET amount = :amount, updated_at = CURRENT_TIMESTAMP WHERE asset = :asset AND is_paper = :paper"),
                    {"amount": amount, "asset": asset, "paper": paper_int}
                )
            else:
                # Створюємо новий
                self.db.execute(
                    text("INSERT INTO balances (asset, amount, is_paper) VALUES (:asset, :amount, :paper)"),
                    {"asset": asset, "amount": amount, "paper": paper_int}
                )

            self.db.commit()
            logger.info(f"Баланс оновлено: {asset} = {amount} (paper={is_paper})")

        except Exception as e:
            logger.error(f"Помилка оновлення балансу: {e}")
            self.db.rollback()

    def reset_paper_balance(self, initial_balance: float = 100.0):
        """Скидання paper балансу - через прямий SQL"""
        try:
            from sqlalchemy import text

            # Видаляємо всі paper угоди
            self.db.execute(text("DELETE FROM trades WHERE is_paper = 1"))
            self.db.execute(text("DELETE FROM orders WHERE is_paper = 1"))

            # Оновлюємо баланс
            self.db.execute(text("DELETE FROM balances WHERE is_paper = 1"))
            self.db.execute(
                text("INSERT INTO balances (asset, amount, is_paper) VALUES (:asset, :amount, :paper)"),
                {"asset": "USDT", "amount": initial_balance, "paper": 1}
            )

            self.db.commit()
            logger.info(f"Paper balance reset to {initial_balance} USDT")

        except Exception as e:
            logger.error(f"Помилка скидання балансу: {e}")
            self.db.rollback()

    # === SIGNALS ===
    def save_signal(self, pair: str, signal: SignalType, price: float,
                    confidence: float = 0.0, indicators: dict = None) -> Signal:
        import json
        signal_data = {
            "pair": pair,
            "signal": signal,
            "price": price,
            "confidence": confidence,
            "indicators": json.dumps(indicators) if indicators else None
        }
        db_signal = Signal(**signal_data)
        self.db.add(db_signal)
        self.db.commit()
        return db_signal

    # === LOGS ===
    def add_log(self, level: str, message: str, module: str = None):
        log = Log(level=level, message=message, module=module)
        self.db.add(log)
        self.db.commit()

    def cleanup_old_logs(self, days: int = 7):
        cutoff_date = datetime.now() - timedelta(days=days)
        deleted = self.db.query(Log).filter(Log.timestamp < cutoff_date).delete()
        self.db.commit()
        logger.info(f"Cleaned up {deleted} old log records")

    # === STATISTICS ===
    def get_daily_pnl(self, is_paper: bool = True) -> float:
        """Отримати PnL за сьогодні"""
        today = datetime.now().date()
        trades = self.db.query(Trade).filter(
            Trade.is_paper == is_paper,
            Trade.status == OrderStatus.CLOSED,
            func.date(Trade.closed_at) == today
        ).all()

        total_pnl = sum(t.pnl for t in trades)
        return total_pnl

    def get_stats(self, is_paper: bool = True) -> dict:
        """Отримати статистику"""
        closed_trades = self.db.query(Trade).filter(
            Trade.is_paper == is_paper,
            Trade.status == OrderStatus.CLOSED
        ).all()

        if not closed_trades:
            return {
                "total_trades": 0,
                "win_rate": 0,
                "total_pnl": 0,
                "avg_pnl": 0,
                "max_drawdown": 0
            }

        wins = len([t for t in closed_trades if t.pnl > 0])
        total_pnl = sum(t.pnl for t in closed_trades)

        return {
            "total_trades": len(closed_trades),
            "win_rate": (wins / len(closed_trades)) * 100,
            "total_pnl": total_pnl,
            "avg_pnl": total_pnl / len(closed_trades),
            "max_drawdown": min(t.pnl for t in closed_trades) if closed_trades else 0
        }