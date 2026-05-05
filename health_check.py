#!/usr/bin/env python3
"""Швидка перевірка всіх компонентів перед запуском"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from utils.config_loader import config
from db.database import SessionLocal
from exchange.bybit_client import BybitClient


def check_all():
    errors = []

    # Перевірка конфігу
    if not config.api_key:
        errors.append("❌ API ключ не налаштовано")
    if not config.telegram_token:
        errors.append("⚠️ Telegram токен не налаштовано (бота не буде)")

    # Перевірка БД
    try:
        db = SessionLocal()
        db.execute("SELECT 1")
        db.close()
        print("✅ База даних: OK")
    except Exception as e:
        errors.append(f"❌ База даних: {e}")

    # Перевірка Bybit
    try:
        exchange = BybitClient()
        price = exchange.get_current_price("BTCUSDT")
        if price:
            print(f"✅ Bybit API: OK (BTCUSDT = ${price:.0f})")
        else:
            errors.append("❌ Bybit API: не відповідає")
    except Exception as e:
        errors.append(f"❌ Bybit API: {e}")

    if errors:
        print("\n".join(errors))
        return False
    return True


if __name__ == "__main__":
    if check_all():
        print("\n✅ Всі перевірки пройдено! Можна запускати бота.")
    else:
        print("\n❌ Є проблеми, виправте їх перед запуском.")