from pybit.unified_trading import HTTP
from utils.config_loader import config
from utils.logger import logger
import pandas as pd
import time
from threading import Lock


class BybitClient:
    """Клієнт для роботи з Bybit API (REST + WebSocket)"""

    _instance = None
    _lock = Lock()

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

        self._ws_public = None
        self._ws_private = None
        self._price_callbacks = []
        self._candle_callbacks = []
        self._order_callbacks = []

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

    def get_current_price(self, symbol: str) -> float:
        """Отримання поточної ціни"""
        try:
            response = self.session.get_tickers(category="spot", symbol=symbol)
            if response['retCode'] == 0:
                return float(response['result']['list'][0]['lastPrice'])
            return None
        except Exception as e:
            logger.error(f"Error getting price: {e}")
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
    def subscribe_public_price(self, symbol: str, callback):
        """Підписка на ціни в реальному часі"""
        self._price_callbacks.append(callback)

        def handle_message(message):
            if message.get('topic') == f'publicBybitTickersV5.{symbol}':
                data = message.get('data', {})
                price_data = {
                    'symbol': symbol,
                    'last_price': float(data.get('lastPrice', 0)),
                    'bid_price': float(data.get('bid1Price', 0)),
                    'ask_price': float(data.get('ask1Price', 0)),
                    'timestamp': pd.Timestamp.now()
                }
                for cb in self._price_callbacks:
                    try:
                        cb(price_data)
                    except Exception as e:
                        logger.error(f"Price callback error: {e}")

        self._ws_public = WebStream(
            testnet=self.testnet,
            channels=[f"tickers.{symbol}"],
            callback=handle_message
        )

        logger.info(f"Subscribed to price updates for {symbol}")

    def subscribe_candles(self, symbol: str, interval: str, callback):
        """Підписка на свічкові дані"""
        self._candle_callbacks.append(callback)

        ws_interval = self.interval_map.get(interval, interval)

        def handle_message(message):
            try:
                if not message or 'topic' not in message:
                    return

                if f'kline.{ws_interval}.{symbol}' in message.get('topic', ''):
                    data_list = message.get('data', [])
                    if not data_list or len(data_list) == 0:
                        return

                    data = data_list[0]
                    if not data or not isinstance(data, dict):
                        return

                    start = data.get('start')
                    if start is None:
                        return

                    candle = {
                        'symbol': symbol,
                        'timestamp': pd.to_datetime(int(start), unit='ms'),
                        'open': float(data.get('open', 0)),
                        'high': float(data.get('high', 0)),
                        'low': float(data.get('low', 0)),
                        'close': float(data.get('close', 0)),
                        'volume': float(data.get('volume', 0)),
                        'confirm': data.get('confirm', False)
                    }

                    if candle['confirm']:
                        for cb in self._candle_callbacks:
                            try:
                                cb(candle)
                            except Exception as e:
                                logger.error(f"Candle callback error: {e}")
            except Exception as e:
                logger.error(f"WebSocket message error: {e}")

        self._ws_public = WebStream(
            testnet=self.testnet,
            channels=[f"kline.{ws_interval}.{symbol}"],
            callback=handle_message
        )

        logger.info(f"Subscribed to {interval} candles for {symbol}")

    def close(self):
        """Закриття з'єднань"""
        if self._ws_public:
            self._ws_public.exit()
        if self._ws_private:
            self._ws_private.exit()
        logger.info("WebSocket connections closed")


# WebSocket клас
class WebStream:
    def __init__(self, testnet, channels, callback):
        self.testnet = testnet
        self.channels = channels
        self.callback = callback
        self._ws = None
        self._connect()

    def _connect(self):
        import websocket
        import threading
        import json

        ws_url = "wss://stream.bybit.com/v5/public/spot" if not self.testnet else "wss://stream-testnet.bybit.com/v5/public/spot"

        def on_message(ws, message):
            data = json.loads(message)
            self.callback(data)

        def on_error(ws, error):
            logger.error(f"WebSocket error: {error}")

        def on_close(ws, close_status_code, close_msg):
            logger.warning("WebSocket closed, reconnecting in 5s...")
            threading.Timer(5, self._connect).start()

        def on_open(ws):
            subscribe_msg = {
                "op": "subscribe",
                "args": self.channels
            }
            ws.send(json.dumps(subscribe_msg))
            logger.info(f"WebSocket connected, subscribed to {self.channels}")

        self._ws = websocket.WebSocketApp(
            ws_url,
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close
        )

        wst = threading.Thread(target=self._ws.run_forever, daemon=True)
        wst.start()

    def exit(self):
        if self._ws:
            self._ws.close()