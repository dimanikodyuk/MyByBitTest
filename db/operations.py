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
        self._balance_cache = {}  # Кеш балансу
        self._balance_cache_time = {}  # Час кешу

    def _get_cached_balance(self, asset: str, is_paper: bool) -> Optional[float]:
        """Отримання балансу з кешу"""
        key = f"{asset}_{is_paper}"
        if key in self._balance_cache:
            # Кеш діє 5 секунд
            if (datetime.now() - self._balance_cache_time.get(key, datetime.min)).seconds < 5:
                return self._balance_cache[key]
        return None

    def _set_cached_balance(self, asset: str, is_paper: bool, amount: float):
        """Збереження балансу в кеш"""
        key = f"{asset}_{is_paper}"
        self._balance_cache[key] = amount
        self._balance_cache_time[key] = datetime.now()

    def _clear_balance_cache(self, asset: str = None, is_paper: bool = None):
        """Очищення кешу балансу"""
        if asset and is_paper is not None:
            key = f"{asset}_{is_paper}"
            self._balance_cache.pop(key, None)
            self._balance_cache_time.pop(key, None)
        else:
            self._balance_cache.clear()
            self._balance_cache_time.clear()

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

    def get_open_trades(self, pair: Optional[str] = None, is_paper: bool = True) -> List[Trade]:
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
        return self.db.query(Trade).filter(Trade.is_paper == is_paper).order_by(Trade.opened_at.desc()).limit(
            limit).all()

    # === BALANCE ===
    def get_balance(self, asset: str = "USDT", is_paper: bool = True) -> float:
        """Отримання балансу - з кешем"""
        # Перевіряємо кеш
        cached = self._get_cached_balance(asset, is_paper)
        if cached is not None:
            return cached

        try:
            # Простий ORM запит
            balance_obj = self.db.query(Balance).filter(
                Balance.asset == asset,
                Balance.is_paper == (1 if is_paper else 0)
            ).first()

            if balance_obj is None:
                if is_paper:
                    # Створюємо новий баланс
                    new_balance = Balance(asset=asset, amount=100.0, is_paper=1)
                    self.db.add(new_balance)
                    self.db.commit()
                    self._set_cached_balance(asset, is_paper, 100.0)
                    return 100.0
                return 0.0

            amount = balance_obj.amount if balance_obj.amount is not None else 0.0
            self._set_cached_balance(asset, is_paper, amount)
            return amount

        except Exception as e:
            logger.error(f"Помилка отримання балансу: {e}")
            # При помилці повертаємо кешоване значення або дефолт
            cached = self._get_cached_balance(asset, is_paper)
            if cached is not None:
                return cached
            return 100.0 if is_paper else 0.0

    def get_trade_by_id(self, trade_id: int) -> Optional[Trade]:
        try:
            return self.db.query(Trade).filter(Trade.id == trade_id).first()
        except Exception as e:
            logger.error(f"Помилка отримання угоди {trade_id}: {e}")
            return None

    def update_balance(self, asset: str, amount: float, is_paper: bool = True):
        """Оновлення балансу з очищенням кешу"""
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
            # Очищаємо кеш
            self._clear_balance_cache(asset, is_paper)
            # Зберігаємо нове значення в кеш
            self._set_cached_balance(asset, is_paper, amount)
            logger.info(f"Баланс оновлено: {asset} = {amount} (paper={is_paper})")

        except Exception as e:
            logger.error(f"Помилка оновлення балансу: {e}")
            self.db.rollback()

    def reset_paper_balance(self, initial_balance: float = 100.0):
        """Скидання paper балансу"""
        try:
            self.db.query(Trade).filter(Trade.is_paper == 1).delete()
            self.db.query(Order).filter(Order.is_paper == 1).delete()
            self.db.query(Balance).filter(Balance.is_paper == 1).delete()

            new_balance = Balance(asset="USDT", amount=initial_balance, is_paper=1)
            self.db.add(new_balance)
            self.db.commit()

            # Очищаємо кеш
            self._clear_balance_cache()
            self._set_cached_balance("USDT", True, initial_balance)

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
        trades = self.db.query(Trade).filter(
            Trade.is_paper == is_paper,
            Trade.status == OrderStatus.CLOSED,
            func.date(Trade.closed_at) == today
        ).all()
        return sum(t.pnl for t in trades)

    def get_stats(self, is_paper: bool = True) -> dict:
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