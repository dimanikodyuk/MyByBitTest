"""
FastAPI веб-інтерфейс для автотрейдинг бота
Оптимізована версія: 6 вкладок (Дашборд, Прогнози, Аналіз+Графіки, Патерни, Налаштування, Логи)
"""

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta
import asyncio
import json
import plotly.graph_objs as go
import plotly.utils
import pandas as pd
import yaml
from pathlib import Path
import pytz
from db.database import SessionLocal
from db.operations import DatabaseOperations
from db.models import Signal, Trade, Log, ForecastDB, OrderSide
from utils.config_loader import config
from utils.logger import logger
from sqlalchemy import text

app = FastAPI(title="AutoTrading Bot API", version="3.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

order_manager_ref = None
active_websockets = []
push_subscriptions = []

CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"

KYIV_TZ = pytz.timezone('Europe/Kiev')


def get_current_time():
    return datetime.now(KYIV_TZ)


def make_aware(dt):
    if dt.tzinfo is None:
        return KYIV_TZ.localize(dt)
    return dt.astimezone(KYIV_TZ)


def set_order_manager(om):
    global order_manager_ref
    order_manager_ref = om


def safe_parse_forecast_id(forecast_id: str) -> float:
    try:
        return float(forecast_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail=f"Невірний формат ID прогнозу: {forecast_id}")


async def get_next_forecast_time():
    now = get_current_time()
    next_minute = ((now.minute // 5) + 1) * 5
    next_time = now.replace(minute=0, second=0, microsecond=0) + timedelta(minutes=next_minute)
    if next_time <= now:
        next_time += timedelta(minutes=5)
    return next_time.isoformat()


async def get_active_forecasts():
    db = SessionLocal()
    try:
        now = get_current_time()
        expired = db.query(ForecastDB).filter(
            ForecastDB.expires_at < now,
            ForecastDB.status == "active"
        ).all()
        for exp in expired:
            exp.status = "expired"
        db.commit()

        active = db.query(ForecastDB).filter(ForecastDB.status == "active").all()

        return [
            {
                "id": f.forecast_id,
                "pair": f.pair,
                "signal_type": f.signal_type,
                "entry_price": f.entry_price,
                "target_price": f.target_price,
                "current_price": f.current_price,
                "confidence": f.confidence,
                "position_quantity": f.position_quantity,
                "position_usdt": f.position_usdt,
                "created_at": make_aware(f.created_at).isoformat(),
                "expires_at": make_aware(f.expires_at).isoformat(),
                "time_remaining": max(0, (make_aware(f.expires_at) - now).total_seconds()),
                "status": f.status,
                # Виправлено: абсолютне значення для profit_potential
                "profit_potential": (abs(f.target_price - f.entry_price) / f.entry_price) * 100 if f.entry_price > 0 else 0
            }
            for f in active
        ]
    except Exception as e:
        logger.error(f"Помилка отримання прогнозів: {e}")
        return []
    finally:
        db.close()


async def create_forecast_internal(pair, signal_type, entry_price, target_price, confidence,
                                   position_quantity=0.0, position_usdt=0.0,
                                   indicators_snapshot=None, description=None):
    """
    Створення прогнозу з розміром позиції, описом та індикаторами

    Args:
        pair: торгова пара (наприклад, 'BTCUSDT')
        signal_type: тип сигналу ('LONG' або 'SHORT')
        entry_price: ціна входу
        target_price: цільова ціна
        confidence: впевненість (0-100)
        position_quantity: кількість в одиницях базової валюти
        position_usdt: сума в USDT
        indicators_snapshot: словник з індикаторами на момент сигналу
        description: текстовий опис прогнозу (якщо None - генерується автоматично)
    """
    db = SessionLocal()
    try:
        forecast_id = datetime.now().timestamp()
        now = get_current_time()

        # Отримуємо тривалість прогнозу з конфігу
        duration_hours = config.get('testing.forecast_duration_hours', 12)

        # Перевіряємо чи вже є активний прогноз для цієї пари та типу
        existing = db.query(ForecastDB).filter(
            ForecastDB.pair == pair,
            ForecastDB.signal_type == signal_type,
            ForecastDB.status == "active"
        ).first()

        if existing:
            logger.debug(f"Прогноз для {pair} {signal_type} вже існує (id={existing.forecast_id})")
            return None

        # Якщо опис не переданий, але є індикатори - генеруємо автоматично
        if description is None and indicators_snapshot is not None:
            description = _generate_forecast_description(pair, signal_type, indicators_snapshot)
        elif description is None:
            # Мінімальний опис, якщо немає індикаторів
            profit_pct = abs((target_price - entry_price) / entry_price * 100) if entry_price > 0 else 0
            description = f"🔍 **Прогноз: {signal_type} для {pair}**\n\n"
            description += f"📊 Вхід: ${entry_price:.2f}\n"
            description += f"🎯 Ціль: ${target_price:.2f} (+{profit_pct:.1f}%)\n"
            description += f"📈 Впевненість: {confidence:.0f}%\n"
            description += f"⏰ Створено: {now.strftime('%Y-%m-%d %H:%M:%S')}\n"
            description += f"⏳ Дійсний до: {(now + timedelta(hours=duration_hours)).strftime('%Y-%m-%d %H:%M:%S')}"

        # Створюємо прогноз
        forecast = ForecastDB(
            forecast_id=forecast_id,
            pair=pair,
            signal_type=signal_type,
            entry_price=entry_price,
            target_price=target_price,
            current_price=entry_price,
            confidence=confidence,
            position_quantity=position_quantity,
            position_usdt=position_usdt,
            created_at=now,
            expires_at=now + timedelta(hours=duration_hours),
            status="active",
            description=description,
            indicators_snapshot=json.dumps(indicators_snapshot) if indicators_snapshot else None
        )
        db.add(forecast)
        db.commit()

        # Підготовка даних для WebSocket
        forecast_dict = {
            "id": forecast_id,
            "pair": pair,
            "signal_type": signal_type,
            "entry_price": entry_price,
            "target_price": target_price,
            "current_price": entry_price,
            "confidence": confidence,
            "position_quantity": position_quantity,
            "position_usdt": position_usdt,
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(hours=duration_hours)).isoformat(),
            "time_remaining": duration_hours * 3600,
            "status": "active",
            "profit_potential": abs((target_price - entry_price) / entry_price * 100) if entry_price > 0 else 0,
            "description": description
        }

        # Повідомляємо всі підключені WebSocket клієнти
        for ws in active_websockets:
            try:
                await ws.send_json({
                    "type": "new_forecast",
                    "forecast": forecast_dict
                })
            except Exception as e:
                logger.debug(f"WebSocket send error: {e}")

        logger.info(f"✅ Прогноз збережено: {pair} {signal_type} | ${position_usdt:.2f} | діє {duration_hours} год")
        return forecast

    except Exception as e:
        logger.error(f"Помилка створення прогнозу: {e}")
        db.rollback()
        return None
    finally:
        db.close()


def _generate_forecast_description(pair: str, signal_type: str, indicators: dict) -> str:
    """
    Генерація текстового опису прогнозу на основі індикаторів

    Args:
        pair: торгова пара
        signal_type: тип сигналу ('LONG' або 'SHORT')
        indicators: словник з індикаторами
    """
    from datetime import datetime, timedelta
    from utils.config_loader import config

    duration_hours = config.get('testing.forecast_duration_hours', 12)
    now = datetime.now()

    desc_lines = []

    # Заголовок
    desc_lines.append(f"🔍 **Прогноз: {signal_type} для {pair}**\n")
    desc_lines.append("📊 **Індикатори на момент сигналу:**")

    # EMA
    ema_fast = indicators.get('ema_fast', 0)
    ema_slow = indicators.get('ema_slow', 0)
    ema_fast_period = indicators.get('ema_fast_period', config.get('strategy.ema_fast', 21))
    ema_slow_period = indicators.get('ema_slow_period', config.get('strategy.ema_slow', 200))

    if ema_fast > ema_slow:
        diff_pct = abs((ema_fast - ema_slow) / ema_slow * 100) if ema_slow > 0 else 0
        desc_lines.append(
            f"• 📈 **EMA{ema_fast_period}** ({ema_fast:.2f}) > **EMA{ema_slow_period}** ({ema_slow:.2f}) → Висхідний тренд (сила {diff_pct:.1f}%)")
    else:
        diff_pct = abs((ema_slow - ema_fast) / ema_slow * 100) if ema_slow > 0 else 0
        desc_lines.append(
            f"• 📉 **EMA{ema_fast_period}** ({ema_fast:.2f}) < **EMA{ema_slow_period}** ({ema_slow:.2f}) → Низхідний тренд (сила {diff_pct:.1f}%)")

    # RSI
    rsi = indicators.get('rsi', 50)
    if signal_type == "LONG":
        if rsi < 40:
            desc_lines.append(f"• 📊 **RSI = {rsi:.1f}** (зона перепроданості) → очікуємо розворот вгору")
        elif rsi < 50:
            desc_lines.append(f"• 📊 **RSI = {rsi:.1f}** (нижче середнього) → є простір для росту")
        else:
            desc_lines.append(f"• 📊 **RSI = {rsi:.1f}** (нейтральна зона)")
    else:  # SHORT
        if rsi > 60:
            desc_lines.append(f"• 📊 **RSI = {rsi:.1f}** (зона перекупленості) → очікуємо розворот вниз")
        elif rsi > 50:
            desc_lines.append(f"• 📊 **RSI = {rsi:.1f}** (вище середнього) → є простір для падіння")
        else:
            desc_lines.append(f"• 📊 **RSI = {rsi:.1f}** (нейтральна зона)")

    # MACD
    macd = indicators.get('macd', 0)
    macd_signal = indicators.get('macd_signal', 0)
    if macd > macd_signal:
        diff = abs(macd - macd_signal)
        desc_lines.append(
            f"• 🟢 **MACD** ({macd:.6f}) > **Signal** ({macd_signal:.6f}) → бичачий імпульс (різниця {diff:.6f})")
    else:
        diff = abs(macd_signal - macd)
        desc_lines.append(
            f"• 🔴 **MACD** ({macd:.6f}) < **Signal** ({macd_signal:.6f}) → ведмежий імпульс (різниця {diff:.6f})")

    # Volume
    volume_ratio = indicators.get('volume_ratio', 1.0)
    if volume_ratio > 1.5:
        desc_lines.append(f"• 📊 **Об'єм = {volume_ratio:.1f}x** середнього → високий інтерес покупців/продавців")
    elif volume_ratio > 1.0:
        desc_lines.append(f"• 📊 **Об'єм = {volume_ratio:.1f}x** середнього → підвищений інтерес")
    elif volume_ratio > 0.7:
        desc_lines.append(f"• 📊 **Об'єм = {volume_ratio:.1f}x** середнього → нормальна активність")
    else:
        desc_lines.append(f"• 📊 **Об'єм = {volume_ratio:.1f}x** середнього → низька активність, можливий боковик")

    # ADX
    adx = indicators.get('adx', 0)
    if adx > 30:
        desc_lines.append(f"• 📊 **ADX = {adx:.1f}** → сильний тренд (найкращі умови для входу)")
    elif adx > 20:
        desc_lines.append(f"• 📊 **ADX = {adx:.1f}** → тренд формується (добрі умови)")
    elif adx > 15:
        desc_lines.append(f"• 📊 **ADX = {adx:.1f}** → слабкий тренд, можливий флет")
    else:
        desc_lines.append(f"• 📊 **ADX = {adx:.1f}** → відсутність тренду (ринок у флеті)")

    # ATR (волатильність)
    atr_percent = indicators.get('atr_percent', 0)
    if atr_percent > 2:
        desc_lines.append(f"• 📊 **ATR = {atr_percent:.2f}%** → висока волатильність, очікуємо сильний рух")
    elif atr_percent > 1:
        desc_lines.append(f"• 📊 **ATR = {atr_percent:.2f}%** → середня волатильність")
    else:
        desc_lines.append(f"• 📊 **ATR = {atr_percent:.2f}%** → низька волатильність, рух може бути повільним")

    # Ціль та потенційний прибуток
    entry = indicators.get('entry_price', 0)
    target = indicators.get('target_price', 0)
    if entry > 0 and target > 0:
        profit_pct = abs((target - entry) / entry * 100)
        if profit_pct > 3:
            desc_lines.append(f"\n🎯 **Ціль: ${target:.2f} (+{profit_pct:.1f}%)** → агресивна ціль")
        elif profit_pct > 1.5:
            desc_lines.append(f"\n🎯 **Ціль: ${target:.2f} (+{profit_pct:.1f}%)** → помірна ціль")
        else:
            desc_lines.append(f"\n🎯 **Ціль: ${target:.2f} (+{profit_pct:.1f}%)** → консервативна ціль")

    # Stop Loss (якщо є)
    sl = indicators.get('stop_loss', 0)
    if sl > 0 and entry > 0:
        if signal_type == "LONG":
            loss_pct = abs((entry - sl) / entry * 100)
        else:
            loss_pct = abs((sl - entry) / entry * 100)
        desc_lines.append(f"🛑 **Stop Loss: ${sl:.2f} ({loss_pct:.1f}%)**")

        # Risk/Reward
        if profit_pct > 0 and loss_pct > 0:
            rr = profit_pct / loss_pct
            if rr > 2.5:
                desc_lines.append(f"📈 **Risk/Reward = 1:{rr:.2f}** → відмінне співвідношення")
            elif rr > 2:
                desc_lines.append(f"📈 **Risk/Reward = 1:{rr:.2f}** → хороше співвідношення")
            elif rr > 1.5:
                desc_lines.append(f"📈 **Risk/Reward = 1:{rr:.2f}** → прийнятне співвідношення")
            else:
                desc_lines.append(f"⚠️ **Risk/Reward = 1:{rr:.2f}** → низьке співвідношення")

    # Часова інформація
    desc_lines.append(f"\n⏰ **Час створення:** {now.strftime('%Y-%m-%d %H:%M:%S')}")
    desc_lines.append(f"⏳ **Дійсний до:** {(now + timedelta(hours=duration_hours)).strftime('%Y-%m-%d %H:%M:%S')}")
    desc_lines.append(f"📈 **Впевненість моделі:** {indicators.get('confidence', 70):.0f}%")

    # Додаткова інформація про позицію
    position_usdt = indicators.get('position_usdt', 0)
    if position_usdt > 0:
        desc_lines.append(f"\n💰 **Розмір позиції:** ${position_usdt:.2f} USDT")

    return "\n".join(desc_lines)

def generate_forecast_description(pair: str, signal_type: str, indicators: dict) -> str:
    """Генерація текстового опису прогнозу"""
    desc_lines = []

    desc_lines.append(f"🔍 **Прогноз: {signal_type} для {pair}**\n")
    desc_lines.append("📊 **Індикатори на момент сигналу:**")

    # EMA
    ema_fast = indicators.get('ema_fast', 0)
    ema_slow = indicators.get('ema_slow', 0)
    if ema_fast > ema_slow:
        desc_lines.append(
            f"• 📈 EMA{indicators.get('ema_fast_period', 21)} ({ema_fast:.2f}) > EMA{indicators.get('ema_slow_period', 200)} ({ema_slow:.2f}) → Висхідний тренд")
    else:
        desc_lines.append(
            f"• 📉 EMA{indicators.get('ema_fast_period', 21)} ({ema_fast:.2f}) < EMA{indicators.get('ema_slow_period', 200)} ({ema_slow:.2f}) → Низхідний тренд")

    # RSI
    rsi = indicators.get('rsi', 50)
    if signal_type == "LONG":
        if rsi < 45:
            desc_lines.append(f"• 📊 RSI = {rsi:.1f} (зона перепроданості) → очікуємо розворот вгору")
        else:
            desc_lines.append(f"• 📊 RSI = {rsi:.1f} (нейтральна зона) → є простір для росту")
    else:
        if rsi > 55:
            desc_lines.append(f"• 📊 RSI = {rsi:.1f} (зона перекупленості) → очікуємо розворот вниз")
        else:
            desc_lines.append(f"• 📊 RSI = {rsi:.1f} (нейтральна зона) → є простір для падіння")

    # MACD
    macd = indicators.get('macd', 0)
    macd_signal = indicators.get('macd_signal', 0)
    if macd > macd_signal:
        desc_lines.append(f"• 🟢 MACD ({macd:.4f}) > Signal ({macd_signal:.4f}) → бичачий імпульс")
    else:
        desc_lines.append(f"• 🔴 MACD ({macd:.4f}) < Signal ({macd_signal:.4f}) → ведмежий імпульс")

    # Volume
    volume_ratio = indicators.get('volume_ratio', 1.0)
    if volume_ratio > 1.5:
        desc_lines.append(f"• 📊 Об'єм = {volume_ratio:.1f}x середнього → високий інтерес")
    elif volume_ratio > 1.0:
        desc_lines.append(f"• 📊 Об'єм = {volume_ratio:.1f}x середнього → підвищений інтерес")
    else:
        desc_lines.append(f"• 📊 Об'єм = {volume_ratio:.1f}x середнього → низька активність")

    # ADX
    adx = indicators.get('adx', 0)
    if adx > 30:
        desc_lines.append(f"• 📊 ADX = {adx:.1f} (сильний тренд)")
    elif adx > 20:
        desc_lines.append(f"• 📊 ADX = {adx:.1f} (тренд формується)")
    else:
        desc_lines.append(f"• 📊 ADX = {adx:.1f} (ринок у флеті)")

    # ATR
    atr_percent = indicators.get('atr_percent', 0)
    desc_lines.append(f"• 📊 ATR = {atr_percent:.2f}% (очікувана волатильність)")

    # Ціль
    target = indicators.get('target_price', 0)
    entry = indicators.get('entry_price', 0)
    profit_pct = abs((target - entry) / entry * 100) if entry > 0 else 0
    desc_lines.append(f"\n🎯 **Ціль: ${target:.2f} (+{profit_pct:.1f}%)**")

    desc_lines.append(f"\n⏰ **Час створення:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    desc_lines.append(f"⏳ **Дійсний до:** {(datetime.now() + timedelta(hours=12)).strftime('%Y-%m-%d %H:%M:%S')}")

    return "\n".join(desc_lines)

async def update_forecast_prices():
    from exchange.bybit_client import BybitClient
    exchange = BybitClient()

    db = SessionLocal()
    try:
        active = db.query(ForecastDB).filter(ForecastDB.status == "active").all()
        for forecast in active:
            try:
                current_price = exchange.get_current_price(forecast.pair)
                if current_price and current_price > 0:
                    forecast.current_price = current_price
                    db.commit()
            except Exception as e:
                logger.error(f"Помилка оновлення ціни для {forecast.pair}: {e}")
    finally:
        db.close()


@app.get("/health")
async def health_check():
    import platform
    bot_status = "running" if order_manager_ref and order_manager_ref.running else "stopped"
    db_status = "ok"
    try:
        db = SessionLocal()
        db.execute(text("SELECT 1"))
        db.close()
    except Exception:
        db_status = "error"
    balance = 0
    try:
        db = SessionLocal()
        db_ops = DatabaseOperations(db)
        balance = db_ops.get_balance("USDT", is_paper=True)
        db.close()
    except Exception:
        balance = 100.0
    uptime_seconds = None
    if hasattr(order_manager_ref, 'start_time') and order_manager_ref.start_time:
        try:
            uptime_delta = get_current_time() - order_manager_ref.start_time
            uptime_seconds = int(uptime_delta.total_seconds())
        except:
            pass
    return {
        "status": "healthy" if bot_status == "running" else "unhealthy",
        "timestamp": get_current_time().isoformat(),
        "server_time": get_current_time().isoformat(),
        "bot": {"status": bot_status, "mode": config.bot_mode, "version": "3.0.0", "uptime_seconds": uptime_seconds},
        "database": {"status": db_status},
        "balance": {"paper_usdt": balance},
        "system": {"python_version": platform.python_version(), "platform": platform.system()}
    }


@app.get("/api/available_pairs")
async def get_available_pairs():
    """Отримання списку доступних пар (з конфігу або стандартних)"""
    # Стандартний список популярних пар
    default_pairs = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "AVAXUSDT",
                     "DOTUSDT", "LINKUSDT", "MATICUSDT", "UNIUSDT", "ATOMUSDT", "LTCUSDT", "ETCUSDT"]

    # Можна також отримувати з API Bybit, але поки використовуємо стандартний
    return {"available_pairs": default_pairs}


@app.get("/api/trading_pairs")
async def get_trading_pairs():
    """Отримання поточного списку торгових пар (з конфігу)"""
    current_pairs = config.get('trading.pairs', ['BTCUSDT', 'ETHUSDT', 'SOLUSDT'])
    available = await get_available_pairs()
    return {
        "current_pairs": current_pairs,
        "available_pairs": available["available_pairs"]
    }


@app.post("/api/settings/testing")
async def update_testing_settings(settings: Dict[str, Any]):
    import yaml
    config_path = Path(__file__).parent.parent / "config.yaml"
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            current_config = yaml.safe_load(f)
        if 'testing' not in current_config:
            current_config['testing'] = {}
        for key, value in settings.items():
            current_config['testing'][key] = value
        with open(config_path, 'w', encoding='utf-8') as f:
            yaml.dump(current_config, f, allow_unicode=True, default_flow_style=False)
        from utils.config_loader import reload_config
        reload_config()
        return {"status": "success", "message": "Налаштування збережено"}
    except Exception as e:
        logger.error(f"Помилка збереження testing налаштувань: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/chart/equity")
async def get_equity_chart():
    db = SessionLocal()
    db_ops = DatabaseOperations(db)
    try:
        trades = db_ops.get_trades_history(limit=1000, is_paper=True)
        equity_points = []
        balance = 100.0
        equity_points.append({"time": datetime.now() - timedelta(days=30), "balance": balance, "pnl": 0, "pair": "Start"})
        for trade in sorted(trades, key=lambda x: x.closed_at if x.closed_at else x.opened_at):
            if trade.closed_at:
                balance += trade.pnl
                equity_points.append({
                    "time": trade.closed_at,
                    "balance": balance,
                    "pnl": trade.pnl,
                    "pair": trade.pair
                })
        if len(equity_points) <= 1:
            fig = go.Figure()
            fig.update_layout(title="Немає даних для Equity Curve", template="plotly_dark", height=400)
            return JSONResponse(content=json.loads(json.dumps(fig, cls=plotly.utils.PlotlyJSONEncoder)))
        fig = go.Figure()
        marker_colors = ['#00ff88' if p.get('pnl', 0) > 0 else '#ff4757' for p in equity_points[1:]]
        fig.add_trace(go.Scatter(
            x=[p["time"] for p in equity_points],
            y=[p["balance"] for p in equity_points],
            mode='lines+markers',
            name='Equity',
            line=dict(color='#00d4ff', width=2),
            marker=dict(size=6, color=marker_colors),
            text=[f"Balance: ${p['balance']:.2f}<br>PnL: ${p.get('pnl', 0):.2f}<br>Pair: {p.get('pair', 'Start')}" for p in equity_points],
            hoverinfo='text'
        ))
        fig.add_hline(y=100, line_dash="dash", line_color="#888", annotation_text="Initial Balance")
        fig.update_layout(
            title="Equity Curve (Баланс у часі)",
            xaxis_title="Дата",
            yaxis_title="Баланс (USDT)",
            template="plotly_dark",
            height=400,
            paper_bgcolor='rgba(0,0,0,0)',
            plot_bgcolor='rgba(26,26,46,0.5)'
        )
        return JSONResponse(content=json.loads(json.dumps(fig, cls=plotly.utils.PlotlyJSONEncoder)))
    except Exception as e:
        logger.error(f"Помилка створення графіку equity: {e}")
        fig = go.Figure()
        fig.update_layout(title=f"Помилка: {str(e)[:100]}", template="plotly_dark", height=400)
        return JSONResponse(content=json.loads(json.dumps(fig, cls=plotly.utils.PlotlyJSONEncoder)))
    finally:
        db.close()


@app.get("/api/trade/chart/{trade_id}")
async def get_trade_chart(trade_id: int, timeframe: str = "1h"):
    from exchange.bybit_client import BybitClient
    import pandas_ta as ta
    import traceback
    db = SessionLocal()
    try:
        trade = db.query(Trade).filter(Trade.id == trade_id).first()
        if not trade:
            raise HTTPException(status_code=404, detail="Угода не знайдена")
        logger.info(f"Створення графіку для угоди {trade_id}: {trade.pair}")
        exchange = BybitClient()
        df = exchange.get_klines(trade.pair, timeframe, limit=300)
        if df is None or df.empty:
            fig = go.Figure()
            fig.update_layout(title=f"Немає даних для {trade.pair}", template="plotly_dark", height=600)
            return JSONResponse(content=json.loads(json.dumps(fig, cls=plotly.utils.PlotlyJSONEncoder)))
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df['display_time'] = df['timestamp'].dt.tz_localize('UTC').dt.tz_convert(KYIV_TZ)
        df['EMA_20'] = ta.ema(df['close'], length=20)
        df['EMA_50'] = ta.ema(df['close'], length=50)
        df['EMA_200'] = ta.ema(df['close'], length=200)
        entry_time = trade.opened_at
        if entry_time.tzinfo is None:
            entry_time = KYIV_TZ.localize(entry_time)
        entry_display = entry_time
        exit_time = None
        if trade.closed_at:
            exit_time = trade.closed_at
            if exit_time.tzinfo is None:
                exit_time = KYIV_TZ.localize(exit_time)
        fig = go.Figure()
        fig.add_trace(go.Candlestick(
            x=df['display_time'],
            open=df['open'],
            high=df['high'],
            low=df['low'],
            close=df['close'],
            name='Ціна',
            showlegend=True
        ))
        fig.add_trace(go.Scatter(x=df['display_time'], y=df['EMA_20'], name='EMA 20', line=dict(color='#f39c12', width=1.5)))
        fig.add_trace(go.Scatter(x=df['display_time'], y=df['EMA_50'], name='EMA 50', line=dict(color='#00d4ff', width=1.5)))
        fig.add_trace(go.Scatter(x=df['display_time'], y=df['EMA_200'], name='EMA 200', line=dict(color='#ff4757', width=1.5)))
        entry_color = '#00ff88' if trade.side == OrderSide.BUY else '#ff4757'
        entry_symbol = 'triangle-up' if trade.side == OrderSide.BUY else 'triangle-down'
        fig.add_trace(go.Scatter(
            x=[entry_display],
            y=[trade.entry_price],
            mode='markers',
            name='Вхід',
            marker=dict(size=20, color=entry_color, symbol=entry_symbol, line=dict(width=2, color='white')),
            text=[f"<b>ВХІД</b><br>Ціна: ${trade.entry_price:.0f}<br>Кількість: {trade.quantity}"],
            hoverinfo='text'
        ))
        if exit_time:
            exit_color = '#00ff88' if trade.pnl > 0 else '#ff4757'
            fig.add_trace(go.Scatter(
                x=[exit_time],
                y=[trade.exit_price],
                mode='markers',
                name='Вихід',
                marker=dict(size=14, color='white', symbol='circle', line=dict(width=2, color=exit_color)),
                text=[f"<b>ВИХІД</b><br>Ціна: ${trade.exit_price:.0f}<br>PnL: ${trade.pnl:.2f} ({trade.pnl_percent:.1f}%)"],
                hoverinfo='text'
            ))
            fig.add_trace(go.Scatter(
                x=[entry_display, exit_time],
                y=[trade.entry_price, trade.exit_price],
                mode='lines',
                name='Траєкторія',
                line=dict(color='#00d4ff', width=2, dash='dash'),
                showlegend=False
            ))
        fig.update_xaxes(
            tickformat="%H:%M<br>%d/%m",
            tickangle=-45,
            title_text="Час (Київ)",
            rangeslider_visible=False
        )
        fig.update_layout(
            title=f"{trade.pair} - Угода #{trade_id}",
            xaxis_title="Час (Київ)",
            yaxis_title="Ціна (USDT)",
            template="plotly_dark",
            height=600,
            paper_bgcolor='rgba(0,0,0,0)',
            plot_bgcolor='rgba(26,26,46,0.5)',
            hovermode='closest'
        )
        return JSONResponse(content=json.loads(json.dumps(fig, cls=plotly.utils.PlotlyJSONEncoder)))
    except Exception as e:
        logger.error(f"Помилка створення графіку угоди: {e}")
        logger.error(traceback.format_exc())
        fig = go.Figure()
        fig.update_layout(title=f"Помилка: {str(e)[:100]}", template="plotly_dark", height=600)
        return JSONResponse(content=json.loads(json.dumps(fig, cls=plotly.utils.PlotlyJSONEncoder)))
    finally:
        db.close()


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    active_websockets.append(websocket)
    try:
        while True:
            await websocket.receive_text()
            if order_manager_ref:
                db = SessionLocal()
                db_ops = DatabaseOperations(db)
                try:
                    forecasts = await get_active_forecasts()
                    next_forecast_time = await get_next_forecast_time()
                    stats = db_ops.get_stats(is_paper=True)
                    await websocket.send_json({
                        "type": "status",
                        "balance": db_ops.get_balance("USDT", is_paper=True),
                        "open_trades": len(db_ops.get_open_trades(is_paper=True)),
                        "total_pnl": stats['total_pnl'],
                        "total_trades": stats['total_trades'],
                        "win_rate": stats['win_rate'],
                        "forecasts": forecasts,
                        "next_forecast": next_forecast_time,
                        "server_time": get_current_time().isoformat(),
                    })
                finally:
                    db.close()
            await asyncio.sleep(1)
    except WebSocketDisconnect:
        if websocket in active_websockets:
            active_websockets.remove(websocket)

# Функція для відправки сповіщень всім підключеним клієнтам
async def broadcast_notification(notification_type: str, data: dict):
    """Відправка сповіщення всім підключеним WebSocket клієнтам"""
    for ws in active_websockets[:]:
        try:
            await ws.send_json({
                "type": notification_type,
                **data
            })
        except Exception as e:
            logger.debug(f"WebSocket send error: {e}")
            if ws in active_websockets:
                active_websockets.remove(ws)

@app.get("/api/patterns/{pair}")
async def get_candle_patterns(pair: str, timeframe: str = "1h"):
    from exchange.bybit_client import BybitClient
    from core.strategy import TradingStrategy
    exchange = BybitClient()
    df = exchange.get_klines(pair, timeframe, limit=100)
    if df is None or df.empty:
        return {"error": "No data", "patterns": [], "signal": "neutral"}
    strategy = TradingStrategy(pair)
    df = strategy.calculate_indicators(df)
    result = strategy.detect_candle_patterns(df)
    return result


@app.get("/api/chart_patterns/{pair}")
async def get_chart_patterns(pair: str, timeframe: str = "1h", limit: int = 200):
    from exchange.bybit_client import BybitClient
    from core.pattern_detector import PatternDetector
    exchange = BybitClient()
    df = exchange.get_klines(pair, timeframe, limit=limit)
    if df is None or df.empty:
        raise HTTPException(status_code=404, detail="Дані не знайдено")
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    df['timestamp'] = df['timestamp'].dt.tz_localize('UTC').dt.tz_convert(KYIV_TZ)
    detector = PatternDetector(df)
    patterns = detector.detect_all()
    fig = go.Figure()
    fig.add_trace(go.Candlestick(
        x=df['timestamp'],
        open=df['open'],
        high=df['high'],
        low=df['low'],
        close=df['close'],
        name=f'{pair}',
        showlegend=True
    ))
    colors = {'bullish': '#00ff88', 'bearish': '#ff4757', 'neutral': '#f39c12'}
    for pattern in patterns:
        color = colors.get(pattern.type, '#888')
        if len(pattern.points) >= 2:
            for i in range(len(pattern.points) - 1):
                idx0 = max(0, min(int(pattern.points[i][0]), len(df) - 1))
                idx1 = max(0, min(int(pattern.points[i + 1][0]), len(df) - 1))
                fig.add_shape(
                    type='line',
                    x0=df['timestamp'].iloc[idx0],
                    y0=float(pattern.points[i][1]),
                    x1=df['timestamp'].iloc[idx1],
                    y1=float(pattern.points[i + 1][1]),
                    line=dict(color=color, width=2, dash='dash'),
                )
        mid_idx = max(0, min((pattern.start_idx + pattern.end_idx) // 2, len(df) - 1))
        price_range = df['high'].iloc[mid_idx] - df['low'].iloc[mid_idx]
        if price_range > 0:
            fig.add_annotation(
                x=df['timestamp'].iloc[mid_idx],
                y=df['high'].iloc[mid_idx] + price_range * 0.5,
                text=f"<b>{pattern.name}</b>",
                showarrow=True,
                arrowhead=2,
                arrowsize=1,
                arrowwidth=2,
                arrowcolor=color,
                bgcolor='rgba(0,0,0,0.7)',
                bordercolor=color,
                borderwidth=1,
                font=dict(color=color, size=11),
                ax=0,
                ay=-40
            )
    fig.update_xaxes(
        tickformat="%H:%M<br>%d/%m",
        tickangle=-45,
        title_text="Час (Київ)",
        rangeslider_visible=False,
        gridcolor='rgba(102,126,234,0.1)',
        showgrid=True
    )
    fig.update_yaxes(
        title_text="Ціна (USDT)",
        gridcolor='rgba(102,126,234,0.1)',
        showgrid=True
    )
    fig.update_layout(
        title=dict(text=f"{pair} - Технічний аналіз (фігури)", font=dict(size=16, color='#fff')),
        template="plotly_dark",
        height=600,
        paper_bgcolor='rgba(0,0,0,0)',
        plot_bgcolor='rgba(26,26,46,0.3)',
        hovermode='closest',
        xaxis=dict(showline=True, showgrid=True, gridcolor='rgba(102,126,234,0.2)', linecolor='rgba(102,126,234,0.5)'),
        yaxis=dict(showline=True, showgrid=True, gridcolor='rgba(102,126,234,0.2)', linecolor='rgba(102,126,234,0.5)'),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1, font=dict(color='#888', size=10))
    )
    return JSONResponse(content=json.loads(json.dumps(fig, cls=plotly.utils.PlotlyJSONEncoder)))


@app.get("/api/current_price/{pair}")
async def get_current_price(pair: str):
    from exchange.bybit_client import BybitClient
    exchange = BybitClient()
    try:
        price = exchange.get_current_price(pair)
        return {"price": price}
    except Exception as e:
        logger.error(f"Помилка отримання ціни для {pair}: {e}")
        return {"price": None}


@app.get("/api/status")
async def get_status():
    db = SessionLocal()
    db_ops = DatabaseOperations(db)
    try:
        next_forecast = await get_next_forecast_time()
        stats = db_ops.get_stats(is_paper=True)
        balance = db_ops.get_balance("USDT", is_paper=True)
        open_trades = db_ops.get_open_trades(is_paper=True)
        locked_amount = sum(t.entry_price * t.quantity for t in open_trades)
        if balance < 10:
            logger.warning(f"Paper баланс критично низький: {balance:.4f} USDT")
        return {
            "status": "running" if order_manager_ref and order_manager_ref.running else "stopped",
            "mode": config.bot_mode,
            "balance": balance,
            "locked_amount": locked_amount,
            "available_balance": balance - locked_amount,
            "open_trades": len(open_trades),
            "total_trades": stats['total_trades'],
            "win_rate": stats['win_rate'],
            "total_pnl": stats['total_pnl'],
            "daily_pnl": db_ops.get_daily_pnl(is_paper=True),
            "next_forecast": next_forecast,
            "server_time": get_current_time().isoformat(),
            "profit_factor": stats.get('profit_factor', 0),
            "max_drawdown": stats.get('max_drawdown', 0),
            "avg_pnl": stats.get('avg_pnl', 0)
        }
    finally:
        db.close()


@app.post("/api/trade/close/{trade_id}")
async def close_trade_manually(trade_id: int, price: float = None):
    if order_manager_ref:
        result = await order_manager_ref.close_trade_manually(trade_id, price)
        return result
    return {"error": "Order manager not available"}


@app.get("/api/trades")
async def get_trades(limit: int = 50):
    db = SessionLocal()
    db_ops = DatabaseOperations(db)
    try:
        trades = db_ops.get_trades_history(limit=limit, is_paper=True)
        return [
            {
                "id": t.id,
                "pair": t.pair,
                "side": t.side.value,
                "entry_price": t.entry_price,
                "exit_price": t.exit_price,
                "quantity": t.quantity,
                "pnl": t.pnl,
                "pnl_percent": t.pnl_percent,
                "opened_at": make_aware(t.opened_at).isoformat() if t.opened_at else None,
                "closed_at": make_aware(t.closed_at).isoformat() if t.closed_at else None
            }
            for t in trades
        ]
    finally:
        db.close()


@app.get("/api/open_trades")
async def get_open_trades():
    db = SessionLocal()
    db_ops = DatabaseOperations(db)
    try:
        trades = db_ops.get_open_trades(is_paper=True)
        return [
            {
                "id": t.id,
                "pair": t.pair,
                "side": t.side.value,
                "entry_price": t.entry_price,
                "quantity": t.quantity,
                "take_profit": t.take_profit,
                "stop_loss": t.stop_loss
            }
            for t in trades
        ]
    finally:
        db.close()


@app.get("/api/balance")
async def get_balance():
    db = SessionLocal()
    db_ops = DatabaseOperations(db)
    try:
        return {"USDT": db_ops.get_balance("USDT", is_paper=True)}
    finally:
        db.close()


@app.get("/api/pairs")
async def get_pairs():
    return {"pairs": config.get('trading.pairs', ['BTCUSDT', 'ETHUSDT', 'SOLUSDT'])}


@app.get("/api/locked_amount")
async def get_locked_amount():
    db = SessionLocal()
    db_ops = DatabaseOperations(db)
    try:
        open_trades = db_ops.get_open_trades(is_paper=True)
        locked_amount = 0.0
        for trade in open_trades:
            position_value = trade.entry_price * trade.quantity
            locked_amount += position_value
        return {"locked_amount": locked_amount, "open_trades_count": len(open_trades)}
    except Exception as e:
        logger.error(f"Помилка отримання заблокованої суми: {e}")
        return {"locked_amount": 0, "open_trades_count": 0}
    finally:
        db.close()


@app.get("/api/forecasts")
async def get_forecasts():
    return await get_active_forecasts()


@app.get("/api/forecasts/history")
async def get_forecasts_history(limit: int = 50, offset: int = 0):
    db = SessionLocal()
    try:
        query = db.query(ForecastDB).order_by(ForecastDB.created_at.desc())
        total = query.count()
        forecasts = query.offset(offset).limit(limit).all()
        result = []
        for f in forecasts:
            if f.status == "completed":
                if f.signal_type == "LONG":
                    profit_percent = ((f.current_price - f.entry_price) / f.entry_price) * 100 if f.entry_price > 0 else 0
                else:
                    profit_percent = ((f.entry_price - f.current_price) / f.entry_price) * 100 if f.entry_price > 0 else 0
                result_text = f"✅ Виконано (+{profit_percent:.1f}%)" if profit_percent > 0 else f"❌ Виконано ({profit_percent:.1f}%)"
            elif f.status == "expired":
                result_text = "⏰ Прострочено"
            elif f.status == "active":
                if f.signal_type == "LONG":
                    current_profit = ((f.current_price - f.entry_price) / f.entry_price) * 100 if f.entry_price > 0 else 0
                else:
                    current_profit = ((f.entry_price - f.current_price) / f.entry_price) * 100 if f.entry_price > 0 else 0
                result_text = f"🟡 Активний ({current_profit:+.1f}%)"
            else:
                result_text = f"❌ {f.status}"
            result.append({
                "id": f.forecast_id,
                "pair": f.pair,
                "signal_type": f.signal_type,
                "entry_price": f.entry_price,
                "target_price": f.target_price,
                "current_price": f.current_price,
                "confidence": f.confidence,
                "status": f.status,
                "result_text": result_text,
                "created_at": make_aware(f.created_at).isoformat(),
                "expires_at": make_aware(f.expires_at).isoformat() if f.expires_at else None,
                "closed_at": make_aware(f.closed_at).isoformat() if f.closed_at else None,
            })
        return {"forecasts": result, "total": total, "limit": limit, "offset": offset}
    except Exception as e:
        logger.error(f"Помилка отримання історії прогнозів: {e}")
        return {"forecasts": [], "total": 0}
    finally:
        db.close()


@app.get("/api/forecast/info/{forecast_id}")
async def get_forecast_info(forecast_id: str):
    db = SessionLocal()
    try:
        fid = safe_parse_forecast_id(forecast_id)
        forecast = db.query(ForecastDB).filter(ForecastDB.forecast_id == fid).first()
        if not forecast:
            raise HTTPException(status_code=404, detail="Прогноз не знайдено")
        return {
            "id": forecast.forecast_id,
            "pair": forecast.pair,
            "signal_type": forecast.signal_type,
            "entry_price": forecast.entry_price,
            "target_price": forecast.target_price,
            "current_price": forecast.current_price,
            "confidence": forecast.confidence,
            "status": forecast.status,
            "created_at": make_aware(forecast.created_at).isoformat(),
        }
    except ValueError:
        raise HTTPException(status_code=400, detail="Невірний ID прогнозу")
    finally:
        db.close()


@app.get("/api/settings/testing")
async def get_testing_settings():
    try:
        return {
            "create_trades_from_forecasts": config.get('testing.create_trades_from_forecasts', False),
            "forecast_position_percent": config.get('testing.forecast_position_percent', 25.0)
        }
    except Exception as e:
        logger.error(f"Помилка отримання testing налаштувань: {e}")
        return {"create_trades_from_forecasts": False, "forecast_position_percent": 25.0}


@app.get("/api/settings/forecast_duration")
async def get_forecast_duration():
    """Отримання тривалості прогнозу"""
    try:
        duration = config.get('testing.forecast_duration_hours', 12)
        return {"forecast_duration_hours": duration}
    except Exception as e:
        logger.error(f"Помилка отримання тривалості прогнозу: {e}")
        return {"forecast_duration_hours": 12}


@app.post("/api/settings/forecast_duration")
async def update_forecast_duration(data: Dict[str, Any]):
    """Оновлення тривалості прогнозу"""
    import yaml
    from pathlib import Path

    new_duration = data.get('forecast_duration_hours', 12)

    # Валідація
    if not isinstance(new_duration, (int, float)) or new_duration < 1 or new_duration > 72:
        raise HTTPException(status_code=400, detail="Тривалість має бути від 1 до 72 годин")

    config_path = Path(__file__).parent.parent / "config.yaml"

    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            current_config = yaml.safe_load(f)

        if 'testing' not in current_config:
            current_config['testing'] = {}

        current_config['testing']['forecast_duration_hours'] = new_duration

        with open(config_path, 'w', encoding='utf-8') as f:
            yaml.dump(current_config, f, allow_unicode=True, default_flow_style=False)

        from utils.config_loader import reload_config
        reload_config()

        return {"status": "success", "forecast_duration_hours": new_duration}
    except Exception as e:
        logger.error(f"Помилка збереження тривалості прогнозу: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/forecast/chart/{forecast_id}")
async def get_forecast_chart(forecast_id: str, timeframe: str = "1h"):
    from exchange.bybit_client import BybitClient
    import pandas_ta as ta
    import traceback
    db = SessionLocal()
    try:
        fid = safe_parse_forecast_id(forecast_id)
        forecast = db.query(ForecastDB).filter(ForecastDB.forecast_id == fid).first()
        if not forecast:
            raise HTTPException(status_code=404, detail="Прогноз не знайдено")
        logger.info(f"Створення графіку для прогнозу {forecast_id}")
        exchange = BybitClient()
        df = exchange.get_klines(forecast.pair, timeframe, limit=300)
        if df is None or df.empty:
            fig = go.Figure()
            fig.update_layout(title=f"Немає даних для {forecast.pair}", template="plotly_dark", height=550)
            return JSONResponse(content=json.loads(json.dumps(fig, cls=plotly.utils.PlotlyJSONEncoder)))
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df['display_time'] = df['timestamp'].dt.tz_localize('UTC').dt.tz_convert(KYIV_TZ)
        entry_time = forecast.created_at
        if entry_time.tzinfo is None:
            entry_time = KYIV_TZ.localize(entry_time)
        entry_time_utc = entry_time.astimezone(pytz.UTC)
        start_filter = entry_time_utc - pd.Timedelta(hours=24)
        df['timestamp_utc'] = df['timestamp'].dt.tz_localize('UTC')
        df_filtered = df[df['timestamp_utc'] >= start_filter]
        if df_filtered.empty:
            df_filtered = df.tail(150)
            df_filtered['display_time'] = df_filtered['timestamp'].dt.tz_localize('UTC').dt.tz_convert(KYIV_TZ)
        df_filtered['EMA_20'] = ta.ema(df_filtered['close'], length=20)
        df_filtered['EMA_50'] = ta.ema(df_filtered['close'], length=50)
        df_filtered['EMA_200'] = ta.ema(df_filtered['close'], length=200)
        fig = go.Figure()
        fig.add_trace(go.Candlestick(
            x=df_filtered['display_time'],
            open=df_filtered['open'],
            high=df_filtered['high'],
            low=df_filtered['low'],
            close=df_filtered['close'],
            name='Ціна',
            showlegend=True
        ))
        fig.add_trace(go.Scatter(x=df_filtered['display_time'], y=df_filtered['EMA_20'], name='EMA 20', line=dict(color='#f39c12', width=1.5)))
        fig.add_trace(go.Scatter(x=df_filtered['display_time'], y=df_filtered['EMA_50'], name='EMA 50', line=dict(color='#00d4ff', width=1.5)))
        fig.add_trace(go.Scatter(x=df_filtered['display_time'], y=df_filtered['EMA_200'], name='EMA 200', line=dict(color='#ff4757', width=1.5)))
        entry_color = '#00ff88' if forecast.signal_type == 'LONG' else '#ff4757'
        entry_symbol = 'triangle-up' if forecast.signal_type == 'LONG' else 'triangle-down'
        fig.add_trace(go.Scatter(
            x=[entry_time],
            y=[forecast.entry_price],
            mode='markers',
            name=f"Вхід {forecast.signal_type}",
            marker=dict(size=20, color=entry_color, symbol=entry_symbol, line=dict(width=2, color='white')),
            text=[f"<b>{forecast.signal_type} ВХІД</b><br>Ціна: ${forecast.entry_price:.0f}<br>Ціль: ${forecast.target_price:.0f}<br>Впевненість: {forecast.confidence}%<br>Час: {entry_time.strftime('%Y-%m-%d %H:%M')}"],
            hoverinfo='text',
            hovertemplate='%{text}<extra></extra>'
        ))
        current_price = df_filtered['close'].iloc[-1]
        current_display_time = df_filtered['display_time'].iloc[-1]
        if current_display_time > entry_time:
            fig.add_trace(go.Scatter(
                x=[entry_time, current_display_time],
                y=[forecast.entry_price, current_price],
                mode='lines',
                name='Траєкторія',
                line=dict(color='#00d4ff', width=2, dash='dash'),
                showlegend=False
            ))
        fig.add_trace(go.Scatter(
            x=[current_display_time],
            y=[current_price],
            mode='markers',
            name='Поточна ціна',
            marker=dict(size=14, color='white', symbol='circle', line=dict(width=2, color='#00d4ff')),
            text=[f"<b>ПОТОЧНА ЦІНА</b><br>${current_price:.0f}<br>{current_display_time.strftime('%Y-%m-%d %H:%M')}"],
            hoverinfo='text',
            hovertemplate='%{text}<extra></extra>'
        ))
        fig.add_hline(
            y=forecast.target_price,
            line_dash="dash",
            line_color="#00ff88",
            annotation_text=f"🎯 Ціль: ${forecast.target_price:.0f}",
            annotation_font_size=11,
            annotation_font_color="#00ff88",
            annotation_x=0.02
        )
        fig.update_xaxes(tickformat="%H:%M<br>%d/%m", tickangle=-45, title_text="Час (Київ)", rangeslider_visible=False)
        fig.update_layout(
            title=f"{forecast.pair} - Прогноз {forecast.signal_type} від {entry_time.strftime('%Y-%m-%d %H:%M')}",
            xaxis_title="Час (Київ)",
            yaxis_title="Ціна (USDT)",
            template="plotly_dark",
            height=550,
            paper_bgcolor='rgba(0,0,0,0)',
            plot_bgcolor='rgba(26,26,46,0.5)',
            hovermode='closest',
            hoverlabel=dict(bgcolor="rgba(0,0,0,0.8)", font_size=12, font_family="monospace")
        )
        return JSONResponse(content=json.loads(json.dumps(fig, cls=plotly.utils.PlotlyJSONEncoder)))
    except ValueError:
        raise HTTPException(status_code=400, detail="Невірний ID прогнозу")
    except Exception as e:
        logger.error(f"Помилка створення графіку прогнозу: {e}")
        logger.error(traceback.format_exc())
        fig = go.Figure()
        fig.update_layout(title=f"Помилка: {str(e)[:100]}", template="plotly_dark", height=550)
        return JSONResponse(content=json.loads(json.dumps(fig, cls=plotly.utils.PlotlyJSONEncoder)))
    finally:
        db.close()


@app.delete("/api/forecast/{forecast_id}")
async def delete_forecast(forecast_id: str):
    db = SessionLocal()
    try:
        fid = safe_parse_forecast_id(forecast_id)
        forecast = db.query(ForecastDB).filter(ForecastDB.forecast_id == fid).first()
        if forecast:
            db.delete(forecast)
            db.commit()
            return {"status": "success"}
        raise HTTPException(status_code=404, detail="Прогноз не знайдено")
    except ValueError:
        raise HTTPException(status_code=400, detail="Невірний ID прогнозу")
    finally:
        db.close()


# ==================== БЕКТЕСТЕР ====================

@app.post("/api/backtest/run")
async def run_backtest(request: Dict[str, Any]):
    """Запуск бектесту"""
    from core.backtest_engine import BacktestEngine
    from datetime import datetime

    symbol = request.get('symbol', 'BTCUSDT')
    timeframe = request.get('timeframe', '15m')
    start_date_str = request.get('start_date')
    end_date_str = request.get('end_date')
    initial_balance = float(request.get('initial_balance', 1000.0))
    risk_percent = float(request.get('risk_percent', 2.0))

    if not start_date_str or not end_date_str:
        raise HTTPException(status_code=400, detail="Не вказані дати початку та кінця")

    try:
        start_date = datetime.strptime(start_date_str, '%Y-%m-%d')
        end_date = datetime.strptime(end_date_str, '%Y-%m-%d')
    except ValueError:
        raise HTTPException(status_code=400, detail="Невірний формат дати. Використовуйте YYYY-MM-DD")

    if start_date >= end_date:
        raise HTTPException(status_code=400, detail="Дата початку має бути раніше дати кінця")

    engine = BacktestEngine()

    try:
        result = engine.run_backtest(
            symbol=symbol,
            timeframe=timeframe,
            start_date=start_date,
            end_date=end_date,
            initial_balance=initial_balance,
            risk_percent=risk_percent
        )

        if 'error' in result:
            raise HTTPException(status_code=400, detail=result['error'])

        return JSONResponse(content=result)

    except Exception as e:
        logger.error(f"Помилка бектесту: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/listing/close/{trade_id}")
async def close_listing_trade(trade_id: int, price: float = None):
    """Закриття угоди нової монети"""
    db = SessionLocal()
    try:
        from db.models import ListingTrade, ListingBalance

        trade = db.query(ListingTrade).filter(ListingTrade.id == trade_id).first()
        if not trade:
            raise HTTPException(status_code=404, detail="Угоду не знайдено")

        if trade.status != "open":
            return {"status": "error", "message": f"Угода вже {trade.status}"}

        # Отримуємо поточну ціну, якщо не передана
        if price is None or price <= 0:
            from exchange.bybit_client import BybitClient
            exchange = BybitClient()
            price = exchange.get_current_price(trade.pair)

            # Якщо ціна не отримана (наприклад, TESTUSDT), використовуємо entry_price
            if not price or price <= 0:
                price = trade.entry_price
                logger.warning(f"Не вдалося отримати ціну для {trade.pair}, використовуємо entry_price")

        # Розраховуємо PnL
        pnl = (price - trade.entry_price) * trade.quantity
        pnl_percent = ((price - trade.entry_price) / trade.entry_price) * 100 if trade.entry_price > 0 else 0

        # Оновлюємо угоду
        trade.exit_price = price
        trade.exit_time = datetime.now()
        trade.pnl = pnl
        trade.pnl_percent = pnl_percent
        trade.status = "closed"
        trade.exit_reason = "manual_close"

        # Оновлюємо баланс
        balance = db.query(ListingBalance).first()
        if balance:
            balance.amount = balance.amount + trade.position_usdt + pnl
            balance.total_pnl += pnl
            balance.total_trades += 1
            if pnl > 0:
                balance.win_trades += 1
        else:
            balance = ListingBalance(amount=100.0 + pnl, initial_balance=100.0, total_pnl=pnl, total_trades=1,
                                     win_trades=1 if pnl > 0 else 0)
            db.add(balance)
        db.commit()

        logger.info(f"✅ Ручне закриття {trade.symbol}: PnL=${pnl:.2f} ({pnl_percent:.1f}%)")
        return {"status": "success", "pnl": pnl, "pnl_percent": pnl_percent, "exit_price": price}

    except Exception as e:
        logger.error(f"Помилка закриття угоди {trade_id}: {e}")
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

@app.get("/api/backtest/history")
async def get_backtest_history(limit: int = 20, offset: int = 0):
    """Отримання історії бектестів"""
    db = SessionLocal()
    try:
        from db.backtest_models import Backtest

        query = db.query(Backtest).order_by(Backtest.created_at.desc())
        total = query.count()
        backtests = query.offset(offset).limit(limit).all()

        return {
            "backtests": [
                {
                    "id": b.id,
                    "name": b.name,
                    "symbol": b.symbol,
                    "timeframe": b.timeframe,
                    "total_return_pct": b.total_return_pct,
                    "final_balance": b.final_balance,
                    "total_trades": b.total_trades,
                    "win_rate": b.win_rate,
                    "profit_factor": b.profit_factor,
                    "max_drawdown": b.max_drawdown,
                    "created_at": b.created_at.isoformat()
                }
                for b in backtests
            ],
            "total": total,
            "limit": limit,
            "offset": offset
        }
    finally:
        db.close()


@app.get("/api/backtest/{backtest_id}")
async def get_backtest_by_id(backtest_id: int):
    """Отримання результату бектесту за ID"""
    db = SessionLocal()
    try:
        from db.backtest_models import Backtest

        backtest = db.query(Backtest).filter(Backtest.id == backtest_id).first()
        if not backtest:
            raise HTTPException(status_code=404, detail="Бектест не знайдено")

        return {
            "id": backtest.id,
            "name": backtest.name,
            "description": backtest.description,
            "symbol": backtest.symbol,
            "timeframe": backtest.timeframe,
            "start_date": backtest.start_date.isoformat(),
            "end_date": backtest.end_date.isoformat(),
            "initial_balance": backtest.initial_balance,
            "total_return_pct": backtest.total_return_pct,
            "final_balance": backtest.final_balance,
            "total_trades": backtest.total_trades,
            "win_trades": backtest.win_trades,
            "loss_trades": backtest.loss_trades,
            "win_rate": backtest.win_rate,
            "profit_factor": backtest.profit_factor,
            "max_drawdown": backtest.max_drawdown,
            "sharpe_ratio": backtest.sharpe_ratio,
            "expectancy": backtest.expectancy,
            "trades": backtest.trades,
            "equity_values": backtest.equity_values,
            "equity_timestamps": backtest.equity_timestamps,
            "created_at": backtest.created_at.isoformat()
        }
    finally:
        db.close()


@app.post("/api/backtest/save")
async def save_backtest(request: Dict[str, Any]):
    """Збереження результату бектесту"""
    from db.backtest_models import Backtest

    db = SessionLocal()
    try:
        backtest = Backtest(
            name=request.get('name', f"Бектест {datetime.now().strftime('%Y-%m-%d %H:%M')}"),
            description=request.get('description'),
            symbol=request['symbol'],
            timeframe=request['timeframe'],
            start_date=datetime.fromisoformat(request['start_date']),
            end_date=datetime.fromisoformat(request['end_date']),
            initial_balance=request['initial_balance'],
            risk_percent=request.get('risk_percent', 2.0),
            total_return_pct=request['total_return_pct'],
            final_balance=request['final_balance'],
            total_trades=request['total_trades'],
            win_trades=request['win_trades'],
            loss_trades=request['loss_trades'],
            win_rate=request['win_rate'],
            total_pnl=request['total_pnl'],
            gross_profit=request['gross_profit'],
            gross_loss=request['gross_loss'],
            profit_factor=request['profit_factor'],
            max_drawdown=request['max_drawdown'],
            sharpe_ratio=request['sharpe_ratio'],
            expectancy=request['expectancy'],
            trades=request['trades'],
            equity_values=request['equity_values'],
            equity_timestamps=request['equity_timestamps']
        )
        db.add(backtest)
        db.commit()
        db.refresh(backtest)

        return {"status": "success", "id": backtest.id, "message": "Бектест збережено"}
    except Exception as e:
        db.rollback()
        logger.error(f"Помилка збереження бектесту: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


@app.delete("/api/backtest/{backtest_id}")
async def delete_backtest(backtest_id: int):
    """Видалення бектесту"""
    from db.backtest_models import Backtest

    db = SessionLocal()
    try:
        backtest = db.query(Backtest).filter(Backtest.id == backtest_id).first()
        if not backtest:
            raise HTTPException(status_code=404, detail="Бектест не знайдено")

        db.delete(backtest)
        db.commit()

        return {"status": "success", "message": "Бектест видалено"}
    finally:
        db.close()

# ==================== API ДЛЯ КЕРУВАННЯ СТРАТЕГІЯМИ ====================

@app.get("/api/strategy/news/status")
async def get_news_strategy_status():
    """Отримання статусу новинної стратегії"""
    if order_manager_ref and order_manager_ref.news_strategy:
        return {
            "enabled": True,
            "running": order_manager_ref.news_strategy.running,
            "position_percent": order_manager_ref.news_strategy.position_percent,
            "hold_minutes": order_manager_ref.news_strategy.hold_minutes
        }
    return {"enabled": False}

@app.post("/api/strategy/news/start")
async def start_news_strategy():
    """Запуск новинної стратегії"""
    if order_manager_ref and order_manager_ref.news_strategy:
        order_manager_ref.news_strategy.running = True
        asyncio.create_task(order_manager_ref.news_strategy.run())
        return {"status": "started"}
    return {"error": "Strategy not available"}

@app.post("/api/strategy/news/stop")
async def stop_news_strategy():
    """Зупинка новинної стратегії"""
    if order_manager_ref and order_manager_ref.news_strategy:
        order_manager_ref.news_strategy.stop()
        return {"status": "stopped"}
    return {"error": "Strategy not available"}

@app.get("/api/strategy/listing/status")
async def get_listing_strategy_status():
    """Отримання статусу стратегії нових монет"""
    if order_manager_ref and order_manager_ref.listing_strategy:
        return {
            "enabled": True,
            "running": order_manager_ref.listing_strategy.running,
            "position_percent": order_manager_ref.listing_strategy.position_percent,
            "hold_minutes": order_manager_ref.listing_strategy.hold_minutes
        }
    return {"enabled": False}

@app.post("/api/strategy/listing/start")
async def start_listing_strategy():
    """Запуск стратегії нових монет"""
    if order_manager_ref and order_manager_ref.listing_strategy:
        order_manager_ref.listing_strategy.running = True
        asyncio.create_task(order_manager_ref.listing_strategy.run())
        return {"status": "started"}
    return {"error": "Strategy not available"}

@app.post("/api/strategy/listing/stop")
async def stop_listing_strategy():
    """Зупинка стратегії нових монет"""
    if order_manager_ref and order_manager_ref.listing_strategy:
        order_manager_ref.listing_strategy.stop()
        return {"status": "stopped"}
    return {"error": "Strategy not available"}

# ==================== НОВИННА ТОРГІВЛЯ ====================

@app.get("/api/news/balance")
async def get_news_balance():
    """Отримання балансу новинної стратегії"""
    db = SessionLocal()
    try:
        from db.models import NewsBalance  # <- ВИПРАВЛЕНО
        balance = db.query(NewsBalance).first()
        if not balance:
            balance = NewsBalance(amount=100.0, initial_balance=100.0)
            db.add(balance)
            db.commit()
            db.refresh(balance)
        return {"balance": balance.amount, "initial": balance.initial_balance, "total_pnl": balance.total_pnl, "total_trades": balance.total_trades, "win_rate": round(balance.win_trades / balance.total_trades * 100, 1) if balance.total_trades > 0 else 0}
    finally:
        db.close()


@app.post("/api/news/reset")
async def reset_news_balance():
    """Скидання балансу новинної стратегії"""
    db = SessionLocal()
    try:
        from db.models import NewsBalance, NewsTrade  # <- ВИПРАВЛЕНО
        # Закриваємо всі відкриті угоди
        open_trades = db.query(NewsTrade).filter(NewsTrade.status == "open").all()
        for trade in open_trades:
            trade.status = "cancelled"
            trade.exit_reason = "RESET"
        # Скидаємо баланс
        balance = db.query(NewsBalance).first()
        if balance:
            balance.amount = 100.0
            balance.initial_balance = 100.0
            balance.total_pnl = 0
            balance.total_trades = 0
            balance.win_trades = 0
        else:
            balance = NewsBalance(amount=100.0, initial_balance=100.0)
            db.add(balance)
        db.commit()
        return {"status": "success", "message": "Баланс скинуто до $100"}
    finally:
        db.close()


@app.get("/api/news/trades")
async def get_news_trades(limit: int = 50):
    """Отримання історії угод за новинами"""
    db = SessionLocal()
    try:
        from db.models import NewsTrade  # <- ВИПРАВЛЕНО
        trades = db.query(NewsTrade).order_by(NewsTrade.entry_time.desc()).limit(limit).all()
        return [
            {
                "id": t.id,
                "title": t.title[:100] if t.title else "-",
                "pair": t.pair,
                "side": t.side,
                "entry_price": t.entry_price,
                "exit_price": t.exit_price,
                "pnl": t.pnl,
                "pnl_percent": t.pnl_percent,
                "entry_time": t.entry_time.isoformat(),
                "exit_time": t.exit_time.isoformat() if t.exit_time else None,
                "status": t.status,
                "exit_reason": t.exit_reason
            }
            for t in trades
        ]
    finally:
        db.close()

# ==================== НОВІ МОНЕТИ ====================

@app.get("/api/listing/balance")
async def get_listing_balance():
    """Отримання балансу стратегії нових монет"""
    db = SessionLocal()
    try:
        from db.models import ListingBalance  # <- ВИПРАВЛЕНО
        balance = db.query(ListingBalance).first()
        if not balance:
            balance = ListingBalance(amount=100.0, initial_balance=100.0)
            db.add(balance)
            db.commit()
            db.refresh(balance)
        return {"balance": balance.amount, "initial": balance.initial_balance, "total_pnl": balance.total_pnl, "total_trades": balance.total_trades, "win_rate": round(balance.win_trades / balance.total_trades * 100, 1) if balance.total_trades > 0 else 0}
    finally:
        db.close()



@app.post("/api/listing/reset")
async def reset_listing_balance():
    """Скидання балансу стратегії нових монет"""
    db = SessionLocal()
    try:
        from db.models import ListingBalance, ListingTrade  # <- ВИПРАВЛЕНО
        open_trades = db.query(ListingTrade).filter(ListingTrade.status == "open").all()
        for trade in open_trades:
            trade.status = "cancelled"
            trade.exit_reason = "RESET"
        balance = db.query(ListingBalance).first()
        if balance:
            balance.amount = 100.0
            balance.initial_balance = 100.0
            balance.total_pnl = 0
            balance.total_trades = 0
            balance.win_trades = 0
        else:
            balance = ListingBalance(amount=100.0, initial_balance=100.0)
            db.add(balance)
        db.commit()
        return {"status": "success", "message": "Баланс скинуто до $100"}
    finally:
        db.close()


@app.get("/api/listing/trades")
async def get_listing_trades(limit: int = 50):
    """Отримання історії угод за новими монетами"""
    db = SessionLocal()
    try:
        from db.models import ListingTrade  # <- ВИПРАВЛЕНО
        trades = db.query(ListingTrade).order_by(ListingTrade.entry_time.desc()).limit(limit).all()
        return [
            {
                "id": t.id,
                "symbol": t.symbol,
                "pair": t.pair,
                "entry_price": t.entry_price,
                "exit_price": t.exit_price,
                "pnl": t.pnl,
                "pnl_percent": t.pnl_percent,
                "entry_time": t.entry_time.isoformat(),
                "exit_time": t.exit_time.isoformat() if t.exit_time else None,
                "status": t.status,
                "exit_reason": t.exit_reason
            }
            for t in trades
        ]
    finally:
        db.close()


@app.get("/api/listing/current")
async def get_current_listings():
    """Отримання поточних активних монет (відкриті позиції)"""
    db = SessionLocal()
    try:
        from db.models import ListingTrade  # <- ВИПРАВЛЕНО
        open_trades = db.query(ListingTrade).filter(ListingTrade.status == "open").all()
        return [
            {
                "id": t.id,
                "symbol": t.symbol,
                "pair": t.pair,
                "entry_price": t.entry_price,
                "entry_time": t.entry_time.isoformat(),
                "position_usdt": t.position_usdt,
                "quantity": t.quantity
            }
            for t in open_trades
        ]
    finally:
        db.close()

@app.delete("/api/forecasts/all")
async def delete_all_forecasts():
    db = SessionLocal()
    try:
        db.query(ForecastDB).delete()
        db.commit()
        return {"status": "success"}
    finally:
        db.close()


@app.get("/api/chart/pnl")
async def get_pnl_chart_data():
    db = SessionLocal()
    db_ops = DatabaseOperations(db)
    try:
        trades = db_ops.get_trades_history(limit=1000, is_paper=True)
        if not trades:
            fig = go.Figure()
            fig.update_layout(title="Немає даних для відображення", template="plotly_dark", height=400)
            return JSONResponse(content=json.loads(json.dumps(fig, cls=plotly.utils.PlotlyJSONEncoder)))
        daily_pnl = {}
        for trade in trades:
            if trade.closed_at:
                date = trade.closed_at.date().isoformat()
                daily_pnl[date] = daily_pnl.get(date, 0) + trade.pnl
        pnl_data = [{"date": date, "pnl": pnl} for date, pnl in sorted(daily_pnl.items())]
        cumulative = 0
        for item in pnl_data:
            cumulative += item["pnl"]
            item["cumulative"] = cumulative
        fig = go.Figure()
        fig.add_trace(go.Bar(
            x=[d["date"] for d in pnl_data],
            y=[d["pnl"] for d in pnl_data],
            name='Денний PnL',
            marker_color=['#00ff88' if d["pnl"] > 0 else '#ff4757' for d in pnl_data]
        ))
        fig.add_trace(go.Scatter(
            x=[d["date"] for d in pnl_data],
            y=[d["cumulative"] for d in pnl_data],
            name='Кумулятивний PnL',
            line=dict(color='#00d4ff', width=3)
        ))
        fig.update_layout(
            title="Динаміка PnL",
            xaxis_title="Дата",
            yaxis_title="PnL (USDT)",
            template="plotly_dark",
            height=400,
            paper_bgcolor='rgba(0,0,0,0)',
            plot_bgcolor='rgba(26,26,46,0.5)'
        )
        return JSONResponse(content=json.loads(json.dumps(fig, cls=plotly.utils.PlotlyJSONEncoder)))
    except Exception as e:
        logger.error(f"Помилка створення графіку PnL: {e}")
        fig = go.Figure()
        fig.update_layout(title="Немає даних для відображення", template="plotly_dark", height=400)
        return JSONResponse(content=json.loads(json.dumps(fig, cls=plotly.utils.PlotlyJSONEncoder)))
    finally:
        db.close()


@app.get("/api/chart/{pair}")
async def get_chart(pair: str, timeframe: str = "1h", limit: int = 200, show_trades: bool = True):
    from exchange.bybit_client import BybitClient
    import pandas_ta as ta
    from plotly.subplots import make_subplots
    exchange = BybitClient()
    df = exchange.get_klines(pair, timeframe, limit)
    if df is None or df.empty:
        raise HTTPException(status_code=404, detail="Дані не знайдено")
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    df['timestamp'] = df['timestamp'].dt.tz_localize('UTC').dt.tz_convert(KYIV_TZ)
    df['EMA_20'] = ta.ema(df['close'], length=20)
    df['EMA_50'] = ta.ema(df['close'], length=50)
    df['EMA_200'] = ta.ema(df['close'], length=200)
    df['RSI'] = ta.rsi(df['close'], length=14)
    df['Volume_MA'] = ta.sma(df['volume'], length=20)
    fig = make_subplots(rows=3, cols=1, shared_xaxes=True, vertical_spacing=0.03, row_heights=[0.6, 0.2, 0.2], subplot_titles=(f"{pair} - Свічковий графік з EMA", "RSI (14)", "Volume"))
    fig.add_trace(go.Candlestick(x=df['timestamp'], open=df['open'], high=df['high'], low=df['low'], close=df['close'], name='Ціна', showlegend=True), row=1, col=1)
    fig.add_trace(go.Scatter(x=df['timestamp'], y=df['EMA_20'], name='EMA 20', line=dict(color='#f39c12', width=1.5)), row=1, col=1)
    fig.add_trace(go.Scatter(x=df['timestamp'], y=df['EMA_50'], name='EMA 50', line=dict(color='#00d4ff', width=1.5)), row=1, col=1)
    fig.add_trace(go.Scatter(x=df['timestamp'], y=df['EMA_200'], name='EMA 200', line=dict(color='#ff4757', width=1.5)), row=1, col=1)
    fig.add_trace(go.Scatter(x=df['timestamp'], y=df['RSI'], name='RSI', line=dict(color='#00ff88', width=1.5)), row=2, col=1)
    fig.add_hline(y=70, line_dash="dash", line_color="#ff4757", row=2, col=1)
    fig.add_hline(y=30, line_dash="dash", line_color="#00ff88", row=2, col=1)
    fig.add_hrect(y0=30, y1=70, fillcolor="rgba(102,126,234,0.1)", line_width=0, row=2, col=1)
    colors = ['#00ff88' if close >= open else '#ff4757' for close, open in zip(df['close'], df['open'])]
    fig.add_trace(go.Bar(x=df['timestamp'], y=df['volume'], name='Volume', marker_color=colors, opacity=0.7), row=3, col=1)
    fig.add_trace(go.Scatter(x=df['timestamp'], y=df['Volume_MA'], name='Volume MA(20)', line=dict(color='#f39c12', width=1, dash='dash')), row=3, col=1)
    if show_trades:
        db = SessionLocal()
        db_ops = DatabaseOperations(db)
        try:
            trades = db_ops.get_trades_history(limit=100, is_paper=True)
            pair_trades = [t for t in trades if t.pair == pair]
            open_trades = [t for t in pair_trades if t.status.value == "PENDING"]
            closed_trades = [t for t in pair_trades if t.status.value == "CLOSED"]
            for trade in open_trades:
                trade_time = pd.Timestamp(trade.opened_at).tz_localize(KYIV_TZ)
                fig.add_trace(go.Scatter(x=[trade_time], y=[trade.entry_price], mode='markers', marker=dict(size=12, color='#ffa502', symbol='star'), text=[f"ВІДКРИТА<br>Вхід: ${trade.entry_price:.0f}"], hoverinfo='text', showlegend=False), row=1, col=1)
            for trade in closed_trades:
                entry_time = pd.Timestamp(trade.opened_at).tz_localize(KYIV_TZ)
                exit_time = pd.Timestamp(trade.closed_at).tz_localize(KYIV_TZ) if trade.closed_at else entry_time
                color = '#00ff88' if trade.pnl > 0 else '#ff4757'
                symbol = 'triangle-up' if trade.side.value == 'BUY' else 'triangle-down'
                fig.add_trace(go.Scatter(x=[entry_time], y=[trade.entry_price], mode='markers', marker=dict(size=10, color=color, symbol=symbol), text=[f"ВХІД<br>${trade.entry_price:.0f}"], hoverinfo='text', showlegend=False), row=1, col=1)
                fig.add_trace(go.Scatter(x=[exit_time], y=[trade.exit_price], mode='markers', marker=dict(size=10, color='white', symbol='circle'), text=[f"ВИХІД<br>${trade.exit_price:.0f}<br>PnL: ${trade.pnl:.2f}"], hoverinfo='text', showlegend=False), row=1, col=1)
                fig.add_trace(go.Scatter(x=[entry_time, exit_time], y=[trade.entry_price, trade.exit_price], mode='lines', line=dict(color=color, width=1.5, dash='dash'), showlegend=False), row=1, col=1)
        finally:
            db.close()
    fig.update_xaxes(tickformat="%H:%M<br>%d/%m", tickangle=-45, title_text="Час (Київ)", rangeslider_visible=False, row=1, col=1)
    fig.update_xaxes(tickformat="%H:%M<br>%d/%m", tickangle=-45, row=2, col=1)
    fig.update_xaxes(tickformat="%H:%M<br>%d/%m", tickangle=-45, title_text="Час (Київ)", row=3, col=1)
    fig.update_yaxes(title_text="Ціна (USDT)", row=1, col=1)
    fig.update_yaxes(title_text="RSI", range=[0, 100], row=2, col=1)
    fig.update_yaxes(title_text="Volume", row=3, col=1)
    fig.update_layout(
        title=f"{pair} - Технічний аналіз",
        template="plotly_dark",
        height=800,
        showlegend=True,
        paper_bgcolor='rgba(0,0,0,0)',
        plot_bgcolor='rgba(26,26,46,0.5)',
        hovermode='x unified',
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        hoverlabel=dict(bgcolor="rgba(0,0,0,0.8)", font_size=12, font_family="monospace")
    )
    return JSONResponse(content=json.loads(json.dumps(fig, cls=plotly.utils.PlotlyJSONEncoder)))


@app.get("/api/analysis/{pair}")
async def get_market_analysis(pair: str):
    if order_manager_ref and hasattr(order_manager_ref, 'analyze_market'):
        try:
            analysis = order_manager_ref.analyze_market(pair)
            return analysis
        except Exception as e:
            return {"error": str(e)}
    return {"error": "Order manager not available"}


@app.get("/api/settings")
async def get_settings():
    return {
        "trading": {
            "pairs": config.get('trading.pairs', ['BTCUSDT', 'ETHUSDT', 'SOLUSDT']),
            "base_timeframe": config.get('trading.base_timeframe', '15m'),
            "signal_check_interval": config.get('trading.signal_check_interval', 30),
            "trading_hours": config.get('trading.trading_hours', {"enabled": False, "start": "09:00", "end": "21:00"}),
        },
        "strategy": {
            "ema_fast": config.get('strategy.ema_fast', 21),  # ВИПРАВЛЕНО: 21 замість 50
            "ema_slow": config.get('strategy.ema_slow', 200),
            "rsi_period": config.get('strategy.rsi_period', 14),
            "rsi_min": config.get('strategy.rsi_min', 35),
            "rsi_max": config.get('strategy.rsi_max', 70),
            "take_profit_percent": config.get('strategy.take_profit_percent', 2.0),
            "stop_loss_percent": config.get('strategy.stop_loss_percent', 1.5),
            "use_bollinger": config.get('strategy.use_bollinger', True),
            # ДОДАНО: ATR множники
            "atr_sl_multiplier": config.get('strategy.atr_sl_multiplier', 1.5),
            "atr_tp_multiplier": config.get('strategy.atr_tp_multiplier', 2.5),
        },
        "risk": {
            "risk_per_trade": config.get('risk.risk_per_trade', 2.0),
            "max_open_trades": config.get('risk.max_open_trades', 5),
            "max_daily_loss": config.get('risk.max_daily_loss', 5.0),
            "use_kelly": config.get('risk.use_kelly', True),
        },
        "notifications": {
            "web_push": config.get('web.enable_web_push', True),
            "telegram": config.get('telegram.enabled', True)
        }
    }


@app.post("/api/settings")
async def update_settings(settings: Dict[str, Any]):
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            current_config = yaml.safe_load(f)
        for category, values in settings.items():
            if category in current_config:
                for key, value in values.items():
                    if key in current_config[category]:
                        current_config[category][key] = value
        with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
            yaml.dump(current_config, f, allow_unicode=True, default_flow_style=False)
        from utils.config_loader import reload_config
        reload_config()
        return {"status": "success", "message": "Налаштування збережено"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/logs")
async def get_logs(level: Optional[str] = None, category: Optional[str] = None, limit: int = 100, offset: int = 0):
    """Отримання логів з фільтрацією по рівню та категорії"""
    db = SessionLocal()
    try:
        query = db.query(Log)
        if level:
            query = query.filter(Log.level == level.upper())
        if category:
            query = query.filter(Log.category == category)

        total = query.count()
        logs = query.order_by(Log.timestamp.desc()).offset(offset).limit(limit).all()

        def convert_to_kyiv(utc_time):
            if utc_time:
                if utc_time.tzinfo is None:
                    utc_time = pytz.UTC.localize(utc_time)
                kyiv_time = utc_time.astimezone(KYIV_TZ)
                return kyiv_time.isoformat()
            return None

        return {
            "logs": [{
                "id": l.id,
                "level": l.level,
                "module": l.module,
                "category": l.category,
                "message": l.message,
                "timestamp": convert_to_kyiv(l.timestamp)
            } for l in logs],
            "total": total,
            "limit": limit,
            "offset": offset
        }
    finally:
        db.close()


@app.get("/api/strategies/status")
async def get_strategies_status():
    """Отримання статусу всіх стратегій"""
    if not order_manager_ref:
        return {"error": "Order manager not available"}

    return {
        "main_strategy": {
            "enabled": True,
            "running": order_manager_ref.running,
            "pairs": order_manager_ref.pairs,
            "base_timeframe": order_manager_ref.base_timeframe
        },
        "news_strategy": {
            "enabled": config.get('news_strategy.enabled', True),
            "running": order_manager_ref.news_strategy.running if order_manager_ref.news_strategy else False,
            "position_percent": config.get('news_strategy.position_percent', 5.0)
        },
        "listing_strategy": {
            "enabled": config.get('listing_strategy.enabled', True),
            "running": order_manager_ref.listing_strategy.running if order_manager_ref.listing_strategy else False,
            "position_percent": config.get('listing_strategy.position_percent', 3.0)
        }
    }

@app.get("/api/logs/categories")
async def get_log_categories():
    """Отримання списку доступних категорій логів"""
    db = SessionLocal()
    try:
        categories = db.query(Log.category).distinct().all()
        categories_list = [c[0] for c in categories if c[0]]
        return {"categories": categories_list}
    finally:
        db.close()


@app.delete("/api/logs")
async def clear_logs():
    db = SessionLocal()
    try:
        db.query(Log).delete()
        db.commit()
        return {"status": "success", "message": "Логи очищено"}
    finally:
        db.close()


@app.get("/api/forecasts/stats")
async def get_forecast_statistics():
    """Отримання статистики прогнозів"""
    from core.forecast_analyzer import ForecastAnalyzer
    analyzer = ForecastAnalyzer()
    stats = await analyzer.get_forecast_statistics()
    return stats


@app.get("/api/forecasts/analysis/{forecast_id}")
async def get_forecast_analysis(forecast_id: str):
    """Детальний аналіз конкретного прогнозу"""
    db = SessionLocal()
    try:
        fid = safe_parse_forecast_id(forecast_id)
        forecast = db.query(ForecastDB).filter(ForecastDB.forecast_id == fid).first()

        if not forecast:
            raise HTTPException(status_code=404, detail="Прогноз не знайдено")

        # Розраховуємо додаткову аналітику
        analysis = {
            "id": forecast.forecast_id,
            "pair": forecast.pair,
            "signal_type": forecast.signal_type,
            "entry_price": forecast.entry_price,
            "target_price": forecast.target_price,
            "max_price_reached": forecast.max_price_reached,
            "min_price_reached": forecast.min_price_reached,
            "hit_percentage": forecast.hit_percentage,
            "quality_score": forecast.quality_score,
            "confidence": forecast.confidence,
            "result": forecast.result,
            "status": forecast.status,
            "created_at": make_aware(forecast.created_at).isoformat(),
            "expires_at": make_aware(forecast.expires_at).isoformat(),
            "target_hit_time": make_aware(forecast.target_hit_time).isoformat() if forecast.target_hit_time else None,
            "actual_profit_pct": forecast.actual_profit_pct,
            "current_pnl": forecast.current_pnl,
        }

        # Додаємо індикатори, якщо вони є
        if forecast.indicators_snapshot:
            try:
                analysis["indicators"] = json.loads(forecast.indicators_snapshot)
            except:
                analysis["indicators"] = None

        return analysis

    except ValueError:
        raise HTTPException(status_code=400, detail="Невірний ID прогнозу")
    finally:
        db.close()

@app.post("/api/reset")
async def reset_paper_trading():
    if order_manager_ref and order_manager_ref.paper_engine:
        order_manager_ref.paper_engine.reset()
        return {"status": "success"}
    return {"status": "error", "message": "Order manager not available"}


@app.post("/api/stop")
async def stop_bot():
    if order_manager_ref:
        order_manager_ref.running = False
        return {"status": "success"}
    return {"status": "error", "message": "Order manager not available"}


@app.post("/api/start")
async def start_bot():
    if order_manager_ref:
        order_manager_ref.running = True
        return {"status": "success"}
    return {"status": "error", "message": "Order manager not available"}


@app.post("/api/test/trade")
async def create_test_trade():
    from db.models import OrderSide, OrderStatus
    import random
    db = SessionLocal()
    try:
        now = datetime.now()
        entry_price = 43000 + random.randint(-500, 500)
        exit_price = entry_price * (1 + random.uniform(0.005, 0.03))
        test_trade = Trade(
            pair="BTCUSDT", side=OrderSide.BUY, entry_price=entry_price, exit_price=exit_price,
            quantity=0.001, pnl=(exit_price - entry_price) * 0.001,
            pnl_percent=((exit_price - entry_price) / entry_price) * 100 if entry_price > 0 else 0,
            is_paper=1, status=OrderStatus.CLOSED, opened_at=now - timedelta(days=random.randint(1, 5)),
            closed_at=now - timedelta(hours=random.randint(1, 48)), take_profit=entry_price * 1.02, stop_loss=entry_price * 0.98
        )
        db.add(test_trade)
        db.commit()
        return {"status": "success", "closed_trade": {"entry": test_trade.entry_price, "exit": test_trade.exit_price, "pnl": test_trade.pnl}}
    except Exception as e:
        db.rollback()
        return {"status": "error", "message": str(e)}
    finally:
        db.close()


@app.delete("/api/test/trades")
async def clear_test_trades():
    db = SessionLocal()
    try:
        db.query(Trade).filter(Trade.is_paper == 1).delete()
        db.commit()
        from db.operations import DatabaseOperations
        db_ops = DatabaseOperations(db)
        db_ops.reset_paper_balance(100.0)
        return {"status": "success", "message": "Тестові угоди видалено"}
    except Exception as e:
        db.rollback()
        return {"status": "error", "message": str(e)}
    finally:
        db.close()


@app.get("/api/stats/advanced")
async def get_advanced_stats():
    db = SessionLocal()
    db_ops = DatabaseOperations(db)
    try:
        trades = db_ops.get_trades_history(limit=1000, is_paper=True)
        if not trades or len(trades) < 5:
            return {"total_trades": 0, "win_rate": 0, "profit_factor": 0, "max_drawdown": 0, "sharpe_ratio": 0, "expectancy": 0, "kelly_criterion": 0}
        wins = [t for t in trades if t.pnl > 0]
        losses = [t for t in trades if t.pnl < 0]
        win_rate = len(wins) / len(trades) * 100 if trades else 0
        avg_win = sum(t.pnl for t in wins) / len(wins) if wins else 0
        avg_loss = abs(sum(t.pnl for t in losses) / len(losses)) if losses else 0
        gross_profit = sum(t.pnl for t in wins) if wins else 0
        gross_loss = abs(sum(t.pnl for t in losses)) if losses else 1
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else 0
        cumulative = 0
        max_drawdown = 0
        peak = 0
        for t in trades:
            cumulative += t.pnl
            if cumulative > peak:
                peak = cumulative
            drawdown = peak - cumulative
            if drawdown > max_drawdown:
                max_drawdown = drawdown
        returns = [t.pnl for t in trades]
        avg_return = sum(returns) / len(returns) if returns else 0
        std_return = (sum((r - avg_return) ** 2 for r in returns) / len(returns)) ** 0.5 if returns else 1
        sharpe_ratio = (avg_return / std_return) * (252 ** 0.5) if std_return > 0 else 0
        b = avg_win / avg_loss if avg_loss > 0 else 1
        p = win_rate / 100
        q = 1 - p
        kelly = (p * b - q) / b if b > 0 else 0
        kelly = max(0, min(kelly, 0.25))
        return {
            "total_trades": len(trades), "win_trades": len(wins), "loss_trades": len(losses),
            "win_rate": round(win_rate, 2), "avg_win": round(avg_win, 2), "avg_loss": round(avg_loss, 2),
            "profit_factor": round(profit_factor, 2), "max_drawdown": round(max_drawdown, 2),
            "sharpe_ratio": round(sharpe_ratio, 2), "expectancy": round((win_rate/100 * avg_win - (1-win_rate/100) * avg_loss), 2),
            "kelly_criterion": round(kelly * 100, 2)
        }
    except Exception as e:
        logger.error(f"Помилка розрахунку статистики: {e}")
        return {"error": str(e)}
    finally:
        db.close()


@app.post("/api/push/subscribe")
async def push_subscribe(subscription: dict):
    push_subscriptions.append(subscription)
    return {"status": "success"}


@app.get("/", response_class=HTMLResponse)
async def root():
    """Головна сторінка - читає зовнішній HTML файл"""
    html_path = Path(__file__).parent / "index.html"
    try:
        with open(html_path, 'r', encoding='utf-8') as f:
            return HTMLResponse(content=f.read())
    except FileNotFoundError:
        logger.error(f"Файл {html_path} не знайдено")
        return HTMLResponse(content="<h1>Помилка: index.html не знайдено</h1>")



def start_web_server(host="0.0.0.0", port=8000):
    import uvicorn
    uvicorn.run(app, host=host, port=port, log_level="warning")