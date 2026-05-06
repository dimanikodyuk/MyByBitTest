"""
Торгівля на основі новин з NewsAPI
Віртуальний портфель $100
"""

import asyncio
import requests
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from textblob import TextBlob
from sqlalchemy.orm import Session
from db.database import SessionLocal
from db.news_models import NewsTrade, NewsBalance
from exchange.bybit_client import BybitClient
from utils.config_loader import config
from utils.logger import logger


class NewsTrader:
    """Торгівля на основі новин"""

    def __init__(self):
        self.api_key = config.news_api_key
        self.exchange = BybitClient()
        self.enabled = config.get('news_trading.enabled', True)
        self.initial_balance = config.get('news_trading.initial_balance', 100.0)
        self.position_percent = config.get('news_trading.position_percent', 25.0)
        self.hold_minutes = config.get('news_trading.hold_minutes', 60)
        self.max_trades_per_day = config.get('news_trading.max_trades_per_day', 10)

        # Ключові слова
        self.bullish_keywords = config.get('news_trading.bullish_keywords', [
            "partnership", "integration", "listing", "launch", "upgrade",
            "announcement", "collaboration", "acquisition", "mainnet", "futures"
        ])
        self.bearish_keywords = config.get('news_trading.bearish_keywords', [
            "hack", "exploit", "delay", "lawsuit", "regulator",
            "ban", "suspend", "investigation", "vulnerability"
        ])

        self.running = True

    def get_balance(self, db: Session) -> float:
        """Отримання поточного балансу"""
        balance = db.query(NewsBalance).first()
        if not balance:
            balance = NewsBalance(amount=self.initial_balance, initial_balance=self.initial_balance)
            db.add(balance)
            db.commit()
            db.refresh(balance)
        return balance.amount

    def update_balance(self, db: Session, new_balance: float, pnl: float = 0):
        """Оновлення балансу"""
        balance = db.query(NewsBalance).first()
        if not balance:
            balance = NewsBalance(amount=new_balance, initial_balance=self.initial_balance)
            db.add(balance)
        else:
            balance.amount = new_balance
            balance.total_pnl += pnl
            balance.total_trades += 1
            if pnl > 0:
                balance.win_trades += 1
        db.commit()

    def analyze_sentiment(self, title: str, description: str = "") -> Tuple[float, str]:
        """
        Аналіз тональності новини
        Повертає (score, side) де score від -1 до 1, side: LONG/SHORT/NEUTRAL
        """
        text = f"{title} {description}".lower()

        # Пошук ключових слів
        bullish_score = 0
        bearish_score = 0

        for keyword in self.bullish_keywords:
            if keyword in text:
                bullish_score += 1

        for keyword in self.bearish_keywords:
            if keyword in text:
                bearish_score += 1

        # TextBlob аналіз (якщо доступний)
        try:
            blob = TextBlob(f"{title}. {description}")
            sentiment = blob.sentiment.polarity  # -1 до 1
        except:
            sentiment = 0

        # Комбінований score
        keyword_score = (bullish_score - bearish_score) / max(len(self.bullish_keywords), 1)
        final_score = (sentiment + keyword_score) / 2
        final_score = max(-1, min(1, final_score))  # Обмежуємо

        if final_score > 0.3:
            return final_score, "LONG"
        elif final_score < -0.3:
            return final_score, "SHORT"
        else:
            return final_score, "NEUTRAL"

    def get_crypto_news(self, limit: int = 20) -> List[Dict]:
        """Отримання крипто-новин з NewsAPI"""
        if not self.api_key:
            logger.warning("NewsAPI ключ не налаштовано")
            return []

        try:
            url = "https://newsapi.org/v2/everything"
            params = {
                "q": "cryptocurrency OR bitcoin OR ethereum OR blockchain",
                "language": "en",
                "sortBy": "publishedAt",
                "pageSize": limit,
                "apiKey": self.api_key
            }

            response = requests.get(url, params=params, timeout=10)
            data = response.json()

            if data.get("status") == "ok":
                articles = data.get("articles", [])
                return [
                    {
                        "title": a.get("title", ""),
                        "description": a.get("description", ""),
                        "url": a.get("url", ""),
                        "source": a.get("source", {}).get("name", ""),
                        "published_at": datetime.strptime(a.get("publishedAt", ""), "%Y-%m-%dT%H:%M:%SZ") if a.get(
                            "publishedAt") else datetime.now()
                    }
                    for a in articles
                ]
            else:
                logger.error(f"NewsAPI помилка: {data.get('message')}")
                return []

        except Exception as e:
            logger.error(f"Помилка отримання новин: {e}")
            return []

    def get_coin_from_news(self, text: str) -> Optional[str]:
        """Визначення криптовалюти з тексту новини"""
        coins = {
            "bitcoin": "BTCUSDT",
            "btc": "BTCUSDT",
            "ethereum": "ETHUSDT",
            "eth": "ETHUSDT",
            "solana": "SOLUSDT",
            "sol": "SOLUSDT",
            "dogecoin": "DOGEUSDT",
            "doge": "DOGEUSDT",
            "cardano": "ADAUSDT",
            "ada": "ADAUSDT",
            "ripple": "XRPUSDT",
            "xrp": "XRPUSDT",
            "polkadot": "DOTUSDT",
            "dot": "DOTUSDT",
            "litecoin": "LTCUSDT",
            "ltc": "LTCUSDT",
            "chainlink": "LINKUSDT",
            "link": "LINKUSDT"
        }

        text_lower = text.lower()
        for name, pair in coins.items():
            if name in text_lower:
                return pair
        return None

    def execute_trade(self, db: Session, news: Dict, pair: str, side: str, sentiment_score: float):
        """Виконання угоди на основі новини"""
        balance = self.get_balance(db)
        position_usdt = balance * (self.position_percent / 100)

        if position_usdt < 10:
            logger.warning(f"Недостатньо балансу для угоди: {balance:.2f} USDT")
            return None

        # Отримуємо поточну ціну
        current_price = self.exchange.get_current_price(pair)
        if not current_price:
            logger.error(f"Не вдалося отримати ціну для {pair}")
            return None

        quantity = position_usdt / current_price

        # Мінімальна кількість
        min_qty = 0.001
        if pair == "BTCUSDT":
            min_qty = 0.0001
        elif pair == "DOGEUSDT":
            min_qty = 1.0

        if quantity < min_qty:
            quantity = min_qty
            position_usdt = quantity * current_price

        # Блокуємо кошти
        new_balance = balance - position_usdt
        self.update_balance(db, new_balance, 0)  # Тимчасово оновлюємо баланс

        # Створюємо угоду
        trade = NewsTrade(
            news_id=news.get("url", "")[:100],
            title=news.get("title", "")[:500],
            content=news.get("description", "")[:1000],
            source=news.get("source", ""),
            published_at=news.get("published_at", datetime.now()),
            pair=pair,
            side=side,
            sentiment_score=sentiment_score,
            confidence=abs(sentiment_score) * 100,
            entry_price=current_price,
            entry_time=datetime.now(),
            quantity=quantity,
            position_usdt=position_usdt,
            status="open"
        )
        db.add(trade)
        db.commit()

        logger.info(f"📰 НОВИНА: {news.get('title', '')[:100]}...")
        logger.info(
            f"🎯 Угода: {side} {pair} | {quantity} @ ${current_price:.2f} | ${position_usdt:.2f} | Впевненість: {abs(sentiment_score) * 100:.0f}%")

        return trade

    def check_exit(self, db: Session, trade: NewsTrade):
        """Перевірка виходу з угоди"""
        current_price = self.exchange.get_current_price(trade.pair)
        if not current_price:
            return False

        hold_time = (datetime.now() - trade.entry_time).total_seconds() / 60

        # Вихід за часом
        if hold_time >= self.hold_minutes:
            self.close_trade(db, trade, current_price, "TIME_EXIT")
            return True

        return False

    def close_trade(self, db: Session, trade: NewsTrade, exit_price: float, reason: str):
        """Закриття угоди"""
        if trade.side == "LONG":
            pnl = (exit_price - trade.entry_price) * trade.quantity
        else:
            pnl = (trade.entry_price - exit_price) * trade.quantity

        pnl_percent = (pnl / trade.position_usdt) * 100 if trade.position_usdt > 0 else 0

        trade.exit_price = exit_price
        trade.exit_time = datetime.now()
        trade.exit_reason = reason
        trade.pnl = pnl
        trade.pnl_percent = pnl_percent
        trade.status = "closed"

        # Оновлюємо баланс (повертаємо кошти + прибуток/збиток)
        balance = self.get_balance(db)
        new_balance = balance + trade.position_usdt + pnl
        self.update_balance(db, new_balance, pnl)

        db.commit()

        logger.info(
            f"🔒 Угоду закрито: {trade.side} {trade.pair} | PnL: ${pnl:.2f} ({pnl_percent:.1f}%) | Причина: {reason}")

    async def process_news(self):
        """Обробка новин та створення угод"""
        if not self.enabled:
            return

        db = SessionLocal()
        try:
            # Отримуємо новини
            news_list = self.get_crypto_news(limit=10)

            for news in news_list:
                # Перевіряємо чи вже обробляли
                existing = db.query(NewsTrade).filter(NewsTrade.news_id == news.get("url", "")[:100]).first()
                if existing:
                    continue

                # Аналізуємо тональність
                sentiment_score, side = self.analyze_sentiment(
                    news.get("title", ""),
                    news.get("description", "")
                )

                if side == "NEUTRAL":
                    continue

                # Визначаємо криптовалюту
                text = f"{news.get('title', '')} {news.get('description', '')}"
                pair = self.get_coin_from_news(text)

                if not pair:
                    # Якщо не визначили - використовуємо BTCUSDT
                    pair = "BTCUSDT"

                # Перевіряємо ліміт угод за день
                today = datetime.now().date()
                trades_today = db.query(NewsTrade).filter(
                    NewsTrade.entry_time >= today
                ).count()

                if trades_today >= self.max_trades_per_day:
                    logger.debug(f"Ліміт угод на день ({self.max_trades_per_day}) досягнуто")
                    continue

                # Перевіряємо чи є відкриті позиції
                open_trades = db.query(NewsTrade).filter(NewsTrade.status == "open").count()
                if open_trades >= 3:  # максимум 3 одночасні угоди
                    logger.debug("Забагато відкритих позицій")
                    continue

                # Виконуємо угоду
                self.execute_trade(db, news, pair, side, sentiment_score)

                # Невелика затримка між угодами
                await asyncio.sleep(1)

        except Exception as e:
            logger.error(f"Помилка обробки новин: {e}")
        finally:
            db.close()

    async def check_open_trades(self):
        """Перевірка відкритих угод"""
        db = SessionLocal()
        try:
            open_trades = db.query(NewsTrade).filter(NewsTrade.status == "open").all()
            for trade in open_trades:
                self.check_exit(db, trade)
        except Exception as e:
            logger.error(f"Помилка перевірки угод: {e}")
        finally:
            db.close()

    async def run(self):
        """Головний цикл новинної стратегії"""
        logger.info("📰 Новиний трейдер запущено")

        while self.running:
            try:
                # Обробка новин кожні 5 хвилин
                await self.process_news()

                # Перевірка відкритих угод кожну хвилину
                await self.check_open_trades()

                await asyncio.sleep(300)  # 5 хвилин

            except Exception as e:
                logger.error(f"Помилка циклу новинного трейдера: {e}")
                await asyncio.sleep(60)

    def stop(self):
        """Зупинка стратегії"""
        self.running = False
        logger.info("📰 Новиний трейдер зупинено")