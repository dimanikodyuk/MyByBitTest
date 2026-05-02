from pybit.unified_trading import HTTP
from app.config import config
from loguru import logger
from typing import Dict, List, Optional


class ByBitRealClient:
    def __init__(self):
        self.session = HTTP(
            testnet=config.BYBIT_TESTNET,
            api_key=config.BYBIT_API_KEY,
            api_secret=config.BYBIT_API_SECRET,
        )
        logger.info(f"ByBit client initialized (testnet={config.BYBIT_TESTNET})")

    def _convert_interval(self, interval: str) -> str:
        """Конвертує інтервал у формат ByBit API"""
        intervals = {
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
        return intervals.get(interval, '60')

    async def get_klines(self, symbol: str, interval: str, limit: int = 100) -> List[Dict]:
        """Get candlestick data"""
        try:
            bybit_interval = self._convert_interval(interval)
            response = self.session.get_kline(
                category="spot",
                symbol=symbol,
                interval=bybit_interval,
                limit=limit
            )

            if response['retCode'] == 0:
                klines = []
                for k in response['result']['list']:
                    klines.append({
                        'timestamp': int(k[0]),
                        'open': float(k[1]),
                        'high': float(k[2]),
                        'low': float(k[3]),
                        'close': float(k[4]),
                        'volume': float(k[5]) if len(k) > 5 else 0
                    })
                # Перевертаємо, щоб було від старого до нового
                klines.reverse()
                return klines
            else:
                logger.error(f"Error getting klines: {response}")
                return []
        except Exception as e:
            logger.error(f"Exception in get_klines: {e}")
            return []

    async def get_current_price(self, symbol: str) -> Optional[float]:
        """Get current price for symbol"""
        try:
            response = self.session.get_ticker(category="spot", symbol=symbol)
            if response['retCode'] == 0 and response['result']['list']:
                return float(response['result']['list'][0]['lastPrice'])
            return None
        except Exception as e:
            logger.error(f"Error getting current price: {e}")
            return None

    async def place_order(self, symbol: str, side: str, order_type: str, quantity: float, price: float = None) -> \
    Optional[Dict]:
        """Place real order on ByBit"""
        try:
            params = {
                "category": "spot",
                "symbol": symbol,
                "side": side.upper(),
                "orderType": order_type.upper(),
                "qty": str(quantity),
                "timeInForce": "GTC"
            }

            if order_type.lower() == 'limit' and price:
                params["price"] = str(price)

            response = self.session.place_order(**params)

            if response['retCode'] == 0:
                logger.success(f"Order placed: {side} {quantity} {symbol}")
                return response['result']
            else:
                logger.error(f"Order failed: {response}")
                return None
        except Exception as e:
            logger.error(f"Exception placing order: {e}")
            return None

    async def get_balance(self) -> float:
        """Get USDT balance"""
        try:
            response = self.session.get_wallet_balance(accountType="UNIFIED", coin="USDT")
            if response['retCode'] == 0 and response['result']['list']:
                balance = float(response['result']['list'][0]['coin'][0]['walletBalance'])
                return balance
            return 0.0
        except Exception as e:
            logger.error(f"Error getting balance: {e}")
            return 0.0


bybit_real = ByBitRealClient()