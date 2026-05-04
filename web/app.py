"""
FastAPI веб-інтерфейс для автотрейдинг бота
Повна версія: налаштування, прогнози, графіки, логі, аналітика
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
                "position_usdt": f.position_usdt,  # ← ДОДАТИ ЦЕ
                "created_at": make_aware(f.created_at).isoformat(),
                "expires_at": make_aware(f.expires_at).isoformat(),
                "time_remaining": max(0, (make_aware(f.expires_at) - now).total_seconds()),
                "status": f.status,
                "profit_potential": ((f.target_price - f.entry_price) / f.entry_price) * 100
            }
            for f in active
        ]
    except Exception as e:
        logger.error(f"Помилка отримання прогнозів: {e}")
        return []
    finally:
        db.close()


async def create_forecast_internal(pair, signal_type, entry_price, target_price, confidence,
                                   position_quantity=0.0, position_usdt=0.0):
    """Створення прогнозу з розміром позиції"""
    db = SessionLocal()
    try:
        forecast_id = datetime.now().timestamp()
        now = get_current_time()

        existing = db.query(ForecastDB).filter(
            ForecastDB.pair == pair,
            ForecastDB.signal_type == signal_type,
            ForecastDB.status == "active"
        ).first()

        if existing:
            logger.debug(f"Прогноз для {pair} {signal_type} вже існує")
            return None

        forecast = ForecastDB(
            forecast_id=forecast_id,
            pair=pair,
            signal_type=signal_type,
            entry_price=entry_price,
            target_price=target_price,
            current_price=entry_price,
            confidence=confidence,
            position_quantity=position_quantity,  # ← змінено назву
            position_usdt=position_usdt,
            created_at=now,
            expires_at=now + timedelta(hours=4),
            status="active"
        )
        db.add(forecast)
        db.commit()

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
            "expires_at": (now + timedelta(hours=4)).isoformat(),
            "time_remaining": 4 * 3600,
            "status": "active",
            "profit_potential": ((target_price - entry_price) / entry_price) * 100
        }

        for ws in active_websockets:
            try:
                await ws.send_json({
                    "type": "new_forecast",
                    "forecast": forecast_dict
                })
            except:
                pass

        logger.info(f"✅ Прогноз збережено: {pair} {signal_type} | ${position_usdt:.2f}")
        return forecast

    except Exception as e:
        logger.error(f"Помилка створення прогнозу: {e}")
        db.rollback()
        return None
    finally:
        db.close()


async def update_forecast_prices():
    from exchange.bybit_client import BybitClient
    exchange = BybitClient()

    db = SessionLocal()
    try:
        active = db.query(ForecastDB).filter(ForecastDB.status == "active").all()
        for forecast in active:
            try:
                current_price = exchange.get_current_price(forecast.pair)
                if current_price:
                    forecast.current_price = current_price
                    db.commit()
            except Exception as e:
                logger.error(f"Помилка оновлення ціни для {forecast.pair}: {e}")
    finally:
        db.close()


@app.get("/health")
async def health_check():
    """Перевірка стану бота для моніторингу"""
    import platform

    # Статус бота
    bot_status = "running" if order_manager_ref and order_manager_ref.running else "stopped"

    # Статус БД
    db_status = "ok"
    db_error = None
    try:
        db = SessionLocal()
        db.execute(text("SELECT 1"))  # ← ДОДАТИ text()
        db.close()
    except Exception as e:
        db_status = "error"
        db_error = str(e)[:100]

    # Баланс
    balance = 0
    try:
        db = SessionLocal()
        db_ops = DatabaseOperations(db)
        balance = db_ops.get_balance("USDT", is_paper=True)
        db.close()
    except Exception as e:
        balance = 100.0

    # Час роботи
    uptime_seconds = None
    if hasattr(order_manager_ref, 'start_time') and order_manager_ref.start_time:
        try:
            uptime_delta = get_current_time() - order_manager_ref.start_time
            uptime_seconds = int(uptime_delta.total_seconds())
        except:
            pass

    # Статус healthy якщо бот працює
    is_healthy = bot_status == "running"

    return {
        "status": "healthy" if is_healthy else "unhealthy",
        "timestamp": get_current_time().isoformat(),
        "server_time": get_current_time().isoformat(),
        "bot": {
            "status": bot_status,
            "mode": config.bot_mode,
            "version": "3.0.0",
            "uptime_seconds": uptime_seconds
        },
        "database": {
            "status": db_status,
            "error": db_error
        },
        "balance": {
            "paper_usdt": balance
        },
        "system": {
            "python_version": platform.python_version(),
            "platform": platform.system()
        }
    }


@app.post("/api/settings/testing")
async def update_testing_settings(settings: Dict[str, Any]):
    """Оновлення testing налаштувань"""
    import yaml
    from pathlib import Path

    config_path = Path(__file__).parent.parent / "config.yaml"

    try:
        # Читаємо поточний конфіг
        with open(config_path, 'r', encoding='utf-8') as f:
            current_config = yaml.safe_load(f)

        # Оновлюємо testing секцію
        if 'testing' not in current_config:
            current_config['testing'] = {}

        for key, value in settings.items():
            current_config['testing'][key] = value

        # Зберігаємо конфіг
        with open(config_path, 'w', encoding='utf-8') as f:
            yaml.dump(current_config, f, allow_unicode=True, default_flow_style=False)

        # Оновлюємо глобальний конфіг
        from utils.config_loader import reload_config
        reload_config()

        return {"status": "success", "message": "Налаштування збережено"}

    except Exception as e:
        logger.error(f"Помилка збереження testing налаштувань: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    except Exception as e:
        logger.error(f"Помилка збереження testing налаштувань: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/trade/chart/{trade_id}")
async def get_trade_chart(trade_id: int, timeframe: str = "1h"):
    """Отримання графіку для конкретної угоди"""
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

        # Отримуємо дані
        df = exchange.get_klines(trade.pair, timeframe, limit=300)

        if df is None or df.empty:
            fig = go.Figure()
            fig.update_layout(title=f"Немає даних для {trade.pair}", template="plotly_dark", height=600)
            return JSONResponse(content=json.loads(json.dumps(fig, cls=plotly.utils.PlotlyJSONEncoder)))

        # Конвертуємо час
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df['display_time'] = df['timestamp'].dt.tz_localize('UTC').dt.tz_convert(KYIV_TZ)

        # Розраховуємо EMA
        df['EMA_20'] = ta.ema(df['close'], length=20)
        df['EMA_50'] = ta.ema(df['close'], length=50)
        df['EMA_200'] = ta.ema(df['close'], length=200)

        # Час входу та виходу
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

        # Свічковий графік
        fig.add_trace(go.Candlestick(
            x=df['display_time'],
            open=df['open'],
            high=df['high'],
            low=df['low'],
            close=df['close'],
            name='Ціна',
            showlegend=True
        ))

        # EMA лінії
        fig.add_trace(go.Scatter(
            x=df['display_time'], y=df['EMA_20'],
            name='EMA 20', line=dict(color='#f39c12', width=1.5)
        ))
        fig.add_trace(go.Scatter(
            x=df['display_time'], y=df['EMA_50'],
            name='EMA 50', line=dict(color='#00d4ff', width=1.5)
        ))
        fig.add_trace(go.Scatter(
            x=df['display_time'], y=df['EMA_200'],
            name='EMA 200', line=dict(color='#ff4757', width=1.5)
        ))

        # Точка входу
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

        # Точка виходу (якщо є)
        if exit_time:
            exit_color = '#00ff88' if trade.pnl > 0 else '#ff4757'
            fig.add_trace(go.Scatter(
                x=[exit_time],
                y=[trade.exit_price],
                mode='markers',
                name='Вихід',
                marker=dict(size=14, color='white', symbol='circle', line=dict(width=2, color=exit_color)),
                text=[
                    f"<b>ВИХІД</b><br>Ціна: ${trade.exit_price:.0f}<br>PnL: ${trade.pnl:.2f} ({trade.pnl_percent:.1f}%)"],
                hoverinfo='text'
            ))

            # Лінія між входом і виходом
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
            title=f"{trade.pair} - Угода",
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

# ============ WEBSOCKET ============

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

@app.get("/api/current_price/{pair}")
async def get_current_price(pair: str):
    """Отримання поточної ціни для пари"""
    from exchange.bybit_client import BybitClient
    exchange = BybitClient()
    try:
        price = exchange.get_current_price(pair)
        return {"price": price}
    except Exception as e:
        logger.error(f"Помилка отримання ціни для {pair}: {e}")
        return {"price": None}

# ============ API ЕНДПОІНТИ ============

@app.get("/api/status")
async def get_status():
    db = SessionLocal()
    db_ops = DatabaseOperations(db)
    try:
        next_forecast = await get_next_forecast_time()
        stats = db_ops.get_stats(is_paper=True)
        balance = db_ops.get_balance("USDT", is_paper=True)

        if balance < 10:
            db_ops.reset_paper_balance(100.0)
            balance = 100.0

        return {
            "status": "running" if order_manager_ref and order_manager_ref.running else "stopped",
            "mode": config.bot_mode,
            "balance": balance,
            "open_trades": len(db_ops.get_open_trades(is_paper=True)),
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
    """Примусове закриття угоди"""
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
                # Додаємо 3 години до UTC часу
                "opened_at": (t.opened_at + timedelta(hours=3)).isoformat() if t.opened_at else None,
                "closed_at": (t.closed_at + timedelta(hours=3)).isoformat() if t.closed_at else None
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


@app.get("/api/forecasts")
async def get_forecasts():
    return await get_active_forecasts()


@app.get("/api/forecasts/history")
async def get_forecasts_history(limit: int = 50, offset: int = 0):
    """Отримання історії всіх прогнозів (активні + завершені)"""
    db = SessionLocal()
    try:
        # Отримуємо всі прогнози, сортовані за часом створення (нові зверху)
        query = db.query(ForecastDB).order_by(ForecastDB.created_at.desc())
        total = query.count()
        forecasts = query.offset(offset).limit(limit).all()

        result = []
        for f in forecasts:
            # Розраховуємо результат
            if f.status == "completed":
                if f.signal_type == "LONG":
                    profit_percent = ((f.current_price - f.entry_price) / f.entry_price) * 100
                else:
                    profit_percent = ((f.entry_price - f.current_price) / f.entry_price) * 100
                if profit_percent > 0:
                    result_text = f"✅ Виконано (+{profit_percent:.1f}%)"
                else:
                    result_text = f"❌ Виконано ({profit_percent:.1f}%)"
            elif f.status == "expired":
                result_text = "⏰ Прострочено"
            elif f.status == "active":
                # Для активних прогнозів показуємо поточний прибуток
                if f.signal_type == "LONG":
                    current_profit = ((f.current_price - f.entry_price) / f.entry_price) * 100
                else:
                    current_profit = ((f.entry_price - f.current_price) / f.entry_price) * 100
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
    """Отримання інформації про прогноз для модального вікна"""
    db = SessionLocal()
    try:
        fid = float(forecast_id)
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
    finally:
        db.close()

@app.get("/api/settings/testing")
async def get_testing_settings():
    """Отримання testing налаштувань"""
    try:
        return {
            "create_trades_from_forecasts": config.get('testing.create_trades_from_forecasts', False),
            "forecast_position_percent": config.get('testing.forecast_position_percent', 25.0)
        }
    except Exception as e:
        logger.error(f"Помилка отримання testing налаштувань: {e}")
        return {
            "create_trades_from_forecasts": False,
            "forecast_position_percent": 25.0
        }

@app.get("/api/forecast/chart/{forecast_id}")
async def get_forecast_chart(forecast_id: str, timeframe: str = "1h"):
    """Отримання графіку для конкретного прогнозу - ВИПРАВЛЕНО"""
    from exchange.bybit_client import BybitClient
    import pandas_ta as ta
    import traceback

    db = SessionLocal()
    try:
        fid = float(forecast_id)
        forecast = db.query(ForecastDB).filter(ForecastDB.forecast_id == fid).first()
        if not forecast:
            raise HTTPException(status_code=404, detail="Прогноз не знайдено")

        logger.info(f"Створення графіку для прогнозу {forecast_id}")
        logger.info(f"forecast.created_at (з БД): {forecast.created_at}")

        exchange = BybitClient()

        # Отримуємо дані
        df = exchange.get_klines(forecast.pair, timeframe, limit=300)

        if df is None or df.empty:
            fig = go.Figure()
            fig.update_layout(title=f"Немає даних для {forecast.pair}", template="plotly_dark", height=550)
            return JSONResponse(content=json.loads(json.dumps(fig, cls=plotly.utils.PlotlyJSONEncoder)))

        # ByBit повертає timestamp в мілісекундах (UTC)
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')

        # Створюємо колонку з київським часом для відображення
        df['display_time'] = df['timestamp'].dt.tz_localize('UTC').dt.tz_convert(KYIV_TZ)

        # forecast.created_at ВЖЕ в київському часі (з БД)
        # Просто використовуємо його як є, без додаткової конвертації
        entry_time = forecast.created_at

        # Якщо entry_time без часового поясу - додаємо київський
        if entry_time.tzinfo is None:
            entry_time = KYIV_TZ.localize(entry_time)

        # Конвертуємо entry_time в UTC для фільтрації даних
        entry_time_utc = entry_time.astimezone(pytz.UTC)

        logger.info(f"Час входу (Київ): {entry_time}")
        logger.info(f"Час входу (UTC): {entry_time_utc}")

        # Фільтруємо дані - показуємо 24 години до прогнозу
        start_filter = entry_time_utc - pd.Timedelta(hours=24)

        # Конвертуємо timestamp в UTC для порівняння
        df['timestamp_utc'] = df['timestamp'].dt.tz_localize('UTC')
        df_filtered = df[df['timestamp_utc'] >= start_filter]

        if df_filtered.empty:
            df_filtered = df.tail(150)
            df_filtered['display_time'] = df_filtered['timestamp'].dt.tz_localize('UTC').dt.tz_convert(KYIV_TZ)

        # Розраховуємо EMA
        df_filtered['EMA_20'] = ta.ema(df_filtered['close'], length=20)
        df_filtered['EMA_50'] = ta.ema(df_filtered['close'], length=50)
        df_filtered['EMA_200'] = ta.ema(df_filtered['close'], length=200)

        fig = go.Figure()

        # Свічковий графік
        fig.add_trace(go.Candlestick(
            x=df_filtered['display_time'],
            open=df_filtered['open'],
            high=df_filtered['high'],
            low=df_filtered['low'],
            close=df_filtered['close'],
            name='Ціна',
            showlegend=True
        ))

        # EMA лінії
        fig.add_trace(go.Scatter(
            x=df_filtered['display_time'], y=df_filtered['EMA_20'],
            name='EMA 20', line=dict(color='#f39c12', width=1.5)
        ))
        fig.add_trace(go.Scatter(
            x=df_filtered['display_time'], y=df_filtered['EMA_50'],
            name='EMA 50', line=dict(color='#00d4ff', width=1.5)
        ))
        fig.add_trace(go.Scatter(
            x=df_filtered['display_time'], y=df_filtered['EMA_200'],
            name='EMA 200', line=dict(color='#ff4757', width=1.5)
        ))

        # Точка входу (використовуємо КИЇВСЬКИЙ час без змін)
        entry_color = '#00ff88' if forecast.signal_type == 'LONG' else '#ff4757'
        entry_symbol = 'triangle-up' if forecast.signal_type == 'LONG' else 'triangle-down'

        fig.add_trace(go.Scatter(
            x=[entry_time],
            y=[forecast.entry_price],
            mode='markers',
            name=f"Вхід {forecast.signal_type}",
            marker=dict(
                size=20,
                color=entry_color,
                symbol=entry_symbol,
                line=dict(width=2, color='white')
            ),
            text=[
                f"<b>{forecast.signal_type} ВХІД</b><br>Ціна: ${forecast.entry_price:.0f}<br>Ціль: ${forecast.target_price:.0f}<br>Впевненість: {forecast.confidence}%<br>Час: {entry_time.strftime('%Y-%m-%d %H:%M')}"],
            hoverinfo='text',
            hovertemplate='%{text}<extra></extra>'
        ))

        # Поточна ціна (остання свічка)
        current_price = df_filtered['close'].iloc[-1]
        current_display_time = df_filtered['display_time'].iloc[-1]

        # Лінія від входу до поточної ціни (тільки якщо поточний час після входу)
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

        # Горизонтальна лінія цілі
        fig.add_hline(
            y=forecast.target_price,
            line_dash="dash",
            line_color="#00ff88",
            annotation_text=f"🎯 Ціль: ${forecast.target_price:.0f}",
            annotation_font_size=11,
            annotation_font_color="#00ff88",
            annotation_x=0.02
        )

        fig.update_xaxes(
            tickformat="%H:%M<br>%d/%m",
            tickangle=-45,
            title_text="Час (Київ)",
            rangeslider_visible=False
        )

        fig.update_layout(
            title=f"{forecast.pair} - Прогноз {forecast.signal_type} від {entry_time.strftime('%Y-%m-%d %H:%M')}",
            xaxis_title="Час (Київ)",
            yaxis_title="Ціна (USDT)",
            template="plotly_dark",
            height=550,
            paper_bgcolor='rgba(0,0,0,0)',
            plot_bgcolor='rgba(26,26,46,0.5)',
            hovermode='closest',
            hoverlabel=dict(
                bgcolor="rgba(0,0,0,0.8)",
                font_size=12,
                font_family="monospace"
            )
        )

        return JSONResponse(content=json.loads(json.dumps(fig, cls=plotly.utils.PlotlyJSONEncoder)))

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
        fid = float(forecast_id)
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
async def get_chart(
        pair: str,
        timeframe: str = "1h",
        limit: int = 200,
        show_trades: bool = True,
        start_date: str = None,
        end_date: str = None
):
    """Отримання свічкового графіку з EMA та опціональними точками угод"""
    from exchange.bybit_client import BybitClient
    import pandas_ta as ta

    exchange = BybitClient()
    df = exchange.get_klines(pair, timeframe, limit)

    if df is None or df.empty:
        raise HTTPException(status_code=404, detail="Дані не знайдено")

    # Конвертуємо час з UTC в Kyiv
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    df['timestamp'] = df['timestamp'].dt.tz_localize('UTC').dt.tz_convert(KYIV_TZ)

    # Розраховуємо EMA
    df['EMA_20'] = ta.ema(df['close'], length=20)
    df['EMA_50'] = ta.ema(df['close'], length=50)
    df['EMA_200'] = ta.ema(df['close'], length=200)

    # RSI для нижнього графіку
    df['RSI'] = ta.rsi(df['close'], length=14)

    # Volume
    df['Volume_MA'] = ta.sma(df['volume'], length=20)

    from plotly.subplots import make_subplots

    # Створюємо графік з 3 рядами
    fig = make_subplots(
        rows=3, cols=1,
        shared_xaxes=True,
        vertical_spacing=0.03,
        row_heights=[0.6, 0.2, 0.2],
        subplot_titles=(f"{pair} - Свічковий графік з EMA", "RSI (14)", "Volume")
    )

    # Ряд 1: Свічковий графік + EMA
    fig.add_trace(go.Candlestick(
        x=df['timestamp'],
        open=df['open'],
        high=df['high'],
        low=df['low'],
        close=df['close'],
        name='Ціна',
        showlegend=True
    ), row=1, col=1)

    fig.add_trace(go.Scatter(
        x=df['timestamp'], y=df['EMA_20'],
        name='EMA 20', line=dict(color='#f39c12', width=1.5)
    ), row=1, col=1)

    fig.add_trace(go.Scatter(
        x=df['timestamp'], y=df['EMA_50'],
        name='EMA 50', line=dict(color='#00d4ff', width=1.5)
    ), row=1, col=1)

    fig.add_trace(go.Scatter(
        x=df['timestamp'], y=df['EMA_200'],
        name='EMA 200', line=dict(color='#ff4757', width=1.5)
    ), row=1, col=1)

    # Ряд 2: RSI
    fig.add_trace(go.Scatter(
        x=df['timestamp'], y=df['RSI'],
        name='RSI', line=dict(color='#00ff88', width=1.5)
    ), row=2, col=1)

    # Рівні RSI
    fig.add_hline(y=70, line_dash="dash", line_color="#ff4757", row=2, col=1)
    fig.add_hline(y=30, line_dash="dash", line_color="#00ff88", row=2, col=1)
    fig.add_hrect(y0=30, y1=70, fillcolor="rgba(102,126,234,0.1)", line_width=0, row=2, col=1)

    # Ряд 3: Volume
    colors = ['#00ff88' if close >= open else '#ff4757' for close, open in zip(df['close'], df['open'])]
    fig.add_trace(go.Bar(
        x=df['timestamp'], y=df['volume'],
        name='Volume', marker_color=colors, opacity=0.7
    ), row=3, col=1)

    # Лінія середнього об'єму
    fig.add_trace(go.Scatter(
        x=df['timestamp'], y=df['Volume_MA'],
        name='Volume MA(20)', line=dict(color='#f39c12', width=1, dash='dash')
    ), row=3, col=1)

    # Додаємо точки угод, якщо потрібно (через параметр)
    if show_trades:
        db = SessionLocal()
        db_ops = DatabaseOperations(db)
        try:
            trades = db_ops.get_trades_history(limit=100, is_paper=True)
            pair_trades = [t for t in trades if t.pair == pair]

            # Відкриті угоди (активні)
            open_trades = [t for t in pair_trades if t.status.value == "PENDING"]
            # Закриті угоди
            closed_trades = [t for t in pair_trades if t.status.value == "CLOSED"]

            # Точки відкритих угод (жовті трикутники)
            for trade in open_trades:
                trade_time = pd.Timestamp(trade.opened_at).tz_localize(KYIV_TZ)
                fig.add_trace(go.Scatter(
                    x=[trade_time],
                    y=[trade.entry_price],
                    mode='markers',
                    name='Відкрита угода',
                    marker=dict(
                        size=12,
                        color='#ffa502',
                        symbol='star',
                        line=dict(width=1, color='white')
                    ),
                    text=[f"ВІДКРИТА<br>Вхід: ${trade.entry_price:.0f}<br>Кількість: {trade.quantity}"],
                    hoverinfo='text',
                    showlegend=False
                ), row=1, col=1)

            # Точки закритих угод
            for trade in closed_trades:
                entry_time = pd.Timestamp(trade.opened_at).tz_localize(KYIV_TZ)
                exit_time = pd.Timestamp(trade.closed_at).tz_localize(KYIV_TZ) if trade.closed_at else entry_time

                color = '#00ff88' if trade.pnl > 0 else '#ff4757'
                symbol = 'triangle-up' if trade.side.value == 'BUY' else 'triangle-down'

                # Точка входу
                fig.add_trace(go.Scatter(
                    x=[entry_time],
                    y=[trade.entry_price],
                    mode='markers',
                    name='Вхід',
                    marker=dict(size=10, color=color, symbol=symbol),
                    text=[f"ВХІД<br>Ціна: ${trade.entry_price:.0f}<br>PnL: ${trade.pnl:.2f}"],
                    hoverinfo='text',
                    showlegend=False
                ), row=1, col=1)

                # Точка виходу
                fig.add_trace(go.Scatter(
                    x=[exit_time],
                    y=[trade.exit_price],
                    mode='markers',
                    name='Вихід',
                    marker=dict(size=10, color='white', symbol='circle'),
                    text=[f"ВИХІД<br>Ціна: ${trade.exit_price:.0f}<br>PnL: ${trade.pnl:.2f}"],
                    hoverinfo='text',
                    showlegend=False
                ), row=1, col=1)

                # Лінія між входом і виходом (тільки для закритих)
                fig.add_trace(go.Scatter(
                    x=[entry_time, exit_time],
                    y=[trade.entry_price, trade.exit_price],
                    mode='lines',
                    name='Угода',
                    line=dict(color=color, width=1.5, dash='dash'),
                    showlegend=False
                ), row=1, col=1)
        finally:
            db.close()

    # Налаштування осі X
    fig.update_xaxes(
        tickformat="%H:%M<br>%d/%m",
        tickangle=-45,
        title_text="Час (Київ)",
        rangeslider_visible=False,
        row=1, col=1
    )
    fig.update_xaxes(tickformat="%H:%M<br>%d/%m", tickangle=-45, row=2, col=1)
    fig.update_xaxes(tickformat="%H:%M<br>%d/%m", tickangle=-45, title_text="Час (Київ)", row=3, col=1)

    # Налаштування осей Y
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
        legend=dict(
            orientation="h",
            yanchor="bottom",
            y=1.02,
            xanchor="right",
            x=1
        ),
        hoverlabel=dict(
            bgcolor="rgba(0,0,0,0.8)",
            font_size=12,
            font_family="monospace"
        )
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
            "base_timeframe": config.get('trading.base_timeframe', '5m'),
            "signal_check_interval": config.get('trading.signal_check_interval', 30),
            "trading_hours": config.get('trading.trading_hours', {"enabled": False, "start": "09:00", "end": "21:00"}),
        },
        "strategy": {
            "ema_fast": config.get('strategy.ema_fast', 50),
            "ema_slow": config.get('strategy.ema_slow', 200),
            "rsi_period": config.get('strategy.rsi_period', 14),
            "rsi_min": config.get('strategy.rsi_min', 40),
            "rsi_max": config.get('strategy.rsi_max', 60),
            "take_profit_percent": config.get('strategy.take_profit_percent', 2.0),
            "stop_loss_percent": config.get('strategy.stop_loss_percent', 1.5),
            "use_bollinger": config.get('strategy.use_bollinger', True),
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
async def get_logs(level: Optional[str] = None, limit: int = 100, offset: int = 0):
    db = SessionLocal()
    try:
        query = db.query(Log)
        if level:
            query = query.filter(Log.level == level.upper())
        logs = query.order_by(Log.timestamp.desc()).offset(offset).limit(limit).all()
        total = query.count()

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
                "message": l.message,
                "timestamp": convert_to_kyiv(l.timestamp)
            } for l in logs],
            "total": total, "limit": limit, "offset": offset
        }
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
            pair="BTCUSDT",
            side=OrderSide.BUY,
            entry_price=entry_price,
            exit_price=exit_price,
            quantity=0.001,
            pnl=(exit_price - entry_price) * 0.001,
            pnl_percent=((exit_price - entry_price) / entry_price) * 100,
            is_paper=1,
            status=OrderStatus.CLOSED,
            opened_at=now - timedelta(days=random.randint(1, 5)),
            closed_at=now - timedelta(hours=random.randint(1, 48)),
            take_profit=entry_price * 1.02,
            stop_loss=entry_price * 0.98
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
            "total_trades": len(trades),
            "win_trades": len(wins),
            "loss_trades": len(losses),
            "win_rate": round(win_rate, 2),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "profit_factor": round(profit_factor, 2),
            "max_drawdown": round(max_drawdown, 2),
            "sharpe_ratio": round(sharpe_ratio, 2),
            "expectancy": round((win_rate/100 * avg_win - (1-win_rate/100) * avg_loss), 2),
            "kelly_criterion": round(kelly * 100, 2)
        }
    except Exception as e:
        logger.error(f"Помилка розрахунку статистики: {e}")
        return {"error": str(e)}
    finally:
        db.close()


# ============ WEB PUSH ============

@app.post("/api/push/subscribe")
async def push_subscribe(subscription: dict):
    push_subscriptions.append(subscription)
    return {"status": "success"}


# ============ ГОЛОВНА СТОРІНКА ============

@app.get("/", response_class=HTMLResponse)
async def root():
    return HTMLResponse(content='''<!DOCTYPE html>
<html lang="uk">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
    <title>AutoTrading Bot v3.0</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap" rel="stylesheet">
    <script src="https://cdn.plot.ly/plotly-3.0.1.min.js"></script>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: 'Inter', sans-serif; background: linear-gradient(135deg, #0a0a1a 0%, #0f0f2a 100%); color: #fff; min-height: 100vh; }
        .nav { background: rgba(15,20,40,0.95); backdrop-filter: blur(10px); border-bottom: 1px solid rgba(102,126,234,0.3); position: sticky; top: 0; z-index: 100; padding: 12px 20px; }
        .nav-content { max-width: 1400px; margin: 0 auto; display: flex; gap: 8px; overflow-x: auto; }
        .nav-btn { padding: 10px 20px; background: transparent; border: none; color: #888; font-weight: 500; cursor: pointer; border-radius: 40px; transition: all 0.2s; white-space: nowrap; }
        .nav-btn:hover { background: rgba(102,126,234,0.2); color: #fff; }
        .nav-btn.active { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; }
        .container { max-width: 1400px; margin: 0 auto; padding: 20px; }
        .tab { display: none; animation: fadeIn 0.3s ease-out; }
        .tab.active { display: block; }
        @keyframes fadeIn { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: translateY(0); } }
        .card { background: rgba(26,26,46,0.8); backdrop-filter: blur(10px); border-radius: 20px; border: 1px solid rgba(102,126,234,0.2); margin-bottom: 20px; overflow: hidden; }
        .card-header { padding: 16px 20px; background: rgba(15,20,40,0.6); border-bottom: 1px solid rgba(102,126,234,0.2); display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px; }
        .card-header h3 { font-size: 18px; font-weight: 600; }
        .stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 16px; margin-bottom: 20px; }
        .stat-card { background: rgba(26,26,46,0.8); border-radius: 20px; padding: 20px; border: 1px solid rgba(102,126,234,0.3); transition: transform 0.2s; }
        .stat-card:hover { transform: translateY(-2px); border-color: #667eea; }
        .stat-value { font-size: 32px; font-weight: 700; background: linear-gradient(135deg, #fff 0%, #667eea 100%); -webkit-background-clip: text; background-clip: text; color: transparent; }
        .stat-value.positive { background: linear-gradient(135deg, #00ff88 0%, #00cc6a 100%); -webkit-background-clip: text; background-clip: text; }
        .stat-value.negative { background: linear-gradient(135deg, #ff4757 0%, #ff6b81 100%); -webkit-background-clip: text; background-clip: text; }
        .stat-label { color: #888; font-size: 13px; margin-top: 8px; }
        .next-forecast { background: linear-gradient(135deg, rgba(102,126,234,0.2), rgba(118,75,162,0.2)); border-radius: 15px; padding: 15px; margin-bottom: 20px; text-align: center; }
        .timer-big { font-size: 24px; font-weight: 700; color: #00d4ff; font-family: monospace; }
        .btn { padding: 10px 20px; border-radius: 40px; font-weight: 600; font-size: 13px; cursor: pointer; border: none; font-family: inherit; transition: all 0.2s; display: inline-flex; align-items: center; gap: 6px; }
        .btn-primary { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; }
        .btn-primary:hover { transform: scale(1.02); box-shadow: 0 5px 15px rgba(102,126,234,0.4); }
        .btn-danger { background: linear-gradient(135deg, #ff4757 0%, #c0392b 100%); color: white; }
        .btn-outline { background: transparent; border: 1px solid #667eea; color: #667eea; }
        .btn-sm { padding: 6px 12px; font-size: 12px; }
        .btn-success { background: linear-gradient(135deg, #00ff88 0%, #00cc6a 100%); color: #0a0a1a; }
        .table-wrapper { overflow-x: auto; }
        table { width: 100%; border-collapse: collapse; }
        th, td { padding: 12px; text-align: left; border-bottom: 1px solid rgba(102,126,234,0.1); }
        th { background: rgba(15,20,40,0.6); color: #667eea; font-weight: 600; font-size: 13px; }
        td { font-size: 13px; }
        .badge { display: inline-block; padding: 4px 10px; border-radius: 20px; font-size: 11px; font-weight: 600; }
        .badge-buy, .badge-long { background: linear-gradient(135deg, #00ff88 0%, #00cc6a 100%); color: #0a0a1a; }
        .badge-sell, .badge-short { background: linear-gradient(135deg, #ff4757 0%, #c0392b 100%); color: white; }
        .badge-active { background: #00ff88; color: #0a0a1a; }
        .badge-expired { background: #666; color: white; }
        .timer { font-family: monospace; font-size: 14px; font-weight: 700; color: #00d4ff; }
        .form-group { margin-bottom: 15px; }
        .form-group label { display: block; margin-bottom: 5px; color: #888; font-size: 13px; }
        .form-group input, .form-group select { width: 100%; padding: 10px; background: rgba(15,20,40,0.8); border: 1px solid rgba(102,126,234,0.3); border-radius: 10px; color: white; font-family: inherit; }
        .chart-container { padding: 20px; min-height: 450px; }
        .log-info { color: #00d4ff; }
        .log-warning { color: #ffa502; }
        .log-error { color: #ff4757; }
        .positive { color: #00ff88; font-weight: 600; }
        .negative { color: #ff4757; font-weight: 600; }
        .server-time { font-size: 12px; color: #666; text-align: right; margin-top: 10px; }
        .metric-card { background: rgba(26,26,46,0.5); border-radius: 15px; padding: 15px; text-align: center; }
        .metric-value { font-size: 24px; font-weight: 700; }
        .metric-label { font-size: 11px; color: #888; margin-top: 5px; }
        @media (max-width: 768px) { .container { padding: 12px; } .stat-value { font-size: 24px; } th, td { padding: 8px; font-size: 11px; } }
        @media (max-width: 480px) { .stats-grid { grid-template-columns: repeat(2, 1fr); } }
        .notification-permission { position: fixed; bottom: 20px; right: 20px; background: #667eea; padding: 10px 15px; border-radius: 40px; cursor: pointer; z-index: 1000; }
    </style>
</head>
<body>
    <div class="nav">
        <div class="nav-content">
            <button class="nav-btn active" data-tab="dashboard">📊 Дашборд</button>
            <button class="nav-btn" data-tab="forecasts">🎯 Прогнози</button>
            <button class="nav-btn" data-tab="forecasts_history">📜 Історія прогнозів</button>
            <button class="nav-btn" data-tab="analysis">📊 Аналіз</button>
            <button class="nav-btn" data-tab="charts">📈 Графіки</button>
            <button class="nav-btn" data-tab="advanced">📐 Покращена аналітика</button>
            <button class="nav-btn" data-tab="settings">⚙️ Налаштування</button>
            <button class="nav-btn" data-tab="logs">📜 Логи</button>
            <button class="nav-btn" data-tab="monitor">📊 Моніторинг</button>
        </div>
    </div>

    <div class="container">
        <!-- ДАШБОРД -->
        <div id="tab-dashboard" class="tab active">
    <div class="next-forecast">
        <div style="font-size:14px; color:#888;">⏰ Наступний прогноз через</div>
        <div class="timer-big" id="nextForecastTimer">--:--:--</div>
    </div>
    <div class="stats-grid">
        <div class="stat-card"><div class="stat-value" id="balance">$0</div><div class="stat-label">Баланс (USDT)</div></div>
        <div class="stat-card"><div class="stat-value" id="openTrades">0</div><div class="stat-label">Активні позиції</div></div>
        <div class="stat-card"><div class="stat-value" id="totalPnL">$0</div><div class="stat-label">Загальний PnL</div></div>
        <div class="stat-card"><div class="stat-value" id="winRate">0%</div><div class="stat-label">Відсоток успіху</div></div>
        <div class="stat-card"><div class="stat-value" id="dailyPnL">$0</div><div class="stat-label">Сьогодні</div></div>
        <div class="stat-card"><div class="stat-value" id="totalTrades">0</div><div class="stat-label">Всього угод</div></div>
    </div>
    <div class="stats-grid">
        <div class="stat-card"><div class="stat-value" id="profitFactor">0</div><div class="stat-label">Profit Factor</div></div>
        <div class="stat-card"><div class="stat-value" id="maxDrawdown">0</div><div class="stat-label">Max Drawdown ($)</div></div>
        <div class="stat-card"><div class="stat-value" id="avgPnL">$0</div><div class="stat-label">Середній PnL</div></div>
        <div class="stat-card"><div class="stat-value" id="kelly">0%</div><div class="stat-label">Kelly Criterion</div></div>
    </div>
    
    <!-- Відкриті позиції -->
    <div class="card">
        <div class="card-header"><h3>📈 Відкриті позиції</h3></div>
        <div class="table-wrapper">
            <table style="width:100%; border-collapse: collapse;">
                <thead>
                    <tr style="background: rgba(15,20,40,0.6);">
                        <th style="padding: 12px;">Пара</th>
                        <th style="padding: 12px;">Сторона</th>
                        <th style="padding: 12px;">Вхід</th>
                        <th style="padding: 12px;">Кількість</th>
                        <th style="padding: 12px;">TP</th>
                        <th style="padding: 12px;">SL</th>
                        <th style="padding: 12px;">Поточний PnL</th>
                        <th style="padding: 12px;">Дії</th>
                    </tr>
                </thead>
                <tbody id="openTradesBody">
                    <tr><td colspan="6" style="padding: 12px; text-align: center;">Завантаження...</td></tr>
                </tbody>
            </table>
        </div>
    </div>
    
    <!-- Історія угод -->
    <div class="card">
        <div class="card-header"><h3>📜 Історія угод</h3></div>
        <div class="table-wrapper">
            <table style="width:100%; border-collapse: collapse;">
                <thead>
                    <tr style="background: rgba(15,20,40,0.6);">
                        <th style="padding: 12px; text-align: left; color: #667eea;">Час</th>
                        <th style="padding: 12px; text-align: left; color: #667eea;">Пара</th>
                        <th style="padding: 12px; text-align: left; color: #667eea;">Сторона</th>
                        <th style="padding: 12px; text-align: left; color: #667eea;">Вхід</th>
                        <th style="padding: 12px; text-align: left; color: #667eea;">Вихід</th>
                        <th style="padding: 12px; text-align: left; color: #667eea;">PnL</th>
                    </tr>
                </thead>
                <tbody id="tradesBody">
                    <tr><td colspan="6" style="padding: 12px; text-align: center;">Завантаження...</td></tr>
                </tbody>
            </table>
        </div>
    </div>
    <div class="server-time" id="serverTime"></div>
</div>

        <!-- ПРОГНОЗИ -->
        <div id="tab-forecasts" class="tab">
    <div class="card">
        <div class="card-header">
            <h3>🎯 Прогнози</h3>
            <span style="font-size:12px; color:#888;">🤖 Прогнози створюються автоматично на основі торгових сигналів</span>
        </div>
        <div style="padding:15px; background:rgba(0,255,136,0.1); margin:15px; border-radius:10px;">
            📊 Прогнози генеруються автоматично при виявленні торгових сигналів.<br>
            Кожен прогноз діє 4 години.
        </div>
    </div>
    
    <div class="card">
        <div class="card-header">
            <h3>⏰ Активні прогнози</h3>
            <button class="btn btn-outline btn-sm" onclick="clearAllForecasts()" style="margin:5px;">🗑️ Очистити всі</button>
        </div>
        <div class="table-wrapper">
            <table style="width:100%; border-collapse: collapse;">
                <thead>
                    <tr style="background: rgba(15,20,40,0.6);">
                        <th style="padding: 12px; text-align: left; color: #667eea;">Пара</th>
                        <th style="padding: 12px; text-align: left; color: #667eea;">Тип</th>
                        <th style="padding: 12px; text-align: left; color: #667eea;">Вхід</th>
                        <th style="padding: 12px; text-align: left; color: #667eea;">Ціль</th>
                        <th style="padding: 12px; text-align: left; color: #667eea;">Поточна</th>
                        <th style="padding: 12px; text-align: left; color: #667eea;">Прибуток</th>
                        <th style="padding: 12px; text-align: left; color: #667eea;">Впевн.</th>
                        <th style="padding: 12px; text-align: left; color: #667eea;">Час</th>
                        <th style="padding: 12px; text-align: left; color: #667eea;">Дії</th>
                    </tr>
                </thead>
                <tbody id="forecastsBody">
                    <tr><td colspan="9" style="padding: 12px; text-align: center;">Завантаження...</td></tr>
                </tbody>
            </table>
        </div>
    </div>
</div>
        
        <!-- Модальне вікно для графіку прогнозу -->
        <div id="forecastModal" style="display:none; position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,0.9); z-index:2000; overflow:auto;">
            <div style="position:relative; max-width:1200px; margin:20px auto; background:#1a1a2e; border-radius:20px; padding:20px;">
                <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:15px;">
                    <h3 id="forecastModalTitle">Прогноз</h3>
                    <button onclick="closeForecastModal()" style="background:#ff4757; border:none; color:white; font-size:20px; width:40px; height:40px; border-radius:50%; cursor:pointer;">✕</button>
                </div>
                <div id="forecastModalChart" style="height:550px;"></div>
            </div>
        </div>
        
        <!-- Модальне вікно для графіку угоди -->
        <div id="tradeModal" style="display:none; position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,0.95); z-index:2000; overflow:auto;">
            <div style="position:relative; max-width:1400px; margin:20px auto; background:#1a1a2e; border-radius:20px; padding:20px;">
                <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:15px; flex-wrap:wrap; gap:10px;">
                    <h3 id="tradeModalTitle">Угода / Прогноз</h3>
                    <div style="display:flex; gap:10px; align-items:center;">
                        <select id="tradeChartTimeframe" onchange="refreshTradeChart()" style="background:#0f0f2a; border:1px solid #667eea; border-radius:10px; padding:8px; color:white;">
                            <option value="5m">5 хвилин</option>
                            <option value="15m">15 хвилин</option>
                            <option value="30m">30 хвилин</option>
                            <option value="1h" selected>1 година</option>
                            <option value="4h">4 години</option>
                            <option value="1d">1 день</option>
                        </select>
                        <button class="btn btn-outline btn-sm" onclick="closeTradeModal()">✕ Закрити</button>
                    </div>
                </div>
                <div id="tradeModalChart" style="height:600px;"></div>
            </div>
        </div>

        <style>
            #forecastModal {
                animation: fadeIn 0.2s ease-out;
            }
            @keyframes fadeIn {
                from { opacity: 0; }
                to { opacity: 1; }
            }
        </style>

        <!-- ІСТОРІЯ ПРОГНОЗІВ -->
        <div id="tab-forecasts_history" class="tab">
            <div class="card">
                <div class="card-header">
                    <h3>📜 Історія всіх прогнозів</h3>
                    <div style="display:flex;gap:10px;">
                        <button class="btn btn-outline btn-sm" onclick="loadForecastsHistory()">🔄 Оновити</button>
                    </div>
                </div>
                <div class="table-wrapper">
                    <table style="width:100%; border-collapse: collapse;">
                        <thead>
                            <tr style="background: rgba(15,20,40,0.6);">
                                <th style="padding: 12px; text-align: left; color: #667eea;">Час створення</th>
                                <th style="padding: 12px; text-align: left; color: #667eea;">Пара</th>
                                <th style="padding: 12px; text-align: left; color: #667eea;">Тип</th>
                                <th style="padding: 12px; text-align: left; color: #667eea;">Вхід</th>
                                <th style="padding: 12px; text-align: left; color: #667eea;">Ціль</th>
                                <th style="padding: 12px; text-align: left; color: #667eea;">Поточна</th>
                                <th style="padding: 12px; text-align: left; color: #667eea;">Впевн.</th>
                                <th style="padding: 12px; text-align: left; color: #667eea;">Результат</th>
                                <th style="padding: 12px; text-align: left; color: #667eea;">Дії</th>
                            </tr>
                        </thead>
                        <tbody id="forecastsHistoryBody">
                            <tr><td colspan="9" style="padding: 12px; text-align: center;">Завантаження...</td></tr>
                        </tbody>
                    </table>
                </div>
                <div style="padding:15px;text-align:center;">
                    <button class="btn btn-outline btn-sm" id="loadMoreHistoryBtn" onclick="loadMoreHistory()" style="display:none;">📥 Завантажити ще</button>
                </div>
            </div>
        </div>

        <!-- АНАЛІЗ -->
        <div id="tab-analysis" class="tab">
            <div class="card"><div class="card-header"><h3>📊 Ринковий аналіз</h3><div style="display:flex;gap:10px;"><select id="analysisPair" style="background:#1a1a2e;border:1px solid #667eea;border-radius:10px;padding:8px;color:white;"><option value="BTCUSDT">BTCUSDT</option><option value="ETHUSDT">ETHUSDT</option><option value="SOLUSDT">SOLUSDT</option></select><select id="analysisTimeframe" style="background:#1a1a2e;border:1px solid #667eea;border-radius:10px;padding:8px;color:white;"><option value="15m">15 хвилин<option value="1h">1 година</option><option value="4h">4 години</option><option value="1d">1 день</option></select><button class="btn btn-primary btn-sm" onclick="loadAnalysis()">Аналіз</button></div></div><div id="analysisResult" style="padding:20px;"><div style="text-align:center;color:#888;">Виберіть пару та натисніть "Аналіз"</div></div></div>
        </div>

        <!-- ГРАФІКИ -->
        <div id="tab-charts" class="tab">
            
            <div class="card"><div class="card-header"><h3>📊 Графік</h3><div style="display:flex;gap:10px;"><select id="chartPair" style="background:#1a1a2e;border:1px solid #667eea;border-radius:10px;padding:8px;color:white;"><option value="BTCUSDT">BTCUSDT</option><option value="ETHUSDT">ETHUSDT</option><option value="SOLUSDT">SOLUSDT</option></select><select id="chartTimeframe" style="background:#1a1a2e;border:1px solid #667eea;border-radius:10px;padding:8px;color:white;"><option value="15m">15 хвилин<option value="1h">1 година</option><option value="4h">4 години</option><option value="1d">1 день</option></select><button class="btn btn-primary btn-sm" onclick="loadChart()">Оновити</button><button class="btn btn-outline btn-sm" onclick="resetChartView()">⟳ Скинути масштаб</button></div></div><div class="chart-container" id="priceChart"></div></div>
            <div class="card"><div class="card-header"><h3>📈 PnL динаміка</h3></div><div class="chart-container" id="pnlChart"></div></div>
            
        </div>

        <!-- ПОКРАЩЕНА АНАЛІТИКА -->
        <div id="tab-advanced" class="tab">
            <div class="card"><div class="card-header"><h3>📐 Розширена статистика</h3><button class="btn btn-outline btn-sm" onclick="loadAdvancedStats()">🔄 Оновити</button></div>
                <div style="padding:20px;">
                    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:15px;" id="advancedStats">
                        <div class="metric-card"><div class="metric-value" id="advWinRate">--</div><div class="metric-label">Win Rate</div></div>
                        <div class="metric-card"><div class="metric-value" id="advProfitFactor">--</div><div class="metric-label">Profit Factor</div></div>
                        <div class="metric-card"><div class="metric-value" id="advMaxDrawdown">--</div><div class="metric-label">Max Drawdown ($)</div></div>
                        <div class="metric-card"><div class="metric-value" id="advSharpe">--</div><div class="metric-label">Sharpe Ratio</div></div>
                        <div class="metric-card"><div class="metric-value" id="advExpectancy">--</div><div class="metric-label">Expectancy ($)</div></div>
                        <div class="metric-card"><div class="metric-value" id="advKelly">--</div><div class="metric-label">Kelly Criterion</div></div>
                    </div>
                </div>
            </div>
        </div>

        <!-- НАЛАШТУВАННЯ -->
        <div id="tab-settings" class="tab">
            <div class="card"><div class="card-header"><h3>⚙️ Налаштування бота</h3></div><div style="padding:20px;">
                <div style="background:rgba(102,126,234,0.1); border-radius:10px; padding:15px; margin-bottom:20px;">
                    <small>📌 Наведіть курсор на параметр для отримання підказки</small>
                </div>
                
                <h4 style="color:#667eea; margin-bottom:15px;">📊 Торгівля</h4>
                <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:15px;margin-bottom:25px;">
                    <div class="form-group" title="Основний таймфрейм для аналізу (5m, 15m, 1h)">
                        <label>⏱️ Базовий таймфрейм</label>
                        <select id="baseTimeframe">
                            <option value="1m">1 хвилина</option>
                            <option value="5m">5 хвилин</option>
                            <option value="15m">15 хвилин</option>
                            <option value="1h">1 година</option>
                            <option value="4h">4 години</option>
                            <option value="1d">1 день</option>
                        </select>
                        <small style="color:#666;">Період для розрахунку індикаторів</small>
                    </div>
                    
                    <div class="form-group" title="Автоматичне створення угод на основі прогнозів (для тестування)">
                        <label>🚀 Угоди за прогнозами</label>
                        <select id="createTradesFromForecasts">
                            <option value="true">✅ Увімкнено</option>
                            <option value="false">❌ Вимкнено</option>
                        </select>
                        <small style="color:#666;">При увімкненні, кожен прогноз створює реальну угоду</small>
                    </div>
                    
                    <div class="form-group" title="Відсоток балансу для прогнозу">
                        <label>💰 Розмір прогнозу (% балансу)</label>
                        <input type="range" id="forecastPositionPercent" min="1" max="100" step="1" value="25" style="width:100%;">
                        <span id="forecastPositionPercentValue" style="display:inline-block; margin-left:10px;">25%</span>
                        <small style="color:#666;">Скільки від балансу використовувати для прогнозу</small>
                    </div>
                    
                    <div class="form-group" title="Як часто перевіряти нові сигнали (секунди)">
                        <label>🔄 Інтервал перевірки (сек)</label>
                        <input type="number" id="signalCheckInterval" step="5" value="30">
                        <small style="color:#666;">Рекомендовано 30-60 секунд</small>
                    </div>
                    
                    <div class="form-group" title="Обмежити торгівлю тільки в певні години">
                        <label>⏰ Години торгівлі</label>
                        <select id="tradingHoursEnabled">
                            <option value="false">🔴 Вимкнено (торгівля 24/7)</option>
                            <option value="true">🟢 Увімкнено</option>
                        </select>
                        <small style="color:#666;">Актуально для високоволатильних ринків</small>
                    </div>
                </div>
                
                <h4 style="color:#667eea; margin-bottom:15px;">📈 Стратегія (EMA/RSI/MACD)</h4>
                <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:15px;margin-bottom:25px;">
                    <div class="form-group" title="Швидка ковзна середня (реагує на зміни ціни)">
                        <label>📈 EMA Fast</label>
                        <input type="number" id="emaFast" value="50">
                        <small style="color:#666;">50 - стандарт, менше = чутливіший</small>
                    </div>
                    <div class="form-group" title="Повільна ковзна середня (показує загальний тренд)">
                        <label>📉 EMA Slow</label>
                        <input type="number" id="emaSlow" value="200">
                        <small style="color:#666;">200 - довгостроковий тренд</small>
                    </div>
                    <div class="form-group" title="Період розрахунку RSI">
                        <label>📊 RSI період</label>
                        <input type="number" id="rsiPeriod" value="14">
                        <small style="color:#666;">14 - стандартний період</small>
                    </div>
                    <div class="form-group" title="Нижня межа RSI (сигнал до купівлі)">
                        <label>📊 RSI min</label>
                        <input type="number" id="rsiMin" value="40" step="1">
                        <small style="color:#666;">Нижче 30 = перепродано</small>
                    </div>
                    <div class="form-group" title="Верхня межа RSI (сигнал до продажу)">
                        <label>📊 RSI max</label>
                        <input type="number" id="rsiMax" value="60" step="1">
                        <small style="color:#666;">Вище 70 = перекуплено</small>
                    </div>
                    <div class="form-group" title="Відсоток прибутку для автоматичного закриття">
                        <label>🎯 Take Profit %</label>
                        <input type="number" id="tpPercent" step="0.5" value="2.0">
                        <small style="color:#666;">Рекомендовано 2-4%</small>
                    </div>
                    <div class="form-group" title="Відсоток збитку для автоматичного закриття">
                        <label>🛑 Stop Loss %</label>
                        <input type="number" id="slPercent" step="0.5" value="1.5">
                        <small style="color:#666;">Рекомендовано 1-2%</small>
                    </div>
                    <div class="form-group" title="Використовувати Bollinger Bands для підтвердження">
                        <label>📊 Bollinger Bands</label>
                        <input type="checkbox" id="useBollinger">
                        <small style="color:#666;">Додатковий індикатор волатильності</small>
                    </div>
                </div>
                
                <h4 style="color:#667eea; margin-bottom:15px;">🛡️ Ризик-менеджмент</h4>
                <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:15px;margin-bottom:25px;">
                    <div class="form-group" title="Максимальний відсоток балансу на одну угоду">
                        <label>⚠️ Ризик на угоду (%)</label>
                        <input type="number" id="riskPerTrade" step="0.5" value="2.0">
                        <small style="color:#666;">2-3% для консервативної торгівлі</small>
                    </div>
                    <div class="form-group" title="Максимальна кількість одночасних угод">
                        <label>📊 Макс. відкритих угод</label>
                        <input type="number" id="maxOpenTrades" value="5">
                        <small style="color:#666;">Не більше 5 для кращого контролю</small>
                    </div>
                    <div class="form-group" title="При досягненні - автоматична зупинка торгівлі">
                        <label>📉 Макс. денний збиток (%)</label>
                        <input type="number" id="maxDailyLoss" step="0.5" value="5.0">
                        <small style="color:#666;">Захист від великих втрат за день</small>
                    </div>
                    <div class="form-group" title="Автоматичний розрахунок оптимального розміру позиції">
                        <label>🧠 Kelly Criterion</label>
                        <input type="checkbox" id="useKelly">
                        <small style="color:#666;">Математично оптимальний ризик</small>
                    </div>
                </div>
                
                <div style="margin-top:20px; background:rgba(0,255,136,0.1); border-radius:10px; padding:15px;">
                    <h4 style="color:#00ff88; margin-bottom:10px;">💡 Поради</h4>
                    <ul style="margin-left:20px; color:#ccc; font-size:13px;">
                        <li>Почніть з <strong>Paper Trading</strong> перед реальною торгівлею</li>
                        <li>Risk per trade: 1-2% для консервативної стратегії, 3-5% для агресивної</li>
                        <li>Kelly Criterion допомагає уникнути переризикування</li>
                        <li>Max Drawdown показує максимальну просадку за період</li>
                        <li>Profit Factor > 1.5 означає хорошу стратегію</li>
                        <li>Sharpe Ratio > 1 означає хороше співвідношення ризик/прибуток</li>
                    </ul>
                </div>
                
                <div style="display:flex;gap:10px;justify-content:flex-end; margin-top:20px;">
                    <button class="btn btn-outline" onclick="loadSettings()">🔄 Скинути</button>
                    <button class="btn btn-primary" onclick="saveSettings()">💾 Зберегти</button>
                </div>
            </div></div>
        </div>

        <!-- ЛОГИ -->
        <div id="tab-logs" class="tab">
            <div class="card"><div class="card-header"><h3>📜 Логи</h3><div style="display:flex;gap:10px;"><select id="logLevelFilter" style="background:#1a1a2e;border:1px solid #667eea;border-radius:10px;padding:6px;color:white;"><option value="">Всі</option><option value="INFO">INFO</option><option value="WARNING">WARNING</option><option value="ERROR">ERROR</option></select><button class="btn btn-outline btn-sm" onclick="loadLogs(true)">Оновити</button><button class="btn btn-danger btn-sm" onclick="clearLogs()">Очистити</button></div></div>
                <div class="table-wrapper" style="max-height:500px;overflow-y:auto;"><table style="min-width:600px;"><thead><tr><th>Час</th><th>Рівень</th><th>Модуль</th><th>Повідомлення</th></tr></thead><tbody id="logsBody">...</tbody></table></div>
                <div style="padding:15px;text-align:center;"><button class="btn btn-outline btn-sm" id="loadMoreBtn" onclick="loadMoreLogs()" style="display:none;">📥 Завантажити ще</button></div>
            </div>
        </div>
    </div>
    
    <!-- МОНІТОРИНГ -->
    <div id="tab-monitor" class="tab">
        <div class="card">
            <div class="card-header">
                <h3>📊 Стан бота</h3>
                <button class="btn btn-outline btn-sm" onclick="loadHealthCheck()">🔄 Оновити</button>
            </div>
            <div id="healthStatus" style="padding: 20px;">
                <div style="text-align:center; color:#888;">Завантаження...</div>
            </div>
        </div>
    </div>

    <div class="notification-permission" onclick="enableNotifications()">🔔 Увімкнути сповіщення</div>

    <script>
    let ws = null;
    let currentLogOffset = 0;
    let hasMoreLogs = true;
    let nextForecastTimer = null;
    let notificationsEnabled = false;
    
    // Модальне вікно угод
    let currentTradeId = null;
    let currentTradeType = null;
    let currentTradePair = null;

    // ============ WEB SOCKET ============
    function connectWebSocket() {
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        ws = new WebSocket(`${protocol}//${window.location.host}/ws`);
        ws.onmessage = function(event) {
            const data = JSON.parse(event.data);
            if (data.type === 'status') {
                updateDashboard(data);
                if (data.next_forecast) updateNextForecastTimer(data.next_forecast);
            }
            if (data.type === 'notification') {
                showNotification(data.title, data.body);
            }
            if (data.type === 'new_forecast') loadForecasts();
        };
        ws.onclose = function() { setTimeout(connectWebSocket, 3000); };
    }

    // ============ ДОПОМІЖНІ ФУНКЦІЇ ============
    function getCurrentPrice(pair = 'BTCUSDT') {
        return fetch(`/api/current_price/${pair}`)
            .then(res => res.json())
            .then(data => data.price || 40000)
            .catch(e => 40000);
    }

    function updateDashboard(data) {
        const balance = document.getElementById('balance');
        if (balance) balance.innerHTML = `$${data.balance.toFixed(2)}`;
        const openTrades = document.getElementById('openTrades');
        if (openTrades) openTrades.innerHTML = data.open_trades;
        const totalPnL = document.getElementById('totalPnL');
        if (totalPnL) totalPnL.innerHTML = data.total_pnl >= 0 ? `<span class="positive">+$${data.total_pnl.toFixed(2)}</span>` : `<span class="negative">-$${Math.abs(data.total_pnl).toFixed(2)}</span>`;
        const winRate = document.getElementById('winRate');
        if (winRate) winRate.innerHTML = `${data.win_rate.toFixed(1)}%`;
        const dailyPnL = document.getElementById('dailyPnL');
        if (dailyPnL) dailyPnL.innerHTML = data.daily_pnl >= 0 ? `<span class="positive">+$${data.daily_pnl.toFixed(2)}</span>` : `<span class="negative">-$${Math.abs(data.daily_pnl).toFixed(2)}</span>`;
        const totalTrades = document.getElementById('totalTrades');
        if (totalTrades) totalTrades.innerHTML = data.total_trades;
        const profitFactor = document.getElementById('profitFactor');
        if (profitFactor) profitFactor.innerHTML = data.profit_factor?.toFixed(2) || '0';
        const maxDrawdown = document.getElementById('maxDrawdown');
        if (maxDrawdown) maxDrawdown.innerHTML = `$${data.max_drawdown?.toFixed(2) || '0'}`;
        const avgPnL = document.getElementById('avgPnL');
        if (avgPnL) avgPnL.innerHTML = data.avg_pnl >= 0 ? `+$${data.avg_pnl?.toFixed(2)}` : `-$${Math.abs(data.avg_pnl || 0).toFixed(2)}`;
        
        if (data.next_forecast) updateNextForecastTimer(data.next_forecast);
        if (data.server_time && document.getElementById('serverTime')) {
            document.getElementById('serverTime').innerHTML = `🕐 Час сервера: ${new Date(data.server_time).toLocaleString('uk-UA')}`;
        }
    }

    function updateNextForecastTimer(targetTime) {
        if (nextForecastTimer) clearInterval(nextForecastTimer);
        function update() {
            const now = new Date();
            const target = new Date(targetTime);
            const diff = target - now;
            if (diff <= 0) {
                document.getElementById('nextForecastTimer').innerHTML = '00:00:00';
                clearInterval(nextForecastTimer);
                fetch('/api/status').then(r => r.json()).then(data => { if (data.next_forecast) updateNextForecastTimer(data.next_forecast); });
                return;
            }
            const hours = Math.floor(diff / 3600000);
            const minutes = Math.floor((diff % 3600000) / 60000);
            const seconds = Math.floor((diff % 60000) / 1000);
            document.getElementById('nextForecastTimer').innerHTML = `${hours.toString().padStart(2,'0')}:${minutes.toString().padStart(2,'0')}:${seconds.toString().padStart(2,'0')}`;
        }
        update();
        nextForecastTimer = setInterval(update, 1000);
    }

    // ============ МОДАЛЬНІ ВІКНА ============
    function openTradeModal() {
        document.getElementById('tradeModal').style.display = 'block';
        document.body.style.overflow = 'hidden';
    }
    
    function closeTradeModal() {
        document.getElementById('tradeModal').style.display = 'none';
        document.body.style.overflow = 'auto';
        currentTradeId = null;
        currentTradeType = null;
    }
    
    function openForecastModal() {
        document.getElementById('forecastModal').style.display = 'block';
        document.body.style.overflow = 'hidden';
    }
    
    function closeForecastModal() {
        document.getElementById('forecastModal').style.display = 'none';
        document.body.style.overflow = 'auto';
    }
    
    document.addEventListener('keydown', function(e) {
        if (e.key === 'Escape') {
            closeTradeModal();
            closeForecastModal();
        }
    });

    // ============ ГРАФІКИ ============
    async function showTradeOnChart(tradeId, pair, entryPrice, side, exitPrice) {
        console.log('Показ графіку для угоди:', tradeId);
        openTradeModal();
        document.getElementById('tradeModalTitle').innerHTML = `📊 Угода ${pair} | ${side === 'BUY' ? 'LONG' : 'SHORT'} | Вхід: $${entryPrice}`;
        document.getElementById('tradeModalChart').innerHTML = '<div style="text-align:center; padding:50px;">🔄 Завантаження графіку...</div>';
        currentTradeId = tradeId;
        currentTradeType = 'trade';
        currentTradePair = pair;
        await loadTradeChart();
    }
    
    async function showForecastOnChart(forecastId) {
        console.log('Показ графіку для прогнозу:', forecastId);
        openTradeModal();
        document.getElementById('tradeModalTitle').innerHTML = 'Завантаження...';
        document.getElementById('tradeModalChart').innerHTML = '<div style="text-align:center; padding:50px;">🔄 Завантаження графіку...</div>';
        try {
            const forecastRes = await fetch(`/api/forecast/info/${forecastId}`);
            const forecastInfo = await forecastRes.json();
            currentTradeId = forecastId;
            currentTradeType = 'forecast';
            currentTradePair = forecastInfo.pair;
            document.getElementById('tradeModalTitle').innerHTML = `📈 Прогноз ${forecastInfo.pair} | ${forecastInfo.signal_type} | Вхід: $${forecastInfo.entry_price} | Ціль: $${forecastInfo.target_price}`;
            const res = await fetch(`/api/forecast/chart/${forecastId}?timeframe=${document.getElementById('tradeChartTimeframe').value}`);
            const data = await res.json();
            Plotly.newPlot('tradeModalChart', data.data, data.layout || {}, { responsive: true });
        } catch(e) {
            console.error('Помилка:', e);
            document.getElementById('tradeModalChart').innerHTML = '<div style="text-align:center; padding:50px; color:#ff4757;">❌ Помилка завантаження</div>';
        }
    }
    
    async function loadTradeChart() {
        if (!currentTradeId) return;
        const timeframe = document.getElementById('tradeChartTimeframe').value;
        try {
            const res = await fetch(`/api/trade/chart/${currentTradeId}?timeframe=${timeframe}`);
            if (!res.ok) throw new Error('Помилка завантаження');
            const data = await res.json();
            Plotly.newPlot('tradeModalChart', data.data, data.layout || {}, { responsive: true });
        } catch(e) {
            console.error('Помилка завантаження графіку угоди:', e);
            document.getElementById('tradeModalChart').innerHTML = '<div style="text-align:center; padding:50px; color:#ff4757;">❌ Помилка завантаження графіку</div>';
        }
    }
    
    async function loadForecastChart() {
        if (!currentTradeId) return;
        const timeframe = document.getElementById('tradeChartTimeframe').value;
        try {
            const res = await fetch(`/api/forecast/chart/${currentTradeId}?timeframe=${timeframe}`);
            if (!res.ok) throw new Error('Помилка завантаження');
            const data = await res.json();
            Plotly.newPlot('tradeModalChart', data.data, data.layout || {}, { responsive: true });
        } catch(e) {
            console.error('Помилка завантаження графіку прогнозу:', e);
            document.getElementById('tradeModalChart').innerHTML = '<div style="text-align:center; padding:50px; color:#ff4757;">❌ Помилка завантаження графіку</div>';
        }
    }
    
    async function refreshTradeChart() {
        if (!currentTradeId) return;
        document.getElementById('tradeModalChart').innerHTML = '<div style="text-align:center; padding:50px;">🔄 Завантаження графіку з новим таймфреймом...</div>';
        const timeframe = document.getElementById('tradeChartTimeframe').value;
        if (currentTradeType === 'trade') {
            try {
                const res = await fetch(`/api/trade/chart/${currentTradeId}?timeframe=${timeframe}`);
                if (!res.ok) throw new Error('Помилка завантаження');
                const data = await res.json();
                Plotly.newPlot('tradeModalChart', data.data, data.layout || {}, { responsive: true });
            } catch(e) {
                document.getElementById('tradeModalChart').innerHTML = '<div style="text-align:center; padding:50px; color:#ff4757;">❌ Помилка завантаження графіку</div>';
            }
        } else if (currentTradeType === 'forecast') {
            try {
                const res = await fetch(`/api/forecast/chart/${currentTradeId}?timeframe=${timeframe}`);
                if (!res.ok) throw new Error('Помилка завантаження');
                const data = await res.json();
                Plotly.newPlot('tradeModalChart', data.data, data.layout || {}, { responsive: true });
            } catch(e) {
                document.getElementById('tradeModalChart').innerHTML = '<div style="text-align:center; padding:50px; color:#ff4757;">❌ Помилка завантаження графіку</div>';
            }
        }
    }

    // ============ ОСНОВНІ ФУНКЦІЇ ============
    async function enableNotifications() {
        if (!('Notification' in window)) { alert('Ваш браузер не підтримує сповіщення'); return; }
        const permission = await Notification.requestPermission();
        if (permission === 'granted') {
            notificationsEnabled = true;
            document.querySelector('.notification-permission').style.display = 'none';
            showNotification('Сповіщення увімкнено', 'Ви будете отримувати сповіщення про нові сигнали');
        }
    }

    function showNotification(title, body) {
        if (notificationsEnabled && Notification.permission === 'granted') {
            new Notification(title, { 
                body,
                icon: 'data:image/svg+xml,%3Csvg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="%236667ee"%3E%3Cpath d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-2 15h-2v-2h2v2zm0-4h-2V7h2v6z"/%3E%3C/svg%3E'
            });
        }
    }

    function formatServerTime(isoString) {
        if (!isoString) return '-';
        return isoString.substring(0, 10) + ' ' + isoString.substring(11, 16);
    }

    // ============ HISTORY FORECASTS ============
    let historyOffset = 0;
    let hasMoreHistory = true;
    
    async function loadForecastsHistory(reset = true) {
        if (reset) { historyOffset = 0; hasMoreHistory = true; }
        try {
            const res = await fetch(`/api/forecasts/history?limit=50&offset=${historyOffset}`);
            const data = await res.json();
            const body = document.getElementById('forecastsHistoryBody');
            if (!body) return;
            if (!data.forecasts || data.forecasts.length === 0 && historyOffset === 0) {
                body.innerHTML = '<tr><td colspan="9" style="padding: 12px; text-align: center;">Немає прогнозів</td></tr>';
                return;
            }
            const rows = data.forecasts.map(f => {
                let resultClass = f.result_text.includes('✅') ? 'positive' : (f.result_text.includes('❌') ? 'negative' : '');
                const createdTime = f.created_at.substring(0, 10) + ' ' + f.created_at.substring(11, 16);
                return `<tr style="cursor:pointer;" onclick="showForecastOnChart(${f.id})">
                    <td style="padding: 12px; white-space: nowrap;">${createdTime}</td>
                    <td style="padding: 12px;">${f.pair}</td>
                    <td style="padding: 12px;"><span class="badge ${f.signal_type === 'LONG' ? 'badge-long' : 'badge-short'}">${f.signal_type === 'LONG' ? '📈 LONG' : '📉 SHORT'}</span></td>
                    <td style="padding: 12px;">$${f.entry_price.toFixed(0)}</td>
                    <td style="padding: 12px;">$${f.target_price.toFixed(0)}</td>
                    <td style="padding: 12px;">$${f.current_price.toFixed(0)}</td>
                    <td style="padding: 12px;">${f.confidence}%</td>
                    <td style="padding: 12px;" class="${resultClass}">${f.result_text}<br><small>$${f.current_price.toFixed(0)}</small></td>
                    <td style="padding: 12px;"><button class="btn btn-outline btn-sm" onclick="event.stopPropagation(); showForecastOnChart(${f.id})">📊 Графік</button></td>
                </tr>`;
            }).join('');
            if (reset) body.innerHTML = rows;
            else body.innerHTML += rows;
            hasMoreHistory = data.forecasts && data.forecasts.length === 50;
            document.getElementById('loadMoreHistoryBtn').style.display = hasMoreHistory ? 'inline-block' : 'none';
            historyOffset += data.forecasts?.length || 0;
        } catch(e) { console.error('Помилка завантаження історії прогнозів:', e); }
    }
    
    async function loadMoreHistory() { if (hasMoreHistory) await loadForecastsHistory(false); }

    // ============ HEALTH CHECK ============
    async function loadHealthCheck() {
        try {
            const res = await fetch('/health');
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const data = await res.json();
            const statusColor = data.status === 'healthy' ? '#00ff88' : '#ff4757';
            const statusText = data.status === 'healthy' ? '✅ ЗДОРОВИЙ' : '❌ ПРОБЛЕМИ';
            let uptimeText = 'Н/Д';
            if (data.bot && data.bot.uptime_seconds) {
                const hours = Math.floor(data.bot.uptime_seconds / 3600);
                const minutes = Math.floor((data.bot.uptime_seconds % 3600) / 60);
                const seconds = data.bot.uptime_seconds % 60;
                uptimeText = `${hours}г ${minutes}хв ${seconds}с`;
            }
            const balance = data.balance ? data.balance.paper_usdt : 0;
            const wsStatus = data.websocket ? (data.websocket.status === 'connected' ? '✅ Підключено' : '❌ Відключено') : '❓ Н/Д';
            const wsClients = data.websocket ? data.websocket.clients : 0;
            const dbStatus = data.database ? (data.database.status === 'ok' ? '✅ Працює' : '❌ Помилка') : '❓ Н/Д';
            const pythonVer = data.system ? data.system.python_version : 'Н/Д';
            const platform = data.system ? data.system.platform : 'Н/Д';
            const html = `<div style="display:grid; grid-template-columns:repeat(auto-fit, minmax(300px, 1fr)); gap:15px;">
                <div style="background:rgba(26,26,46,0.5); border-radius:15px; padding:15px;">
                    <h4 style="color:#667eea; margin-bottom:10px;">🤖 Статус бота</h4>
                    <div style="font-size:24px; font-weight:700; color:${statusColor};">${statusText}</div>
                    <div>Режим: <strong>${data.bot?.mode === 'paper' ? '📄 Paper Trading' : '💰 Real Trading'}</strong></div>
                    <div>Версія: ${data.bot?.version || '3.0.0'}</div>
                    <div>Час роботи: ${uptimeText}</div>
                </div>
                <div style="background:rgba(26,26,46,0.5); border-radius:15px; padding:15px;">
                    <h4 style="color:#667eea; margin-bottom:10px;">🔌 Підключення</h4>
                    <div>WebSocket: <span style="color:${data.websocket?.status === 'connected' ? '#00ff88' : '#ff4757'}">${wsStatus}</span></div>
                    <div>Клієнтів WebSocket: ${wsClients}</div>
                    <div>База даних: <span style="color:${data.database?.status === 'ok' ? '#00ff88' : '#ff4757'}">${dbStatus}</span></div>
                </div>
                <div style="background:rgba(26,26,46,0.5); border-radius:15px; padding:15px;">
                    <h4 style="color:#667eea; margin-bottom:10px;">💰 Баланс</h4>
                    <div style="font-size:28px; font-weight:700; color:#00d4ff;">$${balance.toFixed(2)}</div>
                    <div>Тип: Paper Trading</div>
                </div>
                <div style="background:rgba(26,26,46,0.5); border-radius:15px; padding:15px;">
                    <h4 style="color:#667eea; margin-bottom:10px;">💻 Система</h4>
                    <div>Python: ${pythonVer}</div>
                    <div>ОС: ${platform}</div>
                </div>
            </div>
            <div style="margin-top:15px; font-size:12px; color:#666; text-align:right;">
                Останнє оновлення: ${new Date(data.timestamp).toLocaleString('uk-UA')}
            </div>`;
            document.getElementById('healthStatus').innerHTML = html;
        } catch(e) {
            console.error('Помилка завантаження health check:', e);
            document.getElementById('healthStatus').innerHTML = `<div style="text-align:center; padding:50px; color:#ff4757;">❌ Помилка завантаження статусу бота: ${e.message}</div>`;
        }
    }

    // ============ DASHBOARD ============
    async function loadDashboard() {
    try {
        const res = await fetch('/api/status');
        const data = await res.json();
        updateDashboard(data);
        if (data.next_forecast) updateNextForecastTimer(data.next_forecast);

        // Відкриті позиції
        const openRes = await fetch('/api/open_trades');
        const openTrades = await openRes.json();
        const openBody = document.getElementById('openTradesBody');
        if (openBody) {
            if (openTrades && openTrades.length > 0) {
                // Отримуємо поточні ціни
                const currentPrices = {};
                for (const trade of openTrades) {
                    if (!currentPrices[trade.pair]) {
                        const priceRes = await fetch(`/api/current_price/${trade.pair}`);
                        const priceData = await priceRes.json();
                        currentPrices[trade.pair] = priceData.price;
                    }
                }
                
                openBody.innerHTML = openTrades.map(t => {
                    const currentPrice = currentPrices[t.pair] || t.entry_price;
                    let currentPnl = 0, currentPnlPercent = 0;
                    if (t.side === 'BUY') {
                        currentPnl = (currentPrice - t.entry_price) * t.quantity;
                        currentPnlPercent = ((currentPrice - t.entry_price) / t.entry_price) * 100;
                    } else {
                        currentPnl = (t.entry_price - currentPrice) * t.quantity;
                        currentPnlPercent = ((t.entry_price - currentPrice) / t.entry_price) * 100;
                    }
                    return `<tr style="cursor:pointer;" onclick="showTradeOnChart(${t.id}, '${t.pair}', ${t.entry_price}, '${t.side}', null)">
                        <td style="padding: 12px;">${t.pair}</td>
                        <td style="padding: 12px;"><span class="badge ${t.side === 'BUY' ? 'badge-buy' : 'badge-sell'}">${t.side === 'BUY' ? 'LONG' : 'SHORT'}</span></td>
                        <td style="padding: 12px;">$${t.entry_price.toFixed(0)}</td>
                        <td style="padding: 12px;">${t.quantity}</td>
                        <td style="padding: 12px;">$${t.take_profit?.toFixed(0) || '-'}</td>
                        <td style="padding: 12px;">$${t.stop_loss?.toFixed(0) || '-'}</td>
                        <td style="padding: 12px;" class="${currentPnl >= 0 ? 'positive' : 'negative'}">${currentPnl >= 0 ? '+' : ''}$${currentPnl.toFixed(2)} (${currentPnlPercent.toFixed(1)}%)</td>
                        <td style="padding: 12px;"><button class="btn btn-danger btn-sm" onclick="event.stopPropagation(); closeTrade(${t.id})">🔴 Закрити</button></td>
                    </tr>`;
                }).join('');
            } else {
                openBody.innerHTML = '<tr><td colspan="8" style="text-align: center;">Немає відкритих позицій</td></tr>';
            }
        }

        // Історія угод
        const tradesRes = await fetch('/api/trades?limit=20');
        const trades = await tradesRes.json();
        const tradesBody = document.getElementById('tradesBody');
        if (tradesBody) {
            if (trades && trades.length > 0) {
                tradesBody.innerHTML = trades.map(t => `<tr style="cursor:pointer;" onclick="showTradeOnChart(${t.id}, '${t.pair}', ${t.entry_price}, '${t.side}', ${t.exit_price || 'null'})">
                    <td style="white-space: nowrap;">${t.opened_at.replace('T', ' ').substring(0, 16)}</td>
                    <td>${t.pair}</td>
                    <td><span class="badge ${t.side === 'BUY' ? 'badge-buy' : 'badge-sell'}">${t.side === 'BUY' ? 'КУПІВЛЯ' : 'ПРОДАЖ'}</span></td>
                    <td>$${t.entry_price.toFixed(0)}</td>
                    <td>${t.exit_price ? '$' + t.exit_price.toFixed(0) : '-'}</td>
                    <td class="${t.pnl >= 0 ? 'positive' : 'negative'}">${t.pnl >= 0 ? '+' : ''}$${t.pnl.toFixed(2)} (${t.pnl_percent.toFixed(1)}%)</td>
                </td>`).join('');
            } else {
                tradesBody.innerHTML = '<tr><td colspan="6" style="text-align: center;">Ще немає угод</td></tr>';
            }
        }
    } catch(e) { console.error('Помилка завантаження дашборду:', e); }
}

            // Історія угод
            const tradesRes = await fetch('/api/trades?limit=20');
            const trades = await tradesRes.json();
            const tradesBody = document.getElementById('tradesBody');
            if (tradesBody) {
                if (trades && trades.length > 0) {
                    tradesBody.innerHTML = trades.map(t => `<tr style="cursor:pointer;" onclick="showTradeOnChart(${t.id}, '${t.pair}', ${t.entry_price}, '${t.side}', ${t.exit_price || 'null'})">
                        <td style="white-space: nowrap;">${t.opened_at.replace('T', ' ').substring(0, 16)}</td>
                        <td>${t.pair}</td>
                        <td><span class="badge ${t.side === 'BUY' ? 'badge-buy' : 'badge-sell'}">${t.side === 'BUY' ? 'КУПІВЛЯ' : 'ПРОДАЖ'}</span></td>
                        <td>$${t.entry_price.toFixed(0)}</td>
                        <td>${t.exit_price ? '$' + t.exit_price.toFixed(0) : '-'}</td>
                        <td class="${t.pnl >= 0 ? 'positive' : 'negative'}">${t.pnl >= 0 ? '+' : ''}$${t.pnl.toFixed(2)} (${t.pnl_percent.toFixed(1)}%)</td>
                    </tr>`).join('');
                } else {
                    tradesBody.innerHTML = '<td><td colspan="6" style="text-align: center;">Ще немає угод</td></tr>';
                }
            }
        } catch(e) { console.error(e); }
    }

    async function closeTrade(tradeId) {
        if (confirm(`Закрити угоду ${tradeId} за поточною ціною?`)) {
            const currentPrice = await getCurrentPrice();
            const res = await fetch(`/api/trade/close/${tradeId}?price=${currentPrice}`, { method: 'POST' });
            const data = await res.json();
            if (data.pnl !== undefined) {
                alert(`Угоду закрито! PnL: ${data.pnl >= 0 ? '+' : ''}$${data.pnl.toFixed(2)}`);
                loadDashboard();
            }
        }
    }

    // ============ FORECASTS ============
    async function loadForecasts() {
    try {
        const res = await fetch('/api/forecasts');
        const forecasts = await res.json();
        const body = document.getElementById('forecastsBody');
        if (body) {
            if (forecasts && forecasts.length > 0) {
                body.innerHTML = forecasts.map(f => {
                    let currentPnl = 0;
                    let currentPnlPercent = 0;
                    
                    if (f.signal_type === 'LONG') {
                        currentPnlPercent = ((f.current_price - f.entry_price) / f.entry_price) * 100;
                        // Використовуємо position_usdt для розрахунку USDT
                        const positionUsdt = f.position_usdt || 10;
                        currentPnl = (currentPnlPercent / 100) * positionUsdt;
                    } else {
                        currentPnlPercent = ((f.entry_price - f.current_price) / f.entry_price) * 100;
                        const positionUsdt = f.position_usdt || 10;
                        currentPnl = (currentPnlPercent / 100) * positionUsdt;
                    }
                    
                    const hours = Math.floor(f.time_remaining / 3600);
                    const minutes = Math.floor((f.time_remaining % 3600) / 60);
                    
                    return `<tr style="cursor:pointer;" onclick="showForecastOnChart(${f.id})">
                        <td style="padding: 12px;">${f.pair}</td>
                        <td style="padding: 12px;"><span class="badge ${f.signal_type === 'LONG' ? 'badge-long' : 'badge-short'}">${f.signal_type === 'LONG' ? '📈 LONG' : '📉 SHORT'}</span></td>
                        <td style="padding: 12px;">$${f.entry_price.toFixed(0)}</td>
                        <td style="padding: 12px;">$${f.target_price.toFixed(0)}</td>
                        <td style="padding: 12px;">$${f.current_price.toFixed(0)}</td>
                        <td style="padding: 12px;" class="${currentPnlPercent >= 0 ? 'positive' : 'negative'}">
                            ${currentPnlPercent >= 0 ? '+' : ''}$${currentPnl.toFixed(2)} (${currentPnlPercent.toFixed(1)}%)
                        </table>
                        <td style="padding: 12px;">${f.confidence.toFixed(1)}%</td>
                        <td style="padding: 12px;" class="timer" data-expires="${f.expires_at}">${hours}г ${minutes}хв</td>
                        <td style="padding: 12px;"><button class="btn btn-outline btn-sm" onclick="event.stopPropagation(); deleteForecast(${f.id})">🗑️</button></td>
                    </tr>`;
                }).join('');
                startTimers();
            } else {
                body.innerHTML = '<tr><td colspan="9" style="text-align: center;">Немає активних прогнозів</td></tr>';
            }
        }
    } catch(e) { console.error(e); }
}

    function startTimers() {
        document.querySelectorAll('.timer[data-expires]').forEach(el => {
            if (el.hasAttribute('data-timer-set')) return;
            el.setAttribute('data-timer-set', 'true');
            const expires = new Date(el.dataset.expires);
            const interval = setInterval(() => {
                const remaining = (expires - new Date()) / 1000;
                if (remaining <= 0) { clearInterval(interval); el.innerHTML = 'Завершено'; loadForecasts(); }
                else { const h = Math.floor(remaining / 3600); const m = Math.floor((remaining % 3600) / 60); el.innerHTML = `${h}г ${m}хв`; }
            }, 1000);
        });
    }

    async function deleteForecast(id) { await fetch(`/api/forecast/${id}`, { method: 'DELETE' }); loadForecasts(); }
    async function clearAllForecasts() { if (confirm('Очистити всі прогнози?')) { await fetch('/api/forecasts/all', { method: 'DELETE' }); loadForecasts(); } }

    // ============ ANALYSIS ============
    async function loadAnalysis() {
        const pair = document.getElementById('analysisPair').value;
        const res = await fetch(`/api/analysis/${pair}`);
        const data = await res.json();
        const container = document.getElementById('analysisResult');
        if (data.error) { container.innerHTML = `<div style="color:#ff4757;">${data.error}</div>`; return; }
        container.innerHTML = `<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:15px;">
            <div class="metric-card"><div class="metric-value">$${data.current_price?.toFixed(2)}</div><div class="metric-label">Ціна</div></div>
            <div class="metric-card"><div class="metric-value" style="color:${data.trend === '📈 ВИСХІДНИЙ' ? '#00ff88' : '#ff4757'}">${data.trend}</div><div class="metric-label">Тренд (сила ${data.trend_strength}%)</div></div>
            <div class="metric-card"><div class="metric-value" style="color:${data.rsi > 70 ? '#ff4757' : (data.rsi < 30 ? '#00ff88' : '#f39c12')}">${data.rsi}</div><div class="metric-label">RSI</div></div>
            <div class="metric-card"><div class="metric-value" style="color:${data.macd_cross === 'bullish' ? '#00ff88' : (data.macd_cross === 'bearish' ? '#ff4757' : '#888')}">${data.macd_cross === 'bullish' ? '🟢 БИЧЯЧИЙ' : (data.macd_cross === 'bearish' ? '🔴 ВЕДМЕЖИЙ' : 'НЕЙТРАЛЬНО')}</div><div class="metric-label">MACD</div></div>
        </div>${data.forecast ? `<div style="margin-top:20px;background:linear-gradient(135deg,rgba(102,126,234,0.2),rgba(118,75,162,0.2));border-radius:15px;padding:20px;text-align:center;"><div style="font-size:32px;font-weight:700;color:${data.forecast === 'LONG' ? '#00ff88' : '#ff4757'}">${data.forecast === 'LONG' ? '📈 LONG' : '📉 SHORT'}</div><div>Ціль: $${data.target_price?.toFixed(2)}</div><div>Впевненість: ${data.confidence}%</div></div>` : '<div style="margin-top:20px;text-align:center;color:#888;">⚠️ Немає чіткого сигналу</div>'}`;
    }

    // ============ CHARTS ============
    let currentChartParams = { pair: 'BTCUSDT', timeframe: '1h', showTrades: true };
    
    async function loadChart() {
        const pair = document.getElementById('chartPair').value;
        const timeframe = document.getElementById('chartTimeframe').value;
        currentChartParams = { pair, timeframe, showTrades: true };
        try {
            const res = await fetch(`/api/chart/${pair}?timeframe=${timeframe}&limit=300&show_trades=true`);
            const data = await res.json();
            Plotly.newPlot('priceChart', data.data, data.layout || {}, { responsive: true });
        } catch(e) {
            console.error('Помилка завантаження графіку:', e);
            document.getElementById('priceChart').innerHTML = '<div style="text-align:center;padding:50px;color:#888;">Помилка завантаження графіку</div>';
        }
    }
    
    function resetChartView() { Plotly.relayout('priceChart', { 'xaxis.autorange': true, 'yaxis.autorange': true }); }
    
    async function loadPnlChart() {
        try {
            const res = await fetch('/api/chart/pnl');
            const data = await res.json();
            Plotly.newPlot('pnlChart', data.data, data.layout || {}, { responsive: true });
        } catch(e) { document.getElementById('pnlChart').innerHTML = '<div style="padding:50px;text-align:center;">Немає даних</div>'; }
    }

    async function loadChartWithTrade(tradeId, pair, entryPrice, side, exitPrice) {
        const timeframe = document.getElementById('chartTimeframe').value;
        try {
            const res = await fetch(`/api/chart/${pair}?timeframe=${timeframe}`);
            const data = await res.json();
            Plotly.newPlot('priceChart', data.data, data.layout || {}, { responsive: true });
            const annotation = {
                text: `${side === 'BUY' ? '🟢 ВХІД' : '🔴 ВХІД'} $${entryPrice.toFixed(0)}`,
                x: new Date(), y: entryPrice, xref: 'x', yref: 'y', showarrow: true, arrowhead: 2, arrowsize: 1,
                arrowwidth: 2, arrowcolor: side === 'BUY' ? '#00ff88' : '#ff4757', ax: 0, ay: -40,
                bgcolor: 'rgba(0,0,0,0.7)', bordercolor: side === 'BUY' ? '#00ff88' : '#ff4757',
                borderwidth: 1, font: { color: 'white', size: 12 }
            };
            const annotations = data.layout?.annotations || [];
            annotations.push(annotation);
            if (exitPrice) {
                annotations.push({
                    text: `🏁 ВИХІД $${exitPrice.toFixed(0)}`,
                    x: new Date(), y: exitPrice, xref: 'x', yref: 'y', showarrow: true, arrowhead: 2,
                    arrowsize: 1, arrowwidth: 2, arrowcolor: 'white', ax: 0, ay: 40,
                    bgcolor: 'rgba(0,0,0,0.7)', bordercolor: 'white', font: { color: 'white', size: 12 }
                });
            }
            Plotly.update('priceChart', {}, { annotations: annotations });
        } catch(e) { console.error(e); loadChart(); }
    }

    // ============ ADVANCED STATS ============
    async function loadAdvancedStats() {
        try {
            const res = await fetch('/api/stats/advanced');
            const data = await res.json();
            if (data.error) return;
            document.getElementById('advWinRate').innerHTML = `${data.win_rate}%`;
            document.getElementById('advProfitFactor').innerHTML = data.profit_factor;
            document.getElementById('advMaxDrawdown').innerHTML = `$${data.max_drawdown}`;
            document.getElementById('advSharpe').innerHTML = data.sharpe_ratio;
            document.getElementById('advExpectancy').innerHTML = `$${data.expectancy}`;
            document.getElementById('advKelly').innerHTML = `${data.kelly_criterion}%`;
            document.getElementById('kelly').innerHTML = `${data.kelly_criterion}%`;
        } catch(e) { console.error(e); }
    }

    // ============ SETTINGS ============
    async function loadSettings() {
        try {
            const res = await fetch('/api/settings');
            const s = await res.json();
            const elements = {
                baseTimeframe: s.trading?.base_timeframe || '5m',
                signalCheckInterval: s.trading?.signal_check_interval || 30,
                emaFast: s.strategy?.ema_fast || 50,
                emaSlow: s.strategy?.ema_slow || 200,
                rsiPeriod: s.strategy?.rsi_period || 14,
                rsiMin: s.strategy?.rsi_min || 40,
                rsiMax: s.strategy?.rsi_max || 60,
                tpPercent: s.strategy?.take_profit_percent || 2.0,
                slPercent: s.strategy?.stop_loss_percent || 1.5,
                riskPerTrade: s.risk?.risk_per_trade || 2.0,
                maxOpenTrades: s.risk?.max_open_trades || 5,
                maxDailyLoss: s.risk?.max_daily_loss || 5.0
            };
            for (const [id, value] of Object.entries(elements)) {
                const el = document.getElementById(id);
                if (el) el.value = value;
            }
            const bollingerEl = document.getElementById('useBollinger');
            if (bollingerEl) bollingerEl.checked = s.strategy?.use_bollinger || false;
            const kellyEl = document.getElementById('useKelly');
            if (kellyEl) kellyEl.checked = s.risk?.use_kelly || false;
            
            try {
                const testingRes = await fetch('/api/settings/testing');
                const testing = await testingRes.json();
                const tradesSelect = document.getElementById('createTradesFromForecasts');
                if (tradesSelect) tradesSelect.value = testing.create_trades_from_forecasts ? 'true' : 'false';
                const percentSlider = document.getElementById('forecastPositionPercent');
                if (percentSlider) {
                    percentSlider.value = testing.forecast_position_percent;
                    const percentValue = document.getElementById('forecastPositionPercentValue');
                    if (percentValue) percentValue.innerHTML = testing.forecast_position_percent + '%';
                }
            } catch(e) { console.error('Помилка завантаження testing налаштувань:', e); }
        } catch(e) { console.error('Помилка завантаження налаштувань:', e); }
    }

    async function saveSettings() {
        const settings = {
            trading: {
                base_timeframe: document.getElementById('baseTimeframe').value,
                signal_check_interval: parseInt(document.getElementById('signalCheckInterval').value)
            },
            strategy: {
                ema_fast: parseInt(document.getElementById('emaFast').value),
                ema_slow: parseInt(document.getElementById('emaSlow').value),
                rsi_period: parseInt(document.getElementById('rsiPeriod').value),
                rsi_min: parseInt(document.getElementById('rsiMin').value),
                rsi_max: parseInt(document.getElementById('rsiMax').value),
                take_profit_percent: parseFloat(document.getElementById('tpPercent').value),
                stop_loss_percent: parseFloat(document.getElementById('slPercent').value),
                use_bollinger: document.getElementById('useBollinger').checked
            },
            risk: {
                risk_per_trade: parseFloat(document.getElementById('riskPerTrade').value),
                max_open_trades: parseInt(document.getElementById('maxOpenTrades').value),
                max_daily_loss: parseFloat(document.getElementById('maxDailyLoss').value),
                use_kelly: document.getElementById('useKelly').checked
            }
        };
        
        try {
            await fetch('/api/settings', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(settings) });
            const createTradesSelect = document.getElementById('createTradesFromForecasts');
            const percentSlider = document.getElementById('forecastPositionPercent');
            if (createTradesSelect && percentSlider) {
                await fetch('/api/settings/testing', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ create_trades_from_forecasts: createTradesSelect.value === 'true', forecast_position_percent: parseFloat(percentSlider.value) }) });
            }
            alert('Налаштування збережено!');
        } catch(e) { console.error('Помилка збереження:', e); alert('Помилка збереження: ' + e.message); }
    }

    // ============ LOGS ============
    async function loadLogs(reset = true) {
        if (reset) { currentLogOffset = 0; hasMoreLogs = true; document.getElementById('loadMoreBtn').style.display = 'none'; }
        const level = document.getElementById('logLevelFilter')?.value || '';
        const res = await fetch(`/api/logs?level=${level}&limit=50&offset=${currentLogOffset}`);
        const data = await res.json();
        const body = document.getElementById('logsBody');
        if (!body) return;
        if (!data.logs || data.logs.length === 0 && currentLogOffset === 0) { body.innerHTML = '<td><td colspan="4" style="text-align:center">Немає логів</td></tr>'; return; }
        const rows = data.logs.map(l => `<tr><td style="white-space:nowrap;">${new Date(l.timestamp).toLocaleString()}</td><td class="log-${l.level.toLowerCase()}">${l.level}</td><td>${l.module || '-'}</td><td>${l.message}</td></tr>`).join('');
        if (reset) body.innerHTML = rows; else body.innerHTML += rows;
        hasMoreLogs = data.logs && data.logs.length === 50;
        document.getElementById('loadMoreBtn').style.display = hasMoreLogs ? 'inline-block' : 'none';
        currentLogOffset += data.logs?.length || 0;
    }
    
    async function loadMoreLogs() { if (hasMoreLogs) await loadLogs(false); }
    async function clearLogs() { if (confirm('Очистити логи?')) { await fetch('/api/logs', { method: 'DELETE' }); loadLogs(true); } }

    // ============ НАВІГАЦІЯ ============
    document.querySelectorAll('.nav-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            document.querySelectorAll('.tab').forEach(tab => tab.classList.remove('active'));
            document.getElementById(`tab-${btn.dataset.tab}`).classList.add('active');
            if (btn.dataset.tab === 'charts') { loadChart(); loadPnlChart(); }
            if (btn.dataset.tab === 'logs') loadLogs(true);
            if (btn.dataset.tab === 'settings') loadSettings();
            if (btn.dataset.tab === 'forecasts') loadForecasts();
            if (btn.dataset.tab === 'advanced') loadAdvancedStats();
            if (btn.dataset.tab === 'forecasts_history') loadForecastsHistory(true);
            if (btn.dataset.tab === 'monitor') loadHealthCheck();
        });
    });

    // ============ СЛАЙДЕР ============
    const percentSlider = document.getElementById('forecastPositionPercent');
    if (percentSlider) {
        percentSlider.addEventListener('input', function() {
            const valueSpan = document.getElementById('forecastPositionPercentValue');
            if (valueSpan) valueSpan.innerHTML = this.value + '%';
        });
    }

    // ============ ІНІЦІАЛІЗАЦІЯ ============
    document.addEventListener('DOMContentLoaded', function() {
        const timeframeSelect = document.getElementById('tradeChartTimeframe');
        if (timeframeSelect) {
            timeframeSelect.addEventListener('change', function() { refreshTradeChart(); });
        }
    });

    connectWebSocket();
    loadDashboard();
    loadPnlChart();
    loadAdvancedStats();
    setInterval(loadDashboard, 5000);
    setInterval(loadForecasts, 10000);
    setInterval(() => { if (document.getElementById('tab-monitor')?.classList.contains('active')) loadHealthCheck(); }, 10000);
</script>

</body>
</html>''')


def start_web_server(host="0.0.0.0", port=8000):
    import uvicorn
    uvicorn.run(app, host=host, port=port, log_level="warning")