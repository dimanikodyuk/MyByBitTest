from sqlalchemy.orm import Session
from sqlalchemy import func, and_, text
from datetime import datetime, timedelta
from typing import List, Optional, Dict
from db.models import Trade, Signal, Balance, Log, Order, OrderStatus, OrderSide, SignalType
from utils.logger import logger


class DatabaseOperations:
    """CRUD операції з БД - ВИПРАВЛЕНО"""

    def __init__(self, db_session: Session):
        self.db = db_session

    def _safe_execute(self, query_func):
        """Безпечне виконання запитів з автоматичним rollback при помилці"""
        try:
            # Скидаємо будь-яку попередню помилку
            try:
                if self.db.is_active:
                    self.db.rollback()
            except:
                pass
            return query_func()
        except Exception as e:
            logger.error(f"Помилка виконання запиту: {e}")
            try:
                self.db.rollback()
            except:
                pass
            raise

    # === TRADES ===
    def create_trade(self, trade_data: dict) -> Trade:
        def _create():
            trade = Trade(**trade_data)
            self.db.add(trade)
            self.db.commit()
            self.db.refresh(trade)
            logger.info(f"Trade created: {trade.id} - {trade.pair} {trade.side}")
            return trade

        return self._safe_execute(_create)

    def update_trade(self, trade_id: int, updates: dict) -> Optional[Trade]:
        def _update():
            trade = self.db.query(Trade).filter(Trade.id == trade_id).first()
            if trade:
                for key, value in updates.items():
                    setattr(trade, key, value)
                self.db.commit()
                self.db.refresh(trade)
            return trade

        return self._safe_execute(_update)

    def get_trade_by_id(self, trade_id: int) -> Optional[Trade]:
        def _get():
            return self.db.query(Trade).filter(Trade.id == trade_id).first()

        return self._safe_execute(_get)

    def get_open_trades(self, pair: Optional[str] = None, is_paper: bool = True) -> List[Trade]:
        """Отримання відкритих угод - ВИПРАВЛЕНО"""

        def _get():
            from db.models import OrderStatus

            paper_val = 1 if is_paper else 0

            # Скидаємо транзакцію перед запитом
            try:
                if self.db.is_active:
                    self.db.rollback()
            except:
                pass

            # Простий ORM запит без складних параметрів
            query = self.db.query(Trade).filter(
                Trade.status == "PENDING",
                Trade.is_paper == paper_val
            )

            if pair:
                query = query.filter(Trade.pair == pair)

            # Обмежуємо кількість результатів
            query = query.order_by(Trade.opened_at.desc()).limit(100)

            # Виконуємо запит
            try:
                trades = query.all()
                logger.debug(f"Знайдено {len(trades)} відкритих угод (pair={pair}, is_paper={is_paper})")
                return trades
            except Exception as e:
                logger.error(f"Помилка виконання запиту: {e}")
                # Скидаємо сесію перед повторною спробою
                try:
                    self.db.rollback()
                except:
                    pass
                return []

        return self._safe_execute(_get)

    def get_trades_history(self, limit: int = 100, is_paper: bool = True) -> List[Trade]:
        def _get():
            paper_val = 1 if is_paper else 0
            return self.db.query(Trade).filter(
                Trade.is_paper == paper_val,
                Trade.status == "CLOSED"
            ).order_by(Trade.opened_at.desc()).limit(limit).all()

        return self._safe_execute(_get)

    # === BALANCE ===
    def get_balance(self, asset: str = "USDT", is_paper: bool = True) -> float:
        """Отримання балансу - БЕЗПЕЧНА ВЕРСІЯ"""

        def _get():
            paper_val = 1 if is_paper else 0

            # Скидаємо транзакцію
            try:
                if self.db.is_active:
                    self.db.rollback()
            except:
                pass

            balance_obj = self.db.query(Balance).filter(
                Balance.asset == asset,
                Balance.is_paper == paper_val
            ).with_for_update().first()

            if balance_obj is not None:
                return float(balance_obj.amount) if balance_obj.amount is not None else 0.0

            if is_paper:
                from utils.config_loader import config
                initial = float(config.get('paper_trading.initial_balance', 100.0))

                # Перевіряємо ще раз
                existing = self.db.query(Balance).filter(
                    Balance.asset == asset,
                    Balance.is_paper == paper_val
                ).first()

                if existing is None:
                    new_balance = Balance(asset=asset, amount=initial, is_paper=paper_val)
                    self.db.add(new_balance)
                    self.db.commit()
                    self.db.refresh(new_balance)
                    logger.info(f"Створено новий paper баланс: {initial} {asset}")
                    return initial
                else:
                    return float(existing.amount)

            return 0.0

        return self._safe_execute(_get)

    def update_balance(self, asset: str, amount: float, is_paper: bool = True):
        def _update():
            paper_val = 1 if is_paper else 0

            # Скидаємо транзакцію
            try:
                if self.db.is_active:
                    self.db.rollback()
            except:
                pass

            balance_obj = self.db.query(Balance).filter(
                Balance.asset == asset,
                Balance.is_paper == paper_val
            ).with_for_update().first()

            if balance_obj:
                balance_obj.amount = amount
            else:
                balance_obj = Balance(asset=asset, amount=amount, is_paper=paper_val)
                self.db.add(balance_obj)

            self.db.commit()
            logger.info(f"Баланс оновлено: {asset} = {amount:.4f} (paper={is_paper})")

        return self._safe_execute(_update)

    def reset_paper_balance(self, initial_balance: float = 100.0):
        def _reset():
            # Скидаємо транзакцію
            try:
                if self.db.is_active:
                    self.db.rollback()
            except:
                pass

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

        return self._safe_execute(_reset)

    # === SIGNALS ===
    def save_signal(self, pair: str, signal: SignalType, price: float,
                    confidence: float = 0.0, indicators: dict = None) -> Signal:
        import json
        def _save():
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

        return self._safe_execute(_save)

    # === LOGS ===
    def add_log(self, level: str, message: str, module: str = None):
        def _add():
            log = Log(level=level, message=message, module=module)
            self.db.add(log)
            self.db.commit()

        return self._safe_execute(_add)

    def cleanup_old_logs(self, days: int = 7):
        def _cleanup():
            cutoff_date = datetime.now() - timedelta(days=days)
            deleted = self.db.query(Log).filter(Log.timestamp < cutoff_date).delete()
            self.db.commit()
            logger.info(f"Cleaned up {deleted} old log records")

        return self._safe_execute(_cleanup)

    # === STATISTICS ===
    def get_daily_pnl(self, is_paper: bool = True) -> float:
        def _get():
            today = datetime.now().date()
            paper_val = 1 if is_paper else 0
            trades = self.db.query(Trade).filter(
                Trade.is_paper == paper_val,
                Trade.status == "CLOSED",
                func.date(Trade.closed_at) == today
            ).all()
            return sum(t.pnl or 0.0 for t in trades)

        return self._safe_execute(_get)

    def get_forecasts_count_today(self) -> int:
        """Кількість прогнозів створених сьогодні"""
        try:
            from db.models import ForecastDB
            from datetime import date
            today = date.today()
            count = self.db.query(ForecastDB).filter(
                func.date(ForecastDB.created_at) == today
            ).count()
            return count
        except Exception as e:
            logger.error(f"Помилка підрахунку прогнозів: {e}")
            return 0


    def get_stats(self, is_paper: bool = True) -> dict:
        def _get():
            paper_val = 1 if is_paper else 0
            closed_trades = self.db.query(Trade).filter(
                Trade.is_paper == paper_val,
                Trade.status == "CLOSED"
            ).all()

            if not closed_trades:
                return {
                    "total_trades": 0,
                    "win_rate": 0,
                    "total_pnl": 0,
                    "avg_pnl": 0,
                    "max_drawdown": 0,
                    "profit_factor": 0
                }

            pnl_list = [t.pnl or 0.0 for t in closed_trades]
            wins = len([p for p in pnl_list if p > 0])
            total_pnl = sum(pnl_list)

            gross_profit = sum(p for p in pnl_list if p > 0)
            gross_loss = abs(sum(p for p in pnl_list if p < 0)) if any(p < 0 for p in pnl_list) else 1

            return {
                "total_trades": len(closed_trades),
                "win_rate": round((wins / len(closed_trades)) * 100, 2),
                "total_pnl": round(total_pnl, 4),
                "avg_pnl": round(total_pnl / len(closed_trades), 4),
                "max_drawdown": round(min(pnl_list), 4) if pnl_list else 0,
                "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss > 0 else 0
            }

        return self._safe_execute(_get)

    def fix_balance_table(self):
        """Виправити дублікати в таблиці balances"""

        def _fix():
            try:
                # Знаходимо дублікати
                query = text("""
                    SELECT asset, is_paper, COUNT(*) as cnt 
                    FROM balances 
                    GROUP BY asset, is_paper 
                    HAVING cnt > 1
                """)
                duplicates = self.db.execute(query).fetchall()

                for dup in duplicates:
                    asset, is_paper, cnt = dup
                    logger.warning(f"Знайдено {cnt} дублікатів для {asset}, is_paper={is_paper}")

                    # Видаляємо дублікати
                    delete_query = text("""
                        DELETE FROM balances 
                        WHERE id NOT IN (
                            SELECT MIN(id) 
                            FROM balances 
                            WHERE asset = :asset AND is_paper = :is_paper
                        )
                        AND asset = :asset AND is_paper = :is_paper
                    """)
                    self.db.execute(delete_query, {"asset": asset, "is_paper": is_paper})
                    self.db.commit()

                logger.info("Таблицю balances виправлено")
                return True
            except Exception as e:
                logger.error(f"Помилка виправлення balances: {e}")
                self.db.rollback()
                return False

        return self._safe_execute(_fix)