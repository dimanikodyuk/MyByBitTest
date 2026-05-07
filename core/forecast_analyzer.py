"""
Аналізатор прогнозів - оцінка якості та статистика
"""

import asyncio
import json
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from sqlalchemy import func, and_
from sqlalchemy.orm import Session

from db.database import SessionLocal
from db.models import ForecastDB
from exchange.bybit_client import BybitClient
from utils.logger import logger
from utils.config_loader import config


class ForecastAnalyzer:
    """Аналізатор прогнозів - оцінка якості та збір статистики"""

    def __init__(self):
        self.exchange = BybitClient()
        self.running = True

    async def analyze_all_active_forecasts(self):
        """Аналіз всіх активних прогнозів (оновлення max/min цін)"""
        db = SessionLocal()
        try:
            active_forecasts = db.query(ForecastDB).filter(
                ForecastDB.status == "active"
            ).all()

            for forecast in active_forecasts:
                await self._update_forecast_prices(db, forecast)

        except Exception as e:
            logger.error(f"Помилка аналізу прогнозів: {e}")
        finally:
            db.close()

    async def _update_forecast_prices(self, db: Session, forecast: ForecastDB):
        """Оновлення max/min цін для прогнозу"""
        try:
            current_price = self.exchange.get_current_price(forecast.pair)
            if not current_price or current_price <= 0:
                return

            # Отримуємо історичні дані за період прогнозу
            klines = self.exchange.get_klines(
                forecast.pair,
                "5m",
                limit=100
            )

            if klines is not None and not klines.empty:
                max_price = max(klines['high'].max(), current_price)
                min_price = min(klines['low'].min(), current_price)

                # Оновлюємо max/min ціни
                if forecast.max_price_reached is None or max_price > forecast.max_price_reached:
                    forecast.max_price_reached = max_price
                if forecast.min_price_reached is None or min_price < forecast.min_price_reached:
                    forecast.min_price_reached = min_price

                # Розраховуємо відсоток досягнення цілі
                if forecast.signal_type == "LONG":
                    if forecast.target_price <= forecast.max_price_reached:
                        # Ціль досягнуто
                        forecast.hit_percentage = 100.0
                        if forecast.status == "active" and forecast.result != "hit":
                            forecast.result = "hit"
                            forecast.target_hit_time = datetime.now()
                            logger.info(f"🎯 Прогноз {forecast.forecast_id} досягнуто! {forecast.pair} {forecast.signal_type}")
                    else:
                        # Відсоток досягнення
                        progress = (forecast.max_price_reached - forecast.entry_price) / (forecast.target_price - forecast.entry_price) * 100
                        forecast.hit_percentage = max(0, min(100, progress))
                else:  # SHORT
                    if forecast.target_price >= forecast.min_price_reached:
                        forecast.hit_percentage = 100.0
                        if forecast.status == "active" and forecast.result != "hit":
                            forecast.result = "hit"
                            forecast.target_hit_time = datetime.now()
                            logger.info(f"🎯 Прогноз {forecast.forecast_id} досягнуто! {forecast.pair} {forecast.signal_type}")
                    else:
                        progress = (forecast.entry_price - forecast.min_price_reached) / (forecast.entry_price - forecast.target_price) * 100
                        forecast.hit_percentage = max(0, min(100, progress))

                # Розраховуємо якість прогнозу
                forecast.quality_score = self._calculate_quality_score(forecast)

            # Оновлюємо поточну ціну
            forecast.current_price = current_price

            # Розраховуємо поточний PnL
            if forecast.signal_type == "LONG":
                forecast.current_pnl = (current_price - forecast.entry_price) / forecast.entry_price * 100
            else:
                forecast.current_pnl = (forecast.entry_price - current_price) / forecast.entry_price * 100

            db.commit()

        except Exception as e:
            logger.error(f"Помилка оновлення цін для прогнозу {forecast.forecast_id}: {e}")

    def _calculate_quality_score(self, forecast: ForecastDB) -> float:
        """Розрахунок оцінки якості прогнозу (0-100)"""
        score = 0.0

        # 1. Наскільки досягнуто ціль (40% ваги)
        score += forecast.hit_percentage * 0.4

        # 2. Впевненість моделі (20% ваги)
        confidence_score = min(100, forecast.confidence) / 100 * 20
        score += confidence_score

        # 3. Час до досягнення цілі (20% ваги)
        if forecast.target_hit_time and forecast.created_at:
            time_to_hit = (forecast.target_hit_time - forecast.created_at).total_seconds() / 3600
            duration_hours = config.get('testing.forecast_duration_hours', 12)
            if time_to_hit <= duration_hours * 0.25:
                score += 20  # швидкий успіх
            elif time_to_hit <= duration_hours * 0.5:
                score += 15
            elif time_to_hit <= duration_hours * 0.75:
                score += 10
            elif time_to_hit <= duration_hours:
                score += 5
        else:
            # Якщо ще не досягнуто, додаємо час до закінчення
            if forecast.expires_at:
                remaining = (forecast.expires_at - datetime.now()).total_seconds() / 3600
                if remaining > 0:
                    score += max(0, 5 * (1 - remaining / duration_hours))

        # 4. Історична точність по парі (20% ваги)
        historical_accuracy = self._get_pair_accuracy(forecast.pair, forecast.signal_type)
        score += historical_accuracy * 0.2

        return min(100, max(0, score))

    def _get_pair_accuracy(self, pair: str, signal_type: str) -> float:
        """Отримання історичної точності по парі та типу сигналу"""
        db = SessionLocal()
        try:
            completed = db.query(ForecastDB).filter(
                ForecastDB.pair == pair,
                ForecastDB.signal_type == signal_type,
                ForecastDB.status == "completed",
                ForecastDB.result == "hit"
            ).count()

            total = db.query(ForecastDB).filter(
                ForecastDB.pair == pair,
                ForecastDB.signal_type == signal_type,
                ForecastDB.status == "completed"
            ).count()

            if total > 0:
                return (completed / total) * 100
            return 50.0  # Нейтральне значення для нових пар
        finally:
            db.close()

    async def check_expired_forecasts(self):
        """Перевірка прострочених прогнозів та їх закриття"""
        db = SessionLocal()
        try:
            now = datetime.now()
            expired = db.query(ForecastDB).filter(
                ForecastDB.expires_at < now,
                ForecastDB.status == "active"
            ).all()

            for forecast in expired:
                await self._close_expired_forecast(db, forecast)

        except Exception as e:
            logger.error(f"Помилка перевірки прострочених прогнозів: {e}")
        finally:
            db.close()

    async def _close_expired_forecast(self, db: Session, forecast: ForecastDB):
        """Закриття простроченого прогнозу з розрахунком результату"""
        try:
            # Визначаємо результат
            if forecast.hit_percentage >= 100:
                forecast.result = "hit"
                forecast.status = "completed"
                forecast.closed_pnl = forecast.current_pnl
                forecast.actual_profit_pct = forecast.current_pnl
                logger.info(f"✅ Прогноз {forecast.forecast_id} успішний! PnL: {forecast.current_pnl:.1f}%")
            elif forecast.hit_percentage >= 70:
                forecast.result = "partial"
                forecast.status = "completed"
                forecast.closed_pnl = forecast.current_pnl
                forecast.actual_profit_pct = forecast.current_pnl
                logger.info(f"📊 Прогноз {forecast.forecast_id} частковий! PnL: {forecast.current_pnl:.1f}%")
            else:
                forecast.result = "miss"
                forecast.status = "expired"
                forecast.closed_pnl = forecast.current_pnl
                forecast.actual_profit_pct = forecast.current_pnl
                logger.info(f"❌ Прогноз {forecast.forecast_id} не спрацював! PnL: {forecast.current_pnl:.1f}%")

            forecast.closed_at = datetime.now()
            forecast.quality_score = self._calculate_quality_score(forecast)
            db.commit()

        except Exception as e:
            logger.error(f"Помилка закриття прогнозу {forecast.forecast_id}: {e}")

    async def get_forecast_statistics(self) -> Dict:
        """Отримання статистики прогнозів"""
        db = SessionLocal()
        try:
            # Загальна статистика
            total = db.query(ForecastDB).count()
            completed = db.query(ForecastDB).filter(ForecastDB.status == "completed").count()
            expired = db.query(ForecastDB).filter(ForecastDB.status == "expired").count()
            active = db.query(ForecastDB).filter(ForecastDB.status == "active").count()

            # Hit rate
            hits = db.query(ForecastDB).filter(ForecastDB.result == "hit").count()
            partials = db.query(ForecastDB).filter(ForecastDB.result == "partial").count()
            misses = db.query(ForecastDB).filter(ForecastDB.result == "miss").count()

            hit_rate = (hits / completed * 100) if completed > 0 else 0
            partial_rate = (partials / completed * 100) if completed > 0 else 0

            # Статистика по парах
            pair_stats = db.query(
                ForecastDB.pair,
                func.count(ForecastDB.id).label('total'),
                func.sum(case((ForecastDB.result == "hit", 1), else_=0)).label('hits')
            ).group_by(ForecastDB.pair).all()

            pairs_accuracy = [
                {"pair": p[0], "total": p[1], "hits": p[2], "accuracy": (p[2] / p[1] * 100) if p[1] > 0 else 0}
                for p in pair_stats
            ]

            # Статистика по типах сигналів
            long_stats = db.query(ForecastDB).filter(ForecastDB.signal_type == "LONG", ForecastDB.status == "completed")
            short_stats = db.query(ForecastDB).filter(ForecastDB.signal_type == "SHORT", ForecastDB.status == "completed")

            long_hits = long_stats.filter(ForecastDB.result == "hit").count()
            long_total = long_stats.count()
            short_hits = short_stats.filter(ForecastDB.result == "hit").count()
            short_total = short_stats.count()

            # Середня якість
            avg_quality = db.query(func.avg(ForecastDB.quality_score)).scalar() or 0

            # Найкращий прогноз
            best_forecast = db.query(ForecastDB).filter(
                ForecastDB.result == "hit"
            ).order_by(ForecastDB.actual_profit_pct.desc()).first()

            # Найгірший прогноз
            worst_forecast = db.query(ForecastDB).filter(
                ForecastDB.status == "expired"
            ).order_by(ForecastDB.actual_profit_pct.asc()).first()

            return {
                "total": total,
                "active": active,
                "completed": completed,
                "expired": expired,
                "hits": hits,
                "partials": partials,
                "misses": misses,
                "hit_rate": round(hit_rate, 2),
                "partial_rate": round(partial_rate, 2),
                "avg_quality": round(avg_quality, 2),
                "long_accuracy": round((long_hits / long_total * 100) if long_total > 0 else 0, 2),
                "short_accuracy": round((short_hits / short_total * 100) if short_total > 0 else 0, 2),
                "pairs_accuracy": sorted(pairs_accuracy, key=lambda x: x['accuracy'], reverse=True)[:10],
                "best_forecast": {
                    "pair": best_forecast.pair if best_forecast else None,
                    "profit_pct": best_forecast.actual_profit_pct if best_forecast else None,
                    "confidence": best_forecast.confidence if best_forecast else None
                } if best_forecast else None,
                "worst_forecast": {
                    "pair": worst_forecast.pair if worst_forecast else None,
                    "loss_pct": worst_forecast.actual_profit_pct if worst_forecast else None,
                } if worst_forecast else None
            }

        except Exception as e:
            logger.error(f"Помилка отримання статистики прогнозів: {e}")
            return {}
        finally:
            db.close()

    async def run(self):
        """Основний цикл аналізатора"""
        logger.info("📊 Аналізатор прогнозів запущено")

        while self.running:
            try:
                # Оновлюємо ціни активних прогнозів кожні 30 секунд
                await self.analyze_all_active_forecasts()

                # Перевіряємо прострочені прогнози кожні хвилину
                await self.check_expired_forecasts()

                await asyncio.sleep(30)

            except Exception as e:
                logger.error(f"Помилка в аналізаторі прогнозів: {e}")
                await asyncio.sleep(30)

    def stop(self):
        self.running = False
        logger.info("📊 Аналізатор прогнозів зупинено")


# Допоміжна функція для SQLAlchemy case
def case(whens, else_=None):
    from sqlalchemy import case as sql_case
    return sql_case(whens, else_=else_)