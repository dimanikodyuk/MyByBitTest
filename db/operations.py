from sqlalchemy.orm import Session
from sqlalchemy import func, and_, text
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

    def get_trade_by_id(self, trade_id: int) -> Optional[Trade]:
        """Отримання угоди за ID"""
        try:
            return self.db.query(Trade).filter(Trade.id == trade_id).first()
        except Exception as e:
            logger.error(f"Помилка отримання угоди {trade_id}: {e}")
            return None

    def get_open_trades(self, pair: Optional[str] = None, is_paper: bool = True) -> List[Trade]:
        """Отримання відкритих угод"""
        try:
            # Імпортуємо тут, щоб уникнути циркулярних імпортів
            from db.models import OrderStatus

            paper_val = 1 if is_paper else 0

            # Простий ORM запит - без raw SQL
            query = self.db.query(Trade).filter(
                Trade.status == OrderStatus.PENDING,
                Trade.is_paper == paper_val
            )

            if pair:
                query = query.filter(Trade.pair == pair)

            # Додаємо сортування та ліміт
            query = query.order_by(Trade.opened_at.desc()).limit(100)

            # Виконуємо запит
            trades = query.all()

            # Логуємо результат
            logger.debug(f"Знайдено {len(trades)} відкритих угод (pair={pair}, is_paper={is_paper})")

            return trades

        except Exception as e:
            logger.error(f"Помилка отримання відкритих угод: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return []

    def get_trades_history(self, limit: int = 100, is_paper: bool = True) -> List[Trade]:
        paper_val = 1 if is_paper else 0
        return self.db.query(Trade).filter(Trade.is_paper == paper_val).order_by(Trade.opened_at.desc()).limit(limit).all()

    # === BALANCE ===
    def get_balance(self, asset: str = "USDT", is_paper: bool = True) -> float:
        """Отримання балансу"""
        import traceback
        try:
            paper_val = 1 if is_paper else 0

            balance_obj = self.db.query(Balance).filter(
                Balance.asset == asset,
                Balance.is_paper == paper_val
            ).first()

            if balance_obj is None:
                if is_paper:
                    from utils.config_loader import config
                    initial = float(config.get('paper_trading.initial_balance', 100.0))
                    new_balance = Balance(asset=asset, amount=initial, is_paper=paper_val)
                    self.db.add(new_balance)
                    self.db.commit()
                    self.db.refresh(new_balance)
                    logger.info(f"Створено новий paper баланс: {initial} {asset}")
                    return initial
                return 0.0

            amount = balance_obj.amount
            if amount is None:
                logger.warning(f"Balance.amount is None для {asset} (is_paper={is_paper})")
                return 0.0

            return float(amount)

        except Exception as e:
            logger.error(f"Помилка отримання балансу ({asset}, is_paper={is_paper}): {e}")
            logger.error(traceback.format_exc())
            # Повертаємо 0, щоб вищий рівень міг коректно відреагувати
            # (НЕ 100.0 — це маскувало б справжні помилки)
            return 0.0

    def update_balance(self, asset: str, amount: float, is_paper: bool = True):
        """Оновлення балансу"""
        try:
            paper_val = 1 if is_paper else 0

            balance_obj = self.db.query(Balance).filter(
                Balance.asset == asset,
                Balance.is_paper == paper_val
            ).first()

            if balance_obj:
                balance_obj.amount = amount
            else:
                balance_obj = Balance(asset=asset, amount=amount, is_paper=paper_val)
                self.db.add(balance_obj)

            self.db.commit()
            logger.info(f"Баланс оновлено: {asset} = {amount:.4f} (paper={is_paper})")

        except Exception as e:
            logger.error(f"Помилка оновлення балансу: {e}")
            self.db.rollback()

    def reset_paper_balance(self, initial_balance: float = 100.0):
        """Скидання paper балансу"""
        try:
            # Видаляємо paper угоди
            self.db.query(Trade).filter(Trade.is_paper == 1).delete()
            self.db.query(Order).filter(Order.is_paper == 1).delete()

            # Видаляємо paper баланс
            self.db.query(Balance).filter(Balance.is_paper == 1).delete()

            # Створюємо новий баланс
            new_balance = Balance(asset="USDT", amount=initial_balance, is_paper=1)
            self.db.add(new_balance)

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
        today = datetime.now().date()
        paper_val = 1 if is_paper else 0
        trades = self.db.query(Trade).filter(
            Trade.is_paper == paper_val,
            Trade.status == OrderStatus.CLOSED,
            func.date(Trade.closed_at) == today
        ).all()
        return sum(t.pnl or 0.0 for t in trades)

    def get_stats(self, is_paper: bool = True) -> dict:
        paper_val = 1 if is_paper else 0
        closed_trades = self.db.query(Trade).filter(
            Trade.is_paper == paper_val,
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

        pnl_list = [t.pnl or 0.0 for t in closed_trades]
        wins = len([p for p in pnl_list if p > 0])
        total_pnl = sum(pnl_list)

        return {
            "total_trades": len(closed_trades),
            "win_rate": round((wins / len(closed_trades)) * 100, 2),
            "total_pnl": round(total_pnl, 4),
            "avg_pnl": round(total_pnl / len(closed_trades), 4),
            "max_drawdown": round(min(pnl_list), 4) if pnl_list else 0
        }