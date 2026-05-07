from pybit.unified_trading import HTTP
from utils.config_loader import config
from utils.logger import logger
import pandas as pd
import time
import json
import threading
from typing import Dict, List, Callable, Optional
from collections import defaultdict
from functools import wraps
from collections import deque


class RateLimiter:
    """Простий rate limiter для API запитів"""

    def __init__(self, max_calls: int, period: float = 1.0):
        self.max_calls = max_calls
        self.period = period
        self.calls = deque()

    def wait_if_needed(self):
        now = time.time()
        while self.calls and self.calls[0] < now - self.period:
            self.calls.popleft()

        if len(self.calls) >= self.max_calls:
            sleep_time = self.period - (now - self.calls[0])
            if sleep_time > 0:
                time.sleep(sleep_time)

        self.calls.append(time.time())


_public_limiter = RateLimiter(max_calls=45, period=1.0)
_private_limiter = RateLimiter(max_calls=45, period=1.0)
_kline_limiter = RateLimiter(max_calls=20, period=1.0)


def rate_limit(limiter):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            limiter.wait_if_needed()
            return func(*args, **kwargs)
        return wrapper
    return decorator


class BybitClient:
    """Клієнт для роботи з Bybit API (REST + WebSocket) - ОПТИМІЗОВАНА ВЕРСІЯ"""

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

        self.session = HTTP(
            api_key=self.api_key,
            api_secret=self.api_secret,
            testnet=self.testnet
        )

        # WebSocket - ОДНЕ з'єднання для всіх підписок
        self._ws_thread = None
        self._ws = None
        self._ws_running = False
        self._subscribed_topics = set()
        self._callbacks = defaultdict(list)

        self._reconnect_delay = 5
        self._max_reconnect_delay = 60

        self.interval_map = {
            '1m': '1', '3m': '3', '5m': '5', '15m': '15', '30m': '30',
            '1h': '60', '2h': '120', '4h': '240', '6h': '360', '12h': '720',
            '1d': 'D', '1w': 'W', '1M': 'M'
        }

        logger.info(f"BybitClient initialized (testnet={self.testnet})")

    # === REST METHODS ===

    @rate_limit(_kline_limiter)
    def get_klines(self, symbol: str, interval: str, limit: int = 200):
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
                    'open': float, 'high': float, 'low': float, 'close': float,
                    'volume': float, 'turnover': float
                })
                df['timestamp'] = pd.to_datetime(df['timestamp'].astype(int), unit='ms')
                df = df.sort_values('timestamp')
                return df
            return None
        except Exception as e:
            logger.error(f"Exception in get_klines: {e}")
            return None

    @rate_limit(_public_limiter)
    def get_current_price(self, symbol: str) -> Optional[float]:
        try:
            response = self.session.get_tickers(category="spot", symbol=symbol)
            if response['retCode'] == 0:
                return float(response['result']['list'][0]['lastPrice'])
            return None
        except Exception as e:
            logger.error(f"Error getting price for {symbol}: {e}")
            return None

    @rate_limit(_private_limiter)
    def get_balance(self, coin: str = "USDT") -> float:
        if not self.api_key:
            return 0.0
        try:
            response = self.session.get_wallet_balance(accountType="UNIFIED", coin=coin)
            if response['retCode'] == 0:
                return float(response['result']['list'][0]['coin'][0]['walletBalance'])
            return 0.0
        except Exception as e:
            logger.error(f"Error getting balance: {e}")
            return 0.0

    @rate_limit(_private_limiter)
    def place_order(self, symbol: str, side: str, order_type: str, qty: float, price: float = None, time_in_force: str = "GTC"):
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

    @rate_limit(_private_limiter)
    def get_order_status(self, symbol: str, order_id: str) -> Optional[Dict]:
        try:
            response = self.session.get_open_orders(category="spot", symbol=symbol, orderId=order_id)
            if response['retCode'] == 0:
                orders = response['result']['list']
                return orders[0] if orders else None
            return None
        except Exception as e:
            logger.error(f"Error getting order status: {e}")
            return None

    def get_all_tickers(self) -> List[Dict]:
        try:
            response = self.session.get_tickers(category="spot")
            if response and response.get('retCode') == 0:
                result = response.get('result', {})
                tickers = result.get('list', [])
                return [{'symbol': t['symbol'], 'price': float(t['lastPrice'])} for t in tickers]
            return []
        except Exception as e:
            logger.error(f"Помилка отримання тикерів: {e}")
            return []

    def wait_for_order_fill(self, symbol: str, order_id: str, timeout: int = 10) -> bool:
        start = time.time()
        while time.time() - start < timeout:
            status = self.get_order_status(symbol, order_id)
            if status and status.get('orderStatus') == 'Filled':
                return True
            if status and status.get('orderStatus') in ['Cancelled', 'Rejected']:
                return False
            time.sleep(0.5)
        return False

    # === WEBSOCKET - ОДИН КЛІЄНТ ДЛЯ ВСІХ ПІДПИСОК ===

    def subscribe_candles(self, symbol: str, interval: str, callback: Callable):
        """Підписка на свічкові дані через єдиний WebSocket"""
        ws_interval = self.interval_map.get(interval, interval)
        topic = f"kline.{ws_interval}.{symbol}"

        self._callbacks[topic].append(callback)

        if not self._ws_running:
            self._start_websocket()
        else:
            self._add_subscription(topic)

        logger.info(f"Subscribed to {interval} candles for {symbol} (total topics: {len(self._subscribed_topics)})")

    def _start_websocket(self):
        """Запуск єдиного WebSocket з'єднання"""

        def websocket_loop():
            import websocket
            import ssl

            ws_url = "wss://stream.bybit.com/v5/public/spot"
            if self.testnet:
                ws_url = "wss://stream-testnet.bybit.com/v5/public/spot"

            reconnect_delay = self._reconnect_delay
            self._ws_running = True

            while self._ws_running:
                try:
                    self._ws = websocket.WebSocketApp(
                        ws_url,
                        on_open=self._on_open,
                        on_message=self._on_message,
                        on_error=self._on_error,
                        on_close=self._on_close
                    )

                    wst = threading.Thread(target=self._ws.run_forever, kwargs={
                        'ping_interval': 20,
                        'ping_timeout': 10,
                        'sslopt': {"cert_reqs": ssl.CERT_NONE}
                    }, daemon=True)
                    wst.start()

                    while self._ws_running and self._ws and not hasattr(self._ws, 'sock'):
                        time.sleep(0.5)

                    if self._subscribed_topics:
                        self._send_subscribe(list(self._subscribed_topics))

                    while self._ws_running:
                        time.sleep(5)

                except Exception as e:
                    logger.error(f"WebSocket error: {e}")

                if self._ws_running:
                    logger.info(f"Reconnecting in {reconnect_delay}s...")
                    time.sleep(reconnect_delay)
                    reconnect_delay = min(reconnect_delay * 1.5, self._max_reconnect_delay)

            logger.info("WebSocket loop ended")

        self._ws_thread = threading.Thread(target=websocket_loop, daemon=True)
        self._ws_thread.start()

    def _on_open(self, ws):
        logger.info("WebSocket connected successfully")

    def _send_subscribe(self, topics: List[str]):
        if not self._ws:
            return

        subscribe_msg = {
            "op": "subscribe",
            "args": topics
        }
        try:
            self._ws.send(json.dumps(subscribe_msg))
            logger.info(f"Subscribed to {len(topics)} topics")
        except Exception as e:
            logger.error(f"Error sending subscribe: {e}")

    def _add_subscription(self, topic: str):
        if topic in self._subscribed_topics:
            return

        self._subscribed_topics.add(topic)
        if self._ws:
            self._send_subscribe([topic])
        logger.debug(f"Added subscription: {topic}")

    def _on_message(self, ws, message: str):
        try:
            data = json.loads(message)

            if data.get('op') == 'subscribe':
                logger.debug(f"Subscription confirmed: {data.get('args')}")
                return

            topic = data.get('topic')
            if not topic or not topic.startswith('kline.'):
                return

            result = data.get('data', [])
            if not result:
                return

            candle_data = result[0] if isinstance(result, list) else result
            if not isinstance(candle_data, dict):
                return

            if not candle_data.get('confirm', False):
                return

            start = candle_data.get('start')
            if not start:
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

            for callback in self._callbacks.get(topic, []):
                try:
                    callback(candle)
                except Exception as e:
                    logger.error(f"Callback error for {topic}: {e}")

        except json.JSONDecodeError as e:
            logger.error(f"JSON decode error: {e}")
        except Exception as e:
            logger.error(f"Message processing error: {e}")

    def _on_error(self, ws, error):
        logger.error(f"WebSocket error: {error}")

    def _on_close(self, ws, close_status_code, close_msg):
        logger.warning(f"WebSocket closed: {close_status_code} - {close_msg}")

    def close(self):
        logger.info("Closing WebSocket connection...")
        self._ws_running = False

        if self._ws:
            try:
                self._ws.close()
            except:
                pass

        self._subscribed_topics.clear()
        self._callbacks.clear()
        logger.info("WebSocket connection closed")