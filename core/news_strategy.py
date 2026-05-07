"""
Новинна торгова стратегія
Автоматичний збір новин, аналіз тональності та торгівля
"""

import asyncio
import json
import re
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass
from enum import Enum

import aiohttp
from bs4 import BeautifulSoup
from textblob import TextBlob
from sqlalchemy.orm import Session

from db.database import SessionLocal
from db.models import NewsTrade, NewsBalance, OrderSide, OrderStatus
from exchange.bybit_client import BybitClient
from utils.logger import logger
from utils.config_loader import config


class Sentiment(Enum):
    """Типи тональності новин"""
    VERY_BULLISH = 2.0  # Дуже позитивна
    BULLISH = 1.0  # Позитивна
    NEUTRAL = 0.0  # Нейтральна
    BEARISH = -1.0  # Негативна
    VERY_BEARISH = -2.0  # Дуже негативна


@dataclass
class NewsItem:
    """Структура новини"""
    id: str
    title: str
    content: str
    source: str
    url: str
    published_at: datetime
    symbols: List[str]  # Список монет, яких стосується новина
    sentiment: Sentiment
    sentiment_score: float
    relevance_score: float  # 0-1, наскільки новина важлива для трейдингу


class NewsFetcher:
    """Збірник новин з різних джерел"""

    SOURCES = {
        "coindesk": "https://www.coindesk.com/feed",
        "cointelegraph": "https://cointelegraph.com/feed",
        "cryptopanic": "https://cryptopanic.com/api/v1/posts/",
        "binance_announcements": "https://www.binance.com/en/support/announcement/c-48",
        "bybit_announcements": "https://announcements.bybit.com/",
    }

    CRYPTO_KEYWORDS = {
        'BTC': ['bitcoin', 'btc', 'bitcoin'],
        'ETH': ['ethereum', 'eth', 'ether'],
        'SOL': ['solana', 'sol'],
        'BNB': ['binance', 'bnb'],
        'XRP': ['ripple', 'xrp'],
        'DOGE': ['dogecoin', 'doge'],
        'ADA': ['cardano', 'ada'],
        'AVAX': ['avalanche', 'avax'],
        'DOT': ['polkadot', 'dot'],
        'LINK': ['chainlink', 'link'],
    }

    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None
        self.last_fetch_time: Dict[str, datetime] = {}

    async def __aenter__(self):
        self.session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()

    async def fetch_all(self, limit: int = 50) -> List[NewsItem]:
        """Збір новин з усіх джерел"""
        all_news = []

        # Завантажуємо з RSS
        all_news.extend(await self._fetch_rss_feed(self.SOURCES["coindesk"], "CoinDesk"))
        all_news.extend(await self._fetch_rss_feed(self.SOURCES["cointelegraph"], "CoinTelegraph"))

        # Завантажуємо з CryptoPanic API
        all_news.extend(await self._fetch_cryptopanic(limit))

        # Завантажуємо оголошення бірж
        all_news.extend(await self._fetch_binance_announcements())
        all_news.extend(await self._fetch_bybit_announcements())

        # Додаємо тестові новини для демонстрації (поки немає реальних)
        if not all_news:
            all_news.extend(self._generate_demo_news())

        # Аналізуємо тональність та визначаємо символи
        for news in all_news:
            news.sentiment, news.sentiment_score = self._analyze_sentiment(news.title + " " + news.content)
            news.symbols = self._extract_symbols(news.title + " " + news.content)
            news.relevance_score = self._calculate_relevance(news)

        # Фільтруємо тільки релевантні новини (score > 0.3)
        relevant_news = [n for n in all_news if n.relevance_score > 0.3 and n.symbols]

        logger.info(f"📰 Зібрано {len(all_news)} новин, релевантних: {len(relevant_news)}")
        return relevant_news[:limit]

    async def _fetch_rss_feed(self, url: str, source: str) -> List[NewsItem]:
        """Завантаження RSS стрічки"""
        try:
            async with self.session.get(url, timeout=10) as response:
                text = await response.text()
                soup = BeautifulSoup(text, 'xml')
                items = []

                for item in soup.find_all('item')[:10]:
                    title = item.find('title').text if item.find('title') else ""
                    description = item.find('description').text if item.find('description') else ""
                    link = item.find('link').text if item.find('link') else ""
                    pub_date = item.find('pubDate').text if item.find('pubDate') else ""

                    try:
                        published_at = datetime.strptime(pub_date, '%a, %d %b %Y %H:%M:%S %z')
                    except:
                        published_at = datetime.now()

                    items.append(NewsItem(
                        id=f"{source}_{hash(title)}",
                        title=title,
                        content=description,
                        source=source,
                        url=link,
                        published_at=published_at,
                        symbols=[],
                        sentiment=Sentiment.NEUTRAL,
                        sentiment_score=0.0,
                        relevance_score=0.0
                    ))
                return items
        except Exception as e:
            logger.error(f"Помилка завантаження RSS {source}: {e}")
            return []

    async def _fetch_cryptopanic(self, limit: int) -> List[NewsItem]:
        """Завантаження новин з CryptoPanic API"""
        try:
            # CryptoPanic API (без ключа - обмежено, але працює)
            url = f"https://cryptopanic.com/api/v1/posts/?limit={limit}"
            async with self.session.get(url, timeout=10) as response:
                data = await response.json()
                items = []

                for result in data.get('results', []):
                    items.append(NewsItem(
                        id=f"cryptopanic_{result.get('id')}",
                        title=result.get('title', ''),
                        content=result.get('metadata', {}).get('description', ''),
                        source="CryptoPanic",
                        url=result.get('url', ''),
                        published_at=datetime.fromisoformat(result.get('published_at', '').replace('Z', '+00:00')),
                        symbols=[],
                        sentiment=Sentiment.NEUTRAL,
                        sentiment_score=0.0,
                        relevance_score=0.0
                    ))
                return items
        except Exception as e:
            logger.error(f"Помилка завантаження CryptoPanic: {e}")
            return []

    async def _fetch_binance_announcements(self) -> List[NewsItem]:
        """Завантаження оголошень Binance"""
        try:
            url = "https://www.binance.com/bapi/accounts/v1/private/announcement/list"
            headers = {"User-Agent": "Mozilla/5.0"}
            async with self.session.get(url, headers=headers, timeout=10) as response:
                data = await response.json()
                items = []

                for article in data.get('data', {}).get('catalogs', [])[:10]:
                    for announcement in article.get('articles', []):
                        items.append(NewsItem(
                            id=f"binance_{announcement.get('code')}",
                            title=announcement.get('title', ''),
                            content=announcement.get('summary', ''),
                            source="Binance",
                            url=f"https://www.binance.com/en/support/announcement/{announcement.get('code')}",
                            published_at=datetime.fromtimestamp(announcement.get('releaseDate', 0) / 1000),
                            symbols=[],
                            sentiment=Sentiment.NEUTRAL,
                            sentiment_score=0.0,
                            relevance_score=0.0
                        ))
                return items
        except Exception as e:
            logger.error(f"Помилка завантаження Binance: {e}")
            return []

    async def _fetch_bybit_announcements(self) -> List[NewsItem]:
        """Завантаження оголошень Bybit"""
        try:
            url = "https://announcements.bybit.com/api/posts"
            async with self.session.get(url, timeout=10) as response:
                data = await response.json()
                items = []

                for post in data.get('data', {}).get('posts', [])[:10]:
                    items.append(NewsItem(
                        id=f"bybit_{post.get('id')}",
                        title=post.get('title', ''),
                        content=post.get('excerpt', ''),
                        source="Bybit",
                        url=post.get('link', ''),
                        published_at=datetime.fromisoformat(post.get('date', '').replace('Z', '+00:00')),
                        symbols=[],
                        sentiment=Sentiment.NEUTRAL,
                        sentiment_score=0.0,
                        relevance_score=0.0
                    ))
                return items
        except Exception as e:
            logger.error(f"Помилка завантаження Bybit: {e}")
            return []

    def _generate_demo_news(self) -> List[NewsItem]:
        """Генерація тестових новин для демонстрації"""
        demo_news = [
            {
                "title": "🚀 Bitcoin пробиває $70,000! Інституційний інтерес зростає",
                "content": "BlackRock та Fidelity збільшили свої позиції в BTC. Очікується подальше зростання.",
                "source": "Demo",
                "url": "#",
                "symbol": "BTC"
            },
            {
                "title": "📉 Ethereum під тиском: Gas fees зростають, мережа перевантажена",
                "content": "Високі комісії можуть відлякати користувачів. Ринок очікує корекцію.",
                "source": "Demo",
                "url": "#",
                "symbol": "ETH"
            },
            {
                "title": "🔔 Solana оголошує про нове партнерство з Visa",
                "content": "Інтеграція Solana з платіжною системою Visa. Позитивний вплив на ціну SOL.",
                "source": "Demo",
                "url": "#",
                "symbol": "SOL"
            },
            {
                "title": "⚠️ Регулятори SEC розпочинають розслідування проти Binance",
                "content": "Новий тиск на криптоіндустрію. Ринок може відреагувати негативно.",
                "source": "Demo",
                "url": "#",
                "symbol": "BNB"
            },
            {
                "title": "💎 Dogecoin додано на Robinhood для всіх користувачів EU",
                "content": "Розширення доступу до DOGE. Очікується зростання об'ємів торгів.",
                "source": "Demo",
                "url": "#",
                "symbol": "DOGE"
            }
        ]

        items = []
        for news in demo_news:
            items.append(NewsItem(
                id=f"demo_{hash(news['title'])}",
                title=news['title'],
                content=news['content'],
                source=news['source'],
                url=news['url'],
                published_at=datetime.now(),
                symbols=[news['symbol']],
                sentiment=Sentiment.NEUTRAL,
                sentiment_score=0.0,
                relevance_score=0.0
            ))
        return items

    def _analyze_sentiment(self, text: str) -> Tuple[Sentiment, float]:
        """Аналіз тональності тексту"""
        # Словники ключових слів для посилення аналізу
        bullish_words = ['зростання', 'рості', 'бичачий', 'позитивний', 'перемога', 'партнерство',
                         'інтеграція', 'запуск', 'схвалення', 'збільшення', 'прорив', 'новий максимум',
                         'bullish', 'surge', 'pump', 'moon', 'partnership', 'launch', 'approval']

        bearish_words = ['падіння', 'зниження', 'ведмежий', 'негативний', 'розслідування', 'заборона',
                         'вразливість', 'злом', 'продаж', 'злив', 'корекція', 'ведмежий ринок',
                         'bearish', 'dump', 'crash', 'ban', 'investigation', 'hack', 'sell-off']

        text_lower = text.lower()

        # Підрахунок ключових слів
        bullish_count = sum(1 for word in bullish_words if word in text_lower)
        bearish_count = sum(1 for word in bearish_words if word in text_lower)

        # Додатковий аналіз за допомогою TextBlob
        try:
            blob = TextBlob(text)
            blob_sentiment = blob.sentiment.polarity
        except:
            blob_sentiment = 0.0

        # Комбінований скоринг
        keyword_score = (bullish_count - bearish_count) / max(bullish_count + bearish_count, 1)
        final_score = (keyword_score * 0.7 + blob_sentiment * 0.3)

        # Визначення Sentiment
        if final_score >= 1.5:
            return Sentiment.VERY_BULLISH, final_score
        elif final_score >= 0.5:
            return Sentiment.BULLISH, final_score
        elif final_score <= -1.5:
            return Sentiment.VERY_BEARISH, final_score
        elif final_score <= -0.5:
            return Sentiment.BEARISH, final_score
        else:
            return Sentiment.NEUTRAL, final_score

    def _extract_symbols(self, text: str) -> List[str]:
        """Витягування символів монет з тексту"""
        found_symbols = []
        text_lower = text.lower()

        for symbol, keywords in self.CRYPTO_KEYWORDS.items():
            for keyword in keywords:
                if keyword in text_lower:
                    found_symbols.append(symbol)
                    break

        return list(set(found_symbols))

    def _calculate_relevance(self, news: NewsItem) -> float:
        """Розрахунок релевантності новини (0-1)"""
        score = 0.0

        # Вага джерела
        source_weights = {
            "Binance": 1.0,
            "Bybit": 1.0,
            "CoinDesk": 0.8,
            "CoinTelegraph": 0.8,
            "CryptoPanic": 0.7,
            "Demo": 0.5
        }
        score += source_weights.get(news.source, 0.5) * 0.3

        # Вага знайдених символів
        score += min(len(news.symbols), 3) * 0.2

        # Вага тональності (чим сильніша тональність, тим релевантніше)
        sentiment_weight = abs(news.sentiment.value) / 2 * 0.3
        score += sentiment_weight

        # Вага свіжості (що новіша, то релевантніше)
        hours_ago = (datetime.now() - news.published_at).total_seconds() / 3600
        freshness = max(0, 1 - hours_ago / 24)  # 1 для нових, 0 для старіших 24 годин
        score += freshness * 0.2

        return min(score, 1.0)


