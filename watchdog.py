#!/usr/bin/env python3
"""
Watchdog для моніторингу та автоматичного перезапуску бота
"""
import subprocess
import time
import requests
import sys
import os
from datetime import datetime


def log(msg):
    """Логування з часом"""
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}")


def is_bot_alive():
    """Перевірка чи бот живий через health check"""
    try:
        response = requests.get("http://localhost:8000/health", timeout=10)
        if response.status_code == 200:
            data = response.json()
            if data.get('status') == 'healthy':
                return True
            else:
                log(f"Bot status: {data.get('status')}")
                return False
        return False
    except requests.exceptions.ConnectionError:
        log("Connection error - bot may be down")
        return False
    except Exception as e:
        log(f"Health check error: {e}")
        return False


def restart_bot():
    """Перезапуск бота через systemctl"""
    log("⚠️ Bot is DOWN! Restarting...")
    try:
        # Надсилаємо сповіщення через curl (якщо бот ще може відповісти)
        try:
            requests.post("http://localhost:8000/api/push/subscribe",
                          json={"type": "restart", "message": "Restarting bot"})
        except:
            pass

        # Перезапуск сервісу
        result = subprocess.run(["sudo", "systemctl", "restart", "autotrading-bot"],
                                capture_output=True, text=True)

        if result.returncode == 0:
            log("✅ Bot restarted successfully")
        else:
            log(f"❌ Failed to restart bot: {result.stderr}")

        time.sleep(10)  # Даємо час на запуск
        return result.returncode == 0
    except Exception as e:
        log(f"Restart error: {e}")
        return False


def main():
    log("🚀 Watchdog started - monitoring bot health")
    log(f"PID: {os.getpid()}")

    restart_count = 0
    last_restart = 0

    while True:
        try:
            if not is_bot_alive():
                # Перевіряємо чи не занадто часто перезапускаємо
                if time.time() - last_restart > 300:  # не частіше ніж раз в 5 хвилин
                    if restart_bot():
                        restart_count += 1
                        last_restart = time.time()
                        log(f"Total restarts: {restart_count}")
                else:
                    log("Skipping restart - too frequent")
            else:
                # Бот живий - показуємо статус раз на хвилину
                pass

        except Exception as e:
            log(f"Watchdog error: {e}")

        time.sleep(30)  # Перевірка кожні 30 секунд


if __name__ == "__main__":
    main()