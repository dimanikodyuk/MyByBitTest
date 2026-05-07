"""
Стратегія торгівлі новими монетами (лістингами)
Автоматичне виявлення нових пар та вхід в позиції
"""

import asyncio
import re
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Set
from dataclasses import dataclass

from sqlalchemy.orm import Session

from db.database import SessionLocal
from db.models import ListingTrade, ListingBalance, OrderSide, OrderStatus
from exchange.bybit_client import BybitClient
from utils.logger import logger
from utils.config_loader import config


@dataclass
class NewListing:
    """Структура нового лістингу"""
    symbol: str
    pair: str
    exchange: str
    listed_at: datetime
    base_asset: str
    quote_asset: str
    initial_price: float
    initial_volume: float
    liquidity_score: float  # 0-1
    volatility_score: float  # 0-1
    entry_signal: bool


class ListingMonitor:
    """Моніторинг нових лістингів на біржах"""

    # Ключові слова для нових пар (фільтр)
    KNOWN_TOKENS = ['BTC', 'ETH', 'SOL', 'BNB', 'XRP', 'DOGE', 'ADA', 'AVAX',
                    'DOT', 'LINK', 'MATIC', 'UNI', 'ATOM', 'LTC', 'ETC',
                    'NEAR', 'APT', 'ARB', 'OP', 'SUI', 'SEI', 'TIA', 'INJ']

    def __init__(self):
        self.exchange = BybitClient()
        self.known_pairs: Set[str] = set()
        self.last_check_time: Optional[datetime] = None

        # Завантажуємо відомі пари при старті
        self._load_known_pairs()

    def _load_known_pairs(self):
        """Завантаження списку відомих пар"""
        try:
            # Отримуємо всі пари з Bybit
            tickers = self.exchange.get_all_tickers()
            self.known_pairs = {t['symbol'] for t in tickers if t['symbol'].endswith('USDT')}
            logger.info(f"📊 Завантажено {len(self.known_pairs)} відомих пар")
        except Exception as e:
            logger.error(f"Помилка завантаження пар: {e}")
            # Стандартний набір пар
            self.known_pairs = {'BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT', 'XRPUSDT',
                                'DOGEUSDT', 'ADAUSDT', 'AVAXUSDT', 'DOTUSDT', 'LINKUSDT',
                                'MATICUSDT', 'UNIUSDT', 'ATOMUSDT', 'LTCUSDT', 'ETCUSDT'}

    async def check_new_listings(self) -> List[NewListing]:
        """Перевірка нових лістингів"""
        try:
            # Отримуємо поточний список всіх пар
            tickers = self.exchange.get_all_tickers()
            current_pairs = {t['symbol'] for t in tickers if t['symbol'].endswith('USDT')}

            # Знаходимо нові пари
            new_pairs = current_pairs - self.known_pairs

            if not new_pairs:
                return []

            logger.info(f"🆕 Виявлено {len(new_pairs)} нових пар: {new_pairs}")

            listings = []
            for pair in new_pairs:
                # Отримуємо дані про нову пару
                listing_info = await self._analyze_new_pair(pair)
                if listing_info and listing_info.entry_signal:
                    listings.append(listing_info)

            # Оновлюємо список відомих пар
            self.known_pairs = current_pairs
            self.last_check_time = datetime.now()

            return listings

        except Exception as e:
            logger.error(f"Помилка перевірки нових лістингів: {e}")
            return []

    async def _analyze_new_pair(self, pair: str) -> Optional[NewListing]:
        """Аналіз нової пари для визначення доцільності входу"""
        try:
            # Отримуємо свічки за останні 10 хвилин
            klines = self.exchange.get_klines(pair, '1m', limit=10)
            if klines is None or klines.empty:
                return None

            # Витягуємо символ
            symbol = pair.replace('USDT', '')

            # Об'єм торгів
            volumes = klines['volume'].tolist()
            avg_volume = sum(volumes[:5]) / 5 if len(volumes) >= 5 else sum(volumes) / len(volumes)
            current_volume = volumes[-1] if volumes else 0

            # Цінова динаміка
            closes = klines['close'].tolist()
            price_change = ((closes[-1] - closes[0]) / closes[0] * 100) if closes else 0

            # Оцінка ліквідності (на основі об'єму та спреду)
            liquidity_score = min(1.0, current_volume / 100000) if current_volume > 0 else 0

            # Оцінка волатильності
            if len(closes) > 1:
                changes = [abs((closes[i] - closes[i - 1]) / closes[i - 1] * 100) for i in range(1, len(closes))]
                volatility_score = min(1.0, sum(changes) / len(changes) / 10)
            else:
                volatility_score = 0

            # Сигнал на вхід
            entry_signal = (
                    liquidity_score > 0.3 and  # Достатня ліквідність
                    volatility_score > 0.1 and  # Висока волатильність (характерно для нових монет)
                    price_change > 0  # Ціна зростає після лістингу
            )

            if entry_signal:
                logger.info(f"🆕 Нова монета {pair}: об'єм={current_volume:.0f}, "
                            f"зміна={price_change:.1f}%, ліквідність={liquidity_score:.2f}")

            return NewListing(
                symbol=symbol,
                pair=pair,
                exchange="Bybit",
                listed_at=datetime.now(),
                base_asset=symbol,
                quote_asset="USDT",
                initial_price=closes[-1] if closes else 0,
                initial_volume=current_volume,
                liquidity_score=liquidity_score,
                volatility_score=volatility_score,
                entry_signal=entry_signal
            )

        except Exception as e:
            logger.error(f"Помилка аналізу пари {pair}: {e}")
            return None