class NewsTradingEngine:
    """Рушій торгівлі за новинами"""

    def __init__(self, order_manager=None):
        self.order_manager = order_manager
        self.exchange = BybitClient()
        self.fetcher = NewsFetcher()
        self.processed_news_ids = set()  # Для запобігання дублікатів
        self.running = True

        # Параметри з конфігу
        self.position_percent = config.get('news_strategy.position_percent', 5.0)  # % від балансу
        self.max_position_usdt = config.get('news_strategy.max_position_usdt', 100.0)
        self.hold_minutes = config.get('news_strategy.hold_minutes', 30)  # час утримання
        self.take_profit_percent = config.get('news_strategy.take_profit_percent', 5.0)
        self.stop_loss_percent = config.get('news_strategy.stop_loss_percent', 3.0)
        self.min_sentiment_score = config.get('news_strategy.min_sentiment_score', 0.6)
        self.min_relevance_score = config.get('news_strategy.min_relevance_score', 0.5)

    async def get_balance(self) -> float:
        """Отримання балансу новинної стратегії"""
        db = SessionLocal()
        try:
            balance = db.query(NewsBalance).first()
            if not balance:
                balance = NewsBalance(amount=100.0, initial_balance=100.0)
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
            balance = db.query(NewsBalance).first()
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

    async def open_trade(self, news: NewsItem) -> Optional[Dict]:
        """Відкриття угоди на основі новини"""
        # Перевіряємо, чи вже обробляли цю новину
        if news.id in self.processed_news_ids:
            return None

        # Перевіряємо релевантність
        if news.relevance_score < self.min_relevance_score:
            logger.debug(f"Новина {news.title[:50]}... недостатньо релевантна ({news.relevance_score})")
            return None

        # Визначаємо сторону угоди
        if news.sentiment in [Sentiment.VERY_BULLISH, Sentiment.BULLISH]:
            side = OrderSide.BUY
            signal_type = "LONG"
        elif news.sentiment in [Sentiment.VERY_BEARISH, Sentiment.BEARISH]:
            side = OrderSide.SELL
            signal_type = "SHORT"
        else:
            return None  # Пропускаємо нейтральні новини

        # Перевіряємо силу сигналу
        if abs(news.sentiment.value) < self.min_sentiment_score:
            logger.debug(f"Тональність {news.sentiment.value} занадто слабка")
            return None

        # Розраховуємо розмір позиції для кожної монети
        trades_opened = []
        position_usdt = await self.calculate_position_size()

        for symbol in news.symbols:
            pair = f"{symbol}USDT"

            # Отримуємо поточну ціну
            current_price = self.exchange.get_current_price(pair)
            if not current_price:
                continue

            quantity = position_usdt / current_price

            # Створюємо угоду через OrderManager (якщо є)
            trade_id = None
            if self.order_manager:
                trade = await self.order_manager.open_paper_trade(
                    pair=pair,
                    side=side,
                    entry_price=current_price,
                    quantity=quantity,
                    take_profit=current_price * (
                                1 + self.take_profit_percent / 100) if side == OrderSide.BUY else current_price * (
                                1 - self.take_profit_percent / 100),
                    stop_loss=current_price * (
                                1 - self.stop_loss_percent / 100) if side == OrderSide.BUY else current_price * (
                                1 + self.stop_loss_percent / 100)
                )
                if trade:
                    trade_id = trade.id

            # Зберігаємо в БД новин
            db = SessionLocal()
            try:
                news_trade = NewsTrade(
                    title=news.title[:500],
                    pair=pair,
                    side=signal_type,
                    sentiment_score=news.sentiment_score,
                    entry_price=current_price,
                    quantity=quantity,
                    position_usdt=position_usdt,
                    status="open",
                    entry_time=datetime.now(),
                    exit_reason=None
                )
                db.add(news_trade)
                db.commit()

                trades_opened.append({
                    "symbol": symbol,
                    "pair": pair,
                    "side": signal_type,
                    "entry_price": current_price,
                    "position_usdt": position_usdt,
                    "trade_id": trade_id,
                    "news_trade_id": news_trade.id
                })

                logger.info(f"📰 ВІДКРИТО УГОДУ ЗА НОВИНОЮ: {pair} {signal_type} | ${position_usdt:.2f} | "
                            f"Тональність: {news.sentiment.value} | {news.title[:80]}...")

            finally:
                db.close()

        # Позначаємо новину як оброблену
        self.processed_news_ids.add(news.id)

        # Запускаємо таймер для закриття угоди
        if trades_opened:
            asyncio.create_task(self._auto_close_trades(trades_opened))

        return {"trades": trades_opened, "news": news}

    async def _auto_close_trades(self, trades: List[Dict]):
        """Автоматичне закриття угод через заданий час"""
        await asyncio.sleep(self.hold_minutes * 60)

        for trade in trades:
            await self.close_trade(trade['news_trade_id'])

    async def close_trade(self, news_trade_id: int, current_price: float = None):
        """Закриття угоди"""
        db = SessionLocal()
        try:
            news_trade = db.query(NewsTrade).filter(NewsTrade.id == news_trade_id).first()
            if not news_trade or news_trade.status != "open":
                return

            # Отримуємо поточну ціну
            if not current_price:
                current_price = self.exchange.get_current_price(news_trade.pair)
                if not current_price:
                    return

            # Розраховуємо PnL
            if news_trade.side == "LONG":
                pnl = (current_price - news_trade.entry_price) * news_trade.quantity
                pnl_percent = ((current_price - news_trade.entry_price) / news_trade.entry_price) * 100
            else:
                pnl = (news_trade.entry_price - current_price) * news_trade.quantity
                pnl_percent = ((news_trade.entry_price - current_price) / news_trade.entry_price) * 100

            # Оновлюємо запис
            news_trade.exit_price = current_price
            news_trade.exit_time = datetime.now()
            news_trade.pnl = pnl
            news_trade.pnl_percent = pnl_percent
            news_trade.status = "closed"
            news_trade.exit_reason = f"auto_close_{self.hold_minutes}min"

            # Оновлюємо баланс
            await self.update_balance(pnl)

            db.commit()

            logger.info(f"📰 ЗАКРИТО УГОДУ ЗА НОВИНОЮ: {news_trade.pair} | PnL: ${pnl:.2f} ({pnl_percent:.1f}%)")

        finally:
            db.close()

    async def check_and_close_expired(self):
        """Перевірка та закриття прострочених угод"""
        db = SessionLocal()
        try:
            expired_time = datetime.now() - timedelta(hours=self.hold_minutes / 60)
            open_trades = db.query(NewsTrade).filter(
                NewsTrade.status == "open",
                NewsTrade.entry_time < expired_time
            ).all()

            for trade in open_trades:
                await self.close_trade(trade.id)

        finally:
            db.close()

    async def run_once(self) -> int:
        """Один цикл збору новин та торгівлі"""
        trades_opened = 0

        async with self.fetcher:
            news_list = await self.fetcher.fetch_all(limit=20)

            for news in news_list:
                # Перевіряємо, чи варто торгувати
                if news.relevance_score >= self.min_relevance_score:
                    if news.sentiment != Sentiment.NEUTRAL:
                        result = await self.open_trade(news)
                        if result and result.get('trades'):
                            trades_opened += len(result['trades'])

        # Перевіряємо прострочені угоди
        await self.check_and_close_expired()

        return trades_opened

    async def run(self):
        """Основний цикл стратегії"""
        logger.info("📰 Новинна стратегія запущена")

        while self.running:
            try:
                trades = await self.run_once()
                if trades > 0:
                    logger.info(f"📰 Відкрито {trades} угод за новинами")

                # Очікуємо перед наступним циклом (перевірка кожні 60 секунд)
                await asyncio.sleep(60)

            except Exception as e:
                logger.error(f"Помилка в новинній стратегії: {e}")
                await asyncio.sleep(30)

        logger.info("📰 Новинна стратегія зупинена")

    def stop(self):
        """Зупинка стратегії"""
        self.running = False