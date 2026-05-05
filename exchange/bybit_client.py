from pybit.unified_trading import HTTP
from utils.config_loader import config
from utils.logger import logger
import pandas as pd
import time
import json
import threading
from typing import Dict, List, Callable, Optional
from collections import defaultdict


class BybitClient:
    """Клієнт для роботи з Bybit API (REST + WebSocket)"""

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialize()
        return cls._instance

    def _initialize(self):
        """Ініціалізація клієнта"""
        self.api_key = config.api_key
        self.api_secret = config.api_secret
        self.testnet = config.get('exchange.testnet', False)

        # REST клієнт
        self.session = HTTP(
            api_key=self.api_key,
            api_secret=self.api_secret,
            testnet=self.testnet
        )

        # WebSocket з'єднання
        self._ws_connections = {}  # topic -> websocket instance
        self._ws_threads = {}
        self._callbacks = defaultdict(list)
        self._running = True
        self._reconnect_delay = 5
        self._max_reconnect_delay = 60

        # Мапа інтервалів
        self.interval_map = {
            '1m': '1',
            '3m': '3',
            '5m': '5',
            '15m': '15',
            '30m': '30',
            '1h': '60',
            '2h': '120',
            '4h': '240',
            '6h': '360',
            '12h': '720',
            '1d': 'D',
            '1w': 'W',
            '1M': 'M'
        }

        logger.info(f"BybitClient initialized (testnet={self.testnet})")

    # === REST METHODS ===
    def get_klines(self, symbol: str, interval: str, limit: int = 200):
        """Отримання історичних свічок"""
        try:
            bybit_interval = self.interval_map.get(interval, interval)

            response = self.session.get_kline(
                category="spot",
                symbol=symbol,
                interval=bybit_interval,
                limit=limit
            )

            if response['retCode'] == 0:
                klines = response['result']['list']
                df = pd.DataFrame(klines, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume', 'turnover'])
                df = df.astype({
                    'open': float,
                    'high': float,
                    'low': float,
                    'close': float,
                    'volume': float,
                    'turnover': float
                })
                df['timestamp'] = pd.to_datetime(df['timestamp'].astype(int), unit='ms')
                df = df.sort_values('timestamp')
                return df
            else:
                logger.error(f"Error getting klines: {response}")
                return None
        except Exception as e:
            logger.error(f"Exception in get_klines: {e}")
            return None

    def get_current_price(self, symbol: str) -> Optional[float]:
        """Отримання поточної ціни"""
        try:
            response = self.session.get_tickers(category="spot", symbol=symbol)
            if response['retCode'] == 0:
                return float(response['result']['list'][0]['lastPrice'])
            return None
        except Exception as e:
            logger.error(f"Error getting price for {symbol}: {e}")
            return None

    def get_balance(self, coin: str = "USDT") -> float:
        """Отримання реального балансу"""
        if not self.api_key:
            logger.warning("No API key for real balance")
            return 0.0

        try:
            response = self.session.get_wallet_balance(accountType="UNIFIED", coin=coin)
            if response['retCode'] == 0:
                balance = response['result']['list'][0]['coin'][0]['walletBalance']
                return float(balance)
            return 0.0
        except Exception as e:
            logger.error(f"Error getting balance: {e}")
            return 0.0

    def place_order(self, symbol: str, side: str, order_type: str,
                    qty: float, price: float = None, time_in_force: str = "GTC"):
        """Розміщення реального ордера"""
        try:
            params = {
                "category": "spot",
                "symbol": symbol,
                "side": side,
                "orderType": order_type,
                "qty": str(qty),
                "timeInForce": time_in_force
            }

            if price and order_type == "LIMIT":
                params["price"] = str(price)

            response = self.session.place_order(**params)

            if response['retCode'] == 0:
                logger.info(f"Order placed: {side} {qty} {symbol}")
                return response['result']
            else:
                logger.error(f"Order failed: {response}")
                return None
        except Exception as e:
            logger.error(f"Exception placing order: {e}")
            return None

    # === WEBSOCKET METHODS ===
    def subscribe_candles(self, symbol: str, interval: str, callback: Callable):
        """Підписка на свічкові дані з автоматичним перепідключенням"""
        ws_interval = self.interval_map.get(interval, interval)
        topic = f"kline.{ws_interval}.{symbol}"

        self._callbacks[topic].append(callback)

        # Якщо вже є підписка на цей topic, не створюємо нову
        if topic in self._ws_connections:
            logger.debug(f"Already subscribed to {topic}")
            return

        # Запускаємо WebSocket в окремому потоці
        self._start_websocket(topic)
        logger.info(f"Subscribed to {interval} candles for {symbol}")

    def _start_websocket(self, topic: str):
        """Запуск WebSocket з'єднання в окремому потоці"""

        def websocket_loop():
            import websocket
            import ssl

            ws_url = "wss://stream.bybit.com/v5/public/spot" if not self.testnet else "wss://stream-testnet.bybit.com/v5/public/spot"

            reconnect_delay = self._reconnect_delay

            while self._running:
                try:
                    # Налаштування WebSocket
                    ws = websocket.WebSocketApp(
                        ws_url,
                        on_open=lambda ws: self._on_open(ws, topic),
                        on_message=lambda ws, msg: self._on_message(ws, msg, topic),
                        on_error=lambda ws, error: self._on_error(ws, error, topic),
                        on_close=lambda ws, close_status_code, close_msg: self._on_close(ws, close_status_code,
                                                                                         close_msg, topic)
                    )

                    self._ws_connections[topic] = ws

                    # Запуск з таймаутом
                    wst = threading.Thread(target=ws.run_forever, kwargs={
                        'ping_interval': 20,
                        'ping_timeout': 10,
                        'sslopt': {"cert_reqs": ssl.CERT_NONE}
                    }, daemon=True)
                    wst.start()
                    self._ws_threads[topic] = wst

                    # Чекаємо поки з'єднання встановиться
                    time.sleep(2)

                    # Якщо з'єднання закрилося - перепідключаємось
                    while self._running and topic in self._ws_connections:
                        time.sleep(1)

                except Exception as e:
                    logger.error(f"WebSocket error for {topic}: {e}")

                if self._running:
                    logger.info(f"Reconnecting {topic} in {reconnect_delay}s...")
                    time.sleep(reconnect_delay)
                    reconnect_delay = min(reconnect_delay * 1.5, self._max_reconnect_delay)

            logger.info(f"WebSocket loop ended for {topic}")

        thread = threading.Thread(target=websocket_loop, daemon=True)
        thread.start()

    def _on_open(self, ws, topic: str):
        """Обробник відкриття WebSocket"""
        subscribe_msg = {
            "op": "subscribe",
            "args": [topic]
        }
        ws.send(json.dumps(subscribe_msg))
        logger.debug(f"WebSocket opened and subscribed to {topic}")

    def _on_message(self, ws, message: str, topic: str):
        """Обробник повідомлень WebSocket"""
        try:
            data = json.loads(message)

            # Перевіряємо чи це підтвердження підписки
            if data.get('op') == 'subscribe':
                logger.debug(f"Subscription confirmed for {topic}")
                return

            # Перевіряємо чи це дані свічки
            if 'topic' not in data or data.get('topic') != topic:
                return

            result = data.get('data', [])
            if not result:
                return

            # Bybit може повертати список або окремий об'єкт
            if isinstance(result, list):
                if not result:
                    return
                candle_data = result[0]
            else:
                candle_data = result

            if not isinstance(candle_data, dict):
                return

            # Перевіряємо чи свічка підтверджена (закрита)
            if not candle_data.get('confirm', False):
                return

            start = candle_data.get('start')
            if start is None:
                return

            candle = {
                'symbol': topic.split('.')[-1],
                'timestamp': pd.to_datetime(int(start), unit='ms'),
                'open': float(candle_data.get('open', 0)),
                'high': float(candle_data.get('high', 0)),
                'low': float(candle_data.get('low', 0)),
                'close': float(candle_data.get('close', 0)),
                'volume': float(candle_data.get('volume', 0)),
                'confirm': True
            }

            # Викликаємо всі callback для цього topic
            for callback in self._callbacks.get(topic, []):
                try:
                    callback(candle)
                except Exception as e:
                    logger.error(f"Callback error for {topic}: {e}")

        except json.JSONDecodeError as e:
            logger.error(f"JSON decode error: {e}")
        except Exception as e:
            logger.error(f"Message processing error: {e}")

    def _on_error(self, ws, error, topic: str):
        """Обробник помилок WebSocket"""
        logger.error(f"WebSocket error for {topic}: {error}")

    def _on_close(self, ws, close_status_code, close_msg, topic: str):
        """Обробник закриття WebSocket"""
        logger.warning(f"WebSocket closed for {topic}: {close_status_code} - {close_msg}")
        # Видаляємо з'єднання, щоб воно перестворилося
        if topic in self._ws_connections:
            del self._ws_connections[topic]
        if topic in self._ws_threads:
            del self._ws_threads[topic]

    def close(self):
        """Закриття всіх з'єднань"""
        logger.info("Closing all WebSocket connections...")
        self._running = False

        for topic, ws in list(self._ws_connections.items()):
            try:
                ws.close()
            except:
                pass

        self._ws_connections.clear()
        self._ws_threads.clear()
        self._callbacks.clear()
        logger.info("WebSocket connections closed")