class ListingTradingEngine:
    """Рушій торгівлі новими монетами"""

    def __init__(self, order_manager=None):
        self.order_manager = order_manager
        self.exchange = BybitClient()
        self.monitor = ListingMonitor()
        self.processed_listings: Set[str] = set()
        self.running = True

        # Параметри з конфігу
        self.position_percent = config.get('listing_strategy.position_percent', 3.0)  # % від балансу
        self.max_position_usdt = config.get('listing_strategy.max_position_usdt', 50.0)
        self.take_profit_percent = config.get('listing_strategy.take_profit_percent', 20.0)  # Агресивний TP
        self.stop_loss_percent = config.get('listing_strategy.stop_loss_percent', 10.0)  # Агресивний SL
        self.hold_minutes = config.get('listing_strategy.hold_minutes', 120)  # 2 години
        self.min_liquidity_score = config.get('listing_strategy.min_liquidity_score', 0.3)

    async def get_balance(self) -> float:
        """Отримання балансу стратегії нових монет"""
        db = SessionLocal()
        try:
            balance = db.query(ListingBalance).first()
            if not balance:
                balance = ListingBalance(amount=100.0, initial_balance=100.0)
                db.add(balance)
                db.commit()
                db.refresh(balance)
            return balance.amount
        finally:
            db.close()

    async def update_balance(self, pnl: float):
        """Оновлення балансу після угоди"""
        db = SessionLocal()
        try:
            balance = db.query(ListingBalance).first()
            if balance:
                balance.amount += pnl
                balance.total_pnl += pnl
                balance.total_trades += 1
                if pnl > 0:
                    balance.win_trades += 1
                db.commit()
        finally:
            db.close()

    async def calculate_position_size(self) -> float:
        """Розрахунок розміру позиції"""
        balance = await self.get_balance()
        position = min(
            balance * (self.position_percent / 100),
            self.max_position_usdt
        )
        return max(position, 10.0)  # Мінімум $10

    async def enter_position(self, listing: NewListing) -> Optional[Dict]:
        """Вхід в позицію нової монети"""
        # Перевіряємо, чи вже входили
        if listing.pair in self.processed_listings:
            return None

        # Перевіряємо ліквідність
        if listing.liquidity_score < self.min_liquidity_score:
            logger.debug(f"Ліквідність {listing.pair} занадто низька ({listing.liquidity_score})")
            return None

        # В нові монети зазвичай входимо тільки в LONG (купівля)
        side = OrderSide.BUY
        signal_type = "LONG"

        # Розраховуємо розмір позиції
        position_usdt = await self.calculate_position_size()
        quantity = position_usdt / listing.initial_price if listing.initial_price > 0 else 0

        if quantity == 0:
            return None

        # Створюємо угоду через OrderManager (якщо є)
        trade_id = None
        if self.order_manager:
            trade = await self.order_manager.open_paper_trade(
                pair=listing.pair,
                side=side,
                entry_price=listing.initial_price,
                quantity=quantity,
                take_profit=listing.initial_price * (1 + self.take_profit_percent / 100),
                stop_loss=listing.initial_price * (1 - self.stop_loss_percent / 100)
            )
            if trade:
                trade_id = trade.id

        # Зберігаємо в БД
        db = SessionLocal()
        try:
            listing_trade = ListingTrade(
                symbol=listing.symbol,
                pair=listing.pair,
                exchange=listing.exchange,
                entry_price=listing.initial_price,
                quantity=quantity,
                position_usdt=position_usdt,
                status="open",
                entry_time=datetime.now(),
                exit_reason=None
            )
            db.add(listing_trade)
            db.commit()

            self.processed_listings.add(listing.pair)

            logger.info(f"🆕 ВІДКРИТО ПОЗИЦІЮ НОВОЇ МОНЕТИ: {listing.pair} | "
                        f"${position_usdt:.2f} | Об'єм: {listing.initial_volume:.0f} | "
                        f"Ліквідність: {listing.liquidity_score:.2f}")

            # Запускаємо таймер для закриття
            asyncio.create_task(self._auto_close_position(listing_trade.id))

            return {
                "trade_id": trade_id,
                "listing_trade_id": listing_trade.id,
                "pair": listing.pair,
                "symbol": listing.symbol,
                "entry_price": listing.initial_price,
                "position_usdt": position_usdt
            }

        finally:
            db.close()

    async def _auto_close_position(self, listing_trade_id: int):
        """Автоматичне закриття позиції через заданий час"""
        await asyncio.sleep(self.hold_minutes * 60)
        await self.close_position(listing_trade_id, reason=f"auto_close_{self.hold_minutes}min")

    async def close_position(self, listing_trade_id: int, current_price: float = None, reason: str = "manual"):
        """Закриття позиції"""
        db = SessionLocal()
        try:
            listing_trade = db.query(ListingTrade).filter(ListingTrade.id == listing_trade_id).first()
            if not listing_trade or listing_trade.status != "open":
                return

            # Отримуємо поточну ціну
            if not current_price:
                current_price = self.exchange.get_current_price(listing_trade.pair)
                if not current_price:
                    return

            # Розраховуємо PnL
            pnl = (current_price - listing_trade.entry_price) * listing_trade.quantity
            pnl_percent = ((current_price - listing_trade.entry_price) / listing_trade.entry_price) * 100

            # Оновлюємо запис
            listing_trade.exit_price = current_price
            listing_trade.exit_time = datetime.now()
            listing_trade.pnl = pnl
            listing_trade.pnl_percent = pnl_percent
            listing_trade.status = "closed"
            listing_trade.exit_reason = reason

            # Оновлюємо баланс
            await self.update_balance(pnl)

            db.commit()

            logger.info(f"🆕 ЗАКРИТО ПОЗИЦІЮ {listing_trade.pair} | PnL: ${pnl:.2f} ({pnl_percent:.1f}%) | {reason}")

        finally:
            db.close()

    async def check_and_close_all(self):
        """Перевірка та закриття всіх прострочених позицій"""
        db = SessionLocal()
        try:
            expired_time = datetime.now() - timedelta(hours=self.hold_minutes / 60)
            open_trades = db.query(ListingTrade).filter(
                ListingTrade.status == "open",
                ListingTrade.entry_time < expired_time
            ).all()

            for trade in open_trades:
                await self.close_position(trade.id, reason=f"expired_{self.hold_minutes}min")

        finally:
            db.close()

    async def run_once(self) -> int:
        """Один цикл моніторингу та торгівлі"""
        positions_opened = 0

        # Перевіряємо нові лістинги
        new_listings = await self.monitor.check_new_listings()

        for listing in new_listings:
            if listing.entry_signal:
                result = await self.enter_position(listing)
                if result:
                    positions_opened += 1

        # Перевіряємо прострочені позиції
        await self.check_and_close_all()

        return positions_opened

    async def run(self):
        """Основний цикл стратегії"""
        logger.info("🆕 Стратегія нових монет запущена")

        while self.running:
            try:
                positions = await self.run_once()
                if positions > 0:
                    logger.info(f"🆕 Відкрито {positions} позицій нових монет")

                # Перевіряємо кожні 30 секунд (швидше для нових монет)
                await asyncio.sleep(30)

            except Exception as e:
                logger.error(f"Помилка в стратегії нових монет: {e}")
                await asyncio.sleep(30)

        logger.info("🆕 Стратегія нових монет зупинена")

    def stop(self):
        """Зупинка стратегії"""
        self.running = False