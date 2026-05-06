"""
Моніторинг нових лістингів на Bybit
Віртуальний портфель $100
"""

import asyncio
import requests
import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from sqlalchemy.orm import Session
from db.database import SessionLocal
from db.news_models import ListingTrade, ListingBalance
from exchange.bybit_client import BybitClient
from utils.config_loader import config
from utils.logger import logger


class ListingMonitor:
    """Моніторинг нових лістингів на Bybit"""

    def __init__(self):
        self.exchange = BybitClient()
        self.enabled = config.get('listing_trading.enabled', True)
        self.initial_balance = config.get('listing_trading.initial_balance', 100.0)
        self.position_percent = config.get('listing_trading.position_percent', 25.0)
        self.hold_minutes = config.get('listing_trading.hold_minutes', 30)
        self.max_trades_per_day = config.get('listing_trading.max_trades_per_day', 5)
        self.tp_percent = config.get('listing_trading.take_profit_percent', 50.0)
        self.sl_percent = config.get('listing_trading.stop_loss_percent', 20.0)

        self.running = True
        self.processed_listings = set()

    def get_balance(self, db: Session) -> float:
        """Отримання поточного балансу"""
        balance = db.query(ListingBalance).first()
        if not balance:
            balance = ListingBalance(amount=self.initial_balance, initial_balance=self.initial_balance)
            db.add(balance)
            db.commit()
            db.refresh(balance)
        return balance.amount

    def update_balance(self, db: Session, new_balance: float, pnl: float = 0):
        """Оновлення балансу"""
        balance = db.query(ListingBalance).first()
        if not balance:
            balance = ListingBalance(amount=new_balance, initial_balance=self.initial_balance)
            db.add(balance)
        else:
            balance.amount = new_balance
            balance.total_pnl += pnl
            balance.total_trades += 1
            if pnl > 0:
                balance.win_trades += 1
        db.commit()

    def get_bybit_announcements(self) -> List[Dict]:
        """
        Отримання анонсів нових лістингів з Bybit
        """
        try:
            url = "https://announcements.bybit.com/api/announcements"
            params = {
                "category": "listing",
                "limit": 20,
                "language": "en_US"
            }

            response = requests.get(url, params=params, timeout=10)
            data = response.json()

            announcements = []
            if data.get("result", {}).get("items"):
                for item in data["result"]["items"]:
                    title = item.get("title", "")

                    if "listing" in title.lower() or "new" in title.lower():
                        match = re.search(r'([A-Z]{3,10})', title)
                        symbol = match.group(1) if match else None

                        if symbol:
                            announcements.append({
                                "title": title,
                                "symbol": symbol,
                                "pair": f"{symbol}USDT",
                                "url": item.get("url", ""),
                                "published_at": datetime.fromtimestamp(item.get("releaseTime", 0) / 1000) if item.get("releaseTime") else datetime.now()
                            })

            return announcements

        except Exception as e:
            logger.error(f"Помилка отримання анонсів Bybit: {e}")
            return []

    def execute_trade(self, db: Session, listing: Dict):
        """Виконання угоди на нову монету"""
        import asyncio

        balance = self.get_balance(db)
        position_usdt = balance * (self.position_percent / 100)

        if position_usdt < 10:
            logger.warning(f"Недостатньо балансу для угоди: {balance:.2f} USDT")
            return None

        pair = listing["pair"]

        # Отримуємо ціну
        current_price = self.exchange.get_current_price(pair)

        retries = 5
        while not current_price and retries > 0:
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(asyncio.sleep(2))
                loop.close()
            except:
                import time
                time.sleep(2)
            current_price = self.exchange.get_current_price(pair)
            retries -= 1

        if not current_price or current_price <= 0:
            logger.error(f"Не вдалося отримати ціну для {pair}")
            return None

        quantity = position_usdt / current_price

        min_qty = 0.001
        if "BTC" in pair:
            min_qty = 0.0001
        elif "DOGE" in pair:
            min_qty = 1.0
        elif "SHIB" in pair:
            min_qty = 100.0

        if quantity < min_qty:
            quantity = min_qty
            position_usdt = quantity * current_price

        if position_usdt > balance:
            logger.warning(f"Недостатньо коштів: потрібно {position_usdt:.2f}, є {balance:.2f}")
            return None

        new_balance = balance - position_usdt
        self.update_balance(db, new_balance, 0)

        trade = ListingTrade(
            symbol=listing["symbol"],
            pair=pair,
            listing_time=listing["published_at"],
            announcement_url=listing["url"],
            entry_price=current_price,
            entry_time=datetime.now(),
            quantity=quantity,
            position_usdt=position_usdt,
            status="open"
        )
        db.add(trade)
        db.commit()

        logger.info(f"🆕 НОВА МОНЕТА: {listing['symbol']} ({pair})")
        logger.info(f"🎯 Угода: BUY {quantity:.6f} @ ${current_price:.4f} | ${position_usdt:.2f}")

        return trade

    def check_exit(self, db: Session, trade: ListingTrade):
        """Перевірка виходу з угоди"""
        current_price = self.exchange.get_current_price(trade.pair)
        if not current_price or current_price <= 0:
            return False

        pnl_percent = ((current_price - trade.entry_price) / trade.entry_price) * 100
        hold_time = (datetime.now() - trade.entry_time).total_seconds() / 60

        if pnl_percent >= self.tp_percent:
            self.close_trade(db, trade, current_price, "TAKE_PROFIT", pnl_percent)
            return True

        if pnl_percent <= -self.sl_percent:
            self.close_trade(db, trade, current_price, "STOP_LOSS", pnl_percent)
            return True

        if hold_time >= self.hold_minutes:
            self.close_trade(db, trade, current_price, "TIME_EXIT", pnl_percent)
            return True

        return False

    def close_trade(self, db: Session, trade: ListingTrade, exit_price: float, reason: str, pnl_percent: float):
        """Закриття угоди"""
        pnl = (pnl_percent / 100) * trade.position_usdt

        trade.exit_price = exit_price
        trade.exit_time = datetime.now()
        trade.exit_reason = reason
        trade.pnl = pnl
        trade.pnl_percent = pnl_percent
        trade.status = "closed"

        balance = self.get_balance(db)
        new_balance = balance + trade.position_usdt + pnl
        self.update_balance(db, new_balance, pnl)

        db.commit()

        emoji = "✅" if pnl > 0 else "❌"
        logger.info(f"{emoji} Угоду закрито: {trade.symbol} | PnL: ${pnl:.2f} ({pnl_percent:.1f}%) | {reason}")

    async def check_new_listings(self):
        """Перевірка нових лістингів"""
        if not self.enabled:
            return

        db = SessionLocal()
        try:
            announcements = self.get_bybit_announcements()

            for announcement in announcements:
                listing_id = f"{announcement['symbol']}_{announcement['published_at'].date()}"

                if listing_id in self.processed_listings:
                    continue

                existing = db.query(ListingTrade).filter(
                    ListingTrade.symbol == announcement["symbol"],
                    ListingTrade.status == "open"
                ).first()

                if existing:
                    self.processed_listings.add(listing_id)
                    continue

                today = datetime.now().date()
                trades_today = db.query(ListingTrade).filter(
                    ListingTrade.entry_time >= datetime(today.year, today.month, today.day)
                ).count()

                if trades_today >= self.max_trades_per_day:
                    logger.debug(f"Ліміт угод на день ({self.max_trades_per_day}) досягнуто")
                    continue

                self.execute_trade(db, announcement)
                self.processed_listings.add(listing_id)

                await asyncio.sleep(1)

        except Exception as e:
            logger.error(f"Помилка перевірки лістингів: {e}")
        finally:
            db.close()

    async def check_open_trades(self):
        """Перевірка відкритих угод"""
        db = SessionLocal()
        try:
            open_trades = db.query(ListingTrade).filter(ListingTrade.status == "open").all()
            for trade in open_trades:
                self.check_exit(db, trade)
        except Exception as e:
            logger.error(f"Помилка перевірки угод: {e}")
        finally:
            db.close()

    async def run(self):
        """Головний цикл моніторингу лістингів"""
        logger.info("🆕 Монітор нових лістингів запущено")

        while self.running:
            try:
                await self.check_new_listings()
                await self.check_open_trades()
                await asyncio.sleep(120)
            except Exception as e:
                logger.error(f"Помилка циклу монітора лістингів: {e}")
                await asyncio.sleep(60)

    def stop(self):
        """Зупинка монітора"""
        self.running = False
        logger.info("🆕 Монітор нових лістингів зупинено")