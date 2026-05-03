import os
import yaml
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()


class Config:
    """Singleton для конфігурації"""
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._load()
        return cls._instance

    def reload_config():
        """Перезавантаження конфігурації"""
        global config
        config = Config()

    def _load(self):
        # Завантаження YAML
        config_path = Path(__file__).parent.parent / "config.yaml"
        with open(config_path, 'r', encoding='utf-8') as f:
            self.data = yaml.safe_load(f)

        # Завантаження з .env
        self.api_key = os.getenv('BYBIT_API_KEY')
        self.api_secret = os.getenv('BYBIT_API_SECRET')
        self.telegram_token = os.getenv('TELEGRAM_BOT_TOKEN')
        self.telegram_chat_id = os.getenv('TELEGRAM_CHAT_ID')
        self.bot_mode = os.getenv('BOT_MODE', 'paper')

        # Валідація
        if self.bot_mode == 'real':
            assert self.api_key and self.api_secret, "API keys required for real trading"

    def get(self, key, default=None):
        keys = key.split('.')
        value = self.data
        for k in keys:
            if isinstance(value, dict):
                value = value.get(k, default)
            else:
                return default
        return value


config = Config()