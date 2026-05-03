"""
FastAPI веб-інтерфейс для автотрейдинг бота
Розширена версія: Web Push сповіщення, покращена аналітика
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
from db.models import Signal, Trade, Log, ForecastDB
from utils.config_loader import config
from utils.logger import logger

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


async def get_next_forecast_time():
    now = get_current_time()
    next_minute = ((now.minute // 5) + 1) * 5
    next_time = now.replace(minute=0, second=0, microsecond=0) + timedelta(minutes=next_minute)
    if next_time <= now:
        next_time += timedelta(minutes=5)
    return next_time.isoformat()


def set_order_manager(om):
    global order_manager_ref
    order_manager_ref = om


async def get_active_forecasts():
    """Отримання активних прогнозів з БД"""
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
                "created_at": f.created_at.isoformat(),
                "expires_at": f.expires_at.isoformat(),
                "time_remaining": max(0, (f.expires_at - now).total_seconds()),
                "status": f.status,
                "profit_potential": ((f.target_price - f.entry_price) / f.entry_price) * 100
            }
            for f in active
        ]
    finally:
        db.close()


async def create_forecast_internal(pair, signal_type, entry_price, target_price, confidence):
    """Створення прогнозу зі збереженням в БД"""
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
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(hours=4)).isoformat(),
            "time_remaining": 4 * 3600,
            "status": "active",
            "profit_potential": ((target_price - entry_price) / entry_price) * 100
        }

        # Web Push сповіщення
        await send_push_notification(
            f"Новий прогноз {pair}",
            f"{signal_type} сигнал! Ціль: ${target_price:.0f}",
            {"forecast_id": forecast_id}
        )

        for ws in active_websockets:
            try:
                await ws.send_json({
                    "type": "new_forecast",
                    "forecast": forecast_dict
                })
            except:
                pass

        logger.info(f"Прогноз збережено в БД: {pair} {signal_type}")
        return forecast

    except Exception as e:
        logger.error(f"Помилка створення прогнозу: {e}")
        db.rollback()
        return None
    finally:
        db.close()


async def update_forecast_prices():
    """Оновлення поточних цін для активних прогнозів"""
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

                    if forecast.signal_type == "LONG" and current_price >= forecast.target_price:
                        forecast.status = "completed"
                        forecast.result = "success"
                        await send_push_notification(
                            f"Прогноз {forecast.pair} виконано!",
                            f"Ціль досягнута! Прибуток: +{((current_price - forecast.entry_price) / forecast.entry_price * 100):.1f}%"
                        )
                    elif forecast.signal_type == "SHORT" and current_price <= forecast.target_price:
                        forecast.status = "completed"
                        forecast.result = "success"
                        await send_push_notification(
                            f"Прогноз {forecast.pair} виконано!",
                            f"Ціль досягнута! Прибуток: +{((forecast.entry_price - current_price) / forecast.entry_price * 100):.1f}%"
                        )
                db.commit()
            except Exception as e:
                logger.error(f"Помилка оновлення ціни для {forecast.pair}: {e}")
    finally:
        db.close()


async def send_push_notification(title: str, body: str, data: dict = None):
    """Відправка Web Push сповіщення всім підписникам"""
    for ws in active_websockets:
        try:
            await ws.send_json({
                "type": "notification",
                "title": title,
                "body": body,
                "data": data
            })
        except:
            pass

    # Також відправляємо через Telegram якщо є
    if order_manager_ref and order_manager_ref.telegram:
        try:
            await order_manager_ref.telegram.send_message(f"🔔 {title}\n{body}")
        except:
            pass


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
                        "profit_factor": stats.get('profit_factor', 0),
                        "max_drawdown": stats.get('max_drawdown', 0)
                    })
                finally:
                    db.close()
            await asyncio.sleep(1)
    except WebSocketDisconnect:
        if websocket in active_websockets:
            active_websockets.remove(websocket)


# ============ API ЕНДПОІНТИ ============
@app.get("/api/chart/pnl")
async def get_pnl_chart_data():
    """Графік PnL за період"""
    db = SessionLocal()
    db_ops = DatabaseOperations(db)
    try:
        trades = db_ops.get_trades_history(limit=1000, is_paper=True)

        if not trades:
            fig = go.Figure()
            fig.update_layout(title="Немає даних для відображення", template="plotly_dark", height=400)
            return JSONResponse(content=json.loads(json.dumps(fig, cls=plotly.utils.PlotlyJSONEncoder)))

        # Групуємо по днях
        daily_pnl = {}
        for trade in trades:
            if trade.closed_at:
                date = trade.closed_at.date().isoformat()
                daily_pnl[date] = daily_pnl.get(date, 0) + trade.pnl

        pnl_data = [{"date": date, "pnl": pnl} for date, pnl in sorted(daily_pnl.items())]

        # Кумулятивний PnL
        cumulative = 0
        for item in pnl_data:
            cumulative += item["pnl"]
            item["cumulative"] = cumulative

        fig = go.Figure()

        # Стовпці (денний PnL)
        fig.add_trace(go.Bar(
            x=[d["date"] for d in pnl_data],
            y=[d["pnl"] for d in pnl_data],
            name='Денний PnL',
            marker_color=['#00ff88' if d["pnl"] > 0 else '#ff4757' for d in pnl_data]
        ))

        # Лінія (кумулятивний PnL)
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
            plot_bgcolor='rgba(26,26,46,0.5)',
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1)
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
async def get_chart(pair: str, timeframe: str = "1h", limit: int = 200, highlight_trade: int = None):
    """Отримання свічкового графіку з точками входу/виходу"""
    from exchange.bybit_client import BybitClient
    import pandas_ta as ta

    exchange = BybitClient()
    df = exchange.get_klines(pair, timeframe, limit)

    if df is None or df.empty:
        raise HTTPException(status_code=404, detail="Дані не знайдено")

    # Розраховуємо EMA
    df['EMA_20'] = ta.ema(df['close'], length=20)
    df['EMA_50'] = ta.ema(df['close'], length=50)
    df['EMA_200'] = ta.ema(df['close'], length=200)

    # Отримуємо угоди для цієї пари
    db = SessionLocal()
    db_ops = DatabaseOperations(db)
    try:
        trades = db_ops.get_trades_history(limit=500, is_paper=True)
        pair_trades = [t for t in trades if t.pair == pair]

        # Якщо потрібно підсвітити конкретну угоду
        highlighted_trade = None
        if highlight_trade:
            for t in pair_trades:
                if t.id == highlight_trade:
                    highlighted_trade = t
                    break
    finally:
        db.close()

    # Підготовка даних для точок входу/виходу
    entry_points = []
    exit_points = []
    entry_colors = []
    entry_texts = []
    exit_texts = []

    # Розміри точок (більші для підсвіченої угоди)
    entry_sizes = []
    exit_sizes = []

    for trade in pair_trades:
        is_highlighted = (highlighted_trade and trade.id == highlighted_trade.id)

        # Розмір точки: 16 для підсвіченої, 10 для звичайної
        size = 16 if is_highlighted else 10

        # Точка входу
        entry_time = trade.opened_at
        entry_price = trade.entry_price
        entry_points.append((entry_time, entry_price))
        entry_sizes.append(size)

        if trade.side.value == "BUY":
            entry_colors.append("green")
            entry_texts.append(f"ВХІД LONG<br>Ціна: ${entry_price:.0f}<br>Кількість: {trade.quantity}")
        else:
            entry_colors.append("red")
            entry_texts.append(f"ВХІД SHORT<br>Ціна: ${entry_price:.0f}<br>Кількість: {trade.quantity}")

        # Точка виходу
        if trade.closed_at and trade.exit_price:
            exit_time = trade.closed_at
            exit_price = trade.exit_price
            exit_points.append((exit_time, exit_price))
            exit_sizes.append(size)
            pnl_text = f"+${trade.pnl:.2f}" if trade.pnl >= 0 else f"-${abs(trade.pnl):.2f}"
            exit_texts.append(f"ВИХІД<br>Ціна: ${exit_price:.0f}<br>PnL: {pnl_text}")

    # Створюємо графік
    fig = go.Figure()

    # Свічковий графік
    fig.add_trace(go.Candlestick(
        x=df['timestamp'],
        open=df['open'],
        high=df['high'],
        low=df['low'],
        close=df['close'],
        name='Ціна',
        showlegend=True
    ))

    # EMA лінії
    fig.add_trace(go.Scatter(
        x=df['timestamp'], y=df['EMA_20'],
        name='EMA 20', line=dict(color='#f39c12', width=1.5), opacity=0.8
    ))
    fig.add_trace(go.Scatter(
        x=df['timestamp'], y=df['EMA_50'],
        name='EMA 50', line=dict(color='#00d4ff', width=1.5), opacity=0.8
    ))
    fig.add_trace(go.Scatter(
        x=df['timestamp'], y=df['EMA_200'],
        name='EMA 200', line=dict(color='#ff4757', width=1.5), opacity=0.8
    ))

    # Точки входу
    if entry_points:
        fig.add_trace(go.Scatter(
            x=[p[0] for p in entry_points],
            y=[p[1] for p in entry_points],
            mode='markers',
            name='Входи',
            marker=dict(
                size=entry_sizes,
                color=entry_colors,
                symbol='triangle-up',
                line=dict(width=2, color='white')
            ),
            text=entry_texts,
            hoverinfo='text',
            hovertemplate='%{text}<extra></extra>'
        ))

    # Точки виходу
    if exit_points:
        fig.add_trace(go.Scatter(
            x=[p[0] for p in exit_points],
            y=[p[1] for p in exit_points],
            mode='markers',
            name='Виходи',
            marker=dict(
                size=exit_sizes,
                color='white',
                symbol='circle',
                line=dict(width=2, color='black')
            ),
            text=exit_texts,
            hoverinfo='text',
            hovertemplate='%{text}<extra></extra>'
        ))

    # Якщо є підсвічена угода, додаємо лінію між входом і виходом
    if highlighted_trade and highlighted_trade.closed_at and highlighted_trade.exit_price:
        fig.add_trace(go.Scatter(
            x=[highlighted_trade.opened_at, highlighted_trade.closed_at],
            y=[highlighted_trade.entry_price, highlighted_trade.exit_price],
            mode='lines',
            name='Угода',
            line=dict(
                color='#00d4ff',
                width=3,
                dash='dash'
            ),
            hoverinfo='text',
            text=f'Вхід: ${highlighted_trade.entry_price:.0f}<br>Вихід: ${highlighted_trade.exit_price:.0f}<br>PnL: ${highlighted_trade.pnl:.2f}'
        ))

    fig.update_layout(
        title=f"{pair} - Свічковий графік з точками входу/виходу",
        xaxis_title="Час",
        yaxis_title="Ціна (USDT)",
        template="plotly_dark",
        height=600,
        margin=dict(l=50, r=50, t=50, b=50),
        paper_bgcolor='rgba(0,0,0,0)',
        plot_bgcolor='rgba(26,26,46,0.5)',
        xaxis_rangeslider_visible=False,
        hovermode='closest',
        legend=dict(
            orientation="h",
            yanchor="bottom",
            y=1.02,
            xanchor="right",
            x=1
        )
    )

    return JSONResponse(content=json.loads(json.dumps(fig, cls=plotly.utils.PlotlyJSONEncoder)))


@app.post("/api/test/trade")
async def create_test_trade():
    """Створення тестової угоди для перевірки графіка"""
    from db.models import OrderSide, OrderStatus
    from datetime import datetime, timedelta
    import random

    db = SessionLocal()
    try:
        # Створюємо тестову угоду LONG
        now = datetime.now()
        entry_price = 43000 + random.randint(-500, 500)
        exit_price = entry_price * (1 + random.uniform(0.005, 0.03))  # Випадковий прибуток

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

        # Створюємо відкриту угоду
        open_entry = 43500 + random.randint(-300, 300)
        open_trade = Trade(
            pair="BTCUSDT",
            side=OrderSide.BUY if random.random() > 0.5 else OrderSide.SELL,
            entry_price=open_entry,
            quantity=0.002,
            is_paper=1,
            status=OrderStatus.PENDING,
            opened_at=now - timedelta(hours=random.randint(1, 12)),
            take_profit=open_entry * 1.02,
            stop_loss=open_entry * 0.98
        )
        db.add(open_trade)

        db.commit()

        return {
            "status": "success",
            "message": "Створено тестові угоди",
            "closed_trade": {
                "entry": test_trade.entry_price,
                "exit": test_trade.exit_price,
                "pnl": test_trade.pnl
            },
            "open_trade": {
                "entry": open_trade.entry_price,
                "side": "LONG" if open_trade.side == OrderSide.BUY else "SHORT"
            }
        }
    except Exception as e:
        db.rollback()
        return {"status": "error", "message": str(e)}
    finally:
        db.close()


@app.delete("/api/test/trades")
async def clear_test_trades():
    """Видалення всіх тестових угод (paper trading)"""
    db = SessionLocal()
    try:
        # Видаляємо тільки paper угоди (is_paper=1)
        db.query(Trade).filter(Trade.is_paper == 1).delete()
        db.commit()

        # Скидаємо баланс
        from db.operations import DatabaseOperations
        db_ops = DatabaseOperations(db)
        db_ops.reset_paper_balance(100.0)

        return {"status": "success", "message": "Тестові угоди видалено, баланс скинуто"}
    except Exception as e:
        db.rollback()
        return {"status": "error", "message": str(e)}
    finally:
        db.close()


@app.get("/api/analysis/{pair}")
async def get_market_analysis(pair: str):
    """Ринковий аналіз для пари"""
    if order_manager_ref:
        analysis = order_manager_ref.analyze_market(pair)
        return analysis
    return {"error": "Order manager not available"}

@app.get("/api/status")
async def get_status():
    db = SessionLocal()
    db_ops = DatabaseOperations(db)
    try:
        next_forecast = await get_next_forecast_time()
        stats = db_ops.get_stats(is_paper=True)
        return {
            "status": "running" if order_manager_ref and order_manager_ref.running else "stopped",
            "mode": config.bot_mode,
            "balance": db_ops.get_balance("USDT", is_paper=True),
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
                "opened_at": t.opened_at.isoformat(),
                "closed_at": t.closed_at.isoformat() if t.closed_at else None
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
                "stop_loss": t.stop_loss,
                "current_pnl": 0
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


@app.get("/api/stats/advanced")
async def get_advanced_stats():
    """Покращена аналітика"""
    db = SessionLocal()
    db_ops = DatabaseOperations(db)
    try:
        trades = db_ops.get_trades_history(limit=1000, is_paper=True)

        if not trades or len(trades) < 5:
            return {
                "total_trades": 0,
                "win_rate": 0,
                "profit_factor": 0,
                "max_drawdown": 0,
                "sharpe_ratio": 0,
                "expectancy": 0,
                "kelly_criterion": 0
            }

        # Розрахунок метрик
        wins = [t for t in trades if t.pnl > 0]
        losses = [t for t in trades if t.pnl < 0]

        win_rate = len(wins) / len(trades) * 100 if trades else 0
        avg_win = sum(t.pnl for t in wins) / len(wins) if wins else 0
        avg_loss = abs(sum(t.pnl for t in losses) / len(losses)) if losses else 0

        # Profit Factor
        gross_profit = sum(t.pnl for t in wins) if wins else 0
        gross_loss = abs(sum(t.pnl for t in losses)) if losses else 1
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else 0

        # Max Drawdown
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

        # Sharpe Ratio (спрощено)
        returns = [t.pnl for t in trades]
        avg_return = sum(returns) / len(returns) if returns else 0
        std_return = (sum((r - avg_return) ** 2 for r in returns) / len(returns)) ** 0.5 if returns else 1
        sharpe_ratio = (avg_return / std_return) * (252 ** 0.5) if std_return > 0 else 0

        # Kelly Criterion
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
            "expectancy": round((win_rate / 100 * avg_win - (1 - win_rate / 100) * avg_loss), 2),
            "kelly_criterion": round(kelly * 100, 2)
        }
    except Exception as e:
        logger.error(f"Помилка розрахунку статистики: {e}")
        return {"error": str(e)}
    finally:
        db.close()


@app.get("/api/settings")
async def get_settings():
    return {
        "trading": {
            "pairs": config.get('trading.pairs', ['BTCUSDT', 'ETHUSDT', 'SOLUSDT']),
            "base_timeframe": config.get('trading.base_timeframe', '5m'),
            "signal_check_interval": config.get('trading.signal_check_interval', 30),
            "trading_hours": config.get('trading.trading_hours', {"enabled": False, "start": "09:00", "end": "21:00"}),
            "news_filter": config.get('trading.news_filter', {"enabled": False})
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
            "use_ichimoku": config.get('strategy.use_ichimoku', False),
            "use_fibonacci": config.get('strategy.use_fibonacci', True)
        },
        "risk": {
            "risk_per_trade": config.get('risk.risk_per_trade', 2.0),
            "max_open_trades": config.get('risk.max_open_trades', 5),
            "max_daily_loss": config.get('risk.max_daily_loss', 5.0),
            "use_kelly": config.get('risk.use_kelly', True),
            "kelly_window": config.get('risk.kelly_window', 50)
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


# ============ WEB PUSH ============

@app.post("/api/push/subscribe")
async def push_subscribe(subscription: dict):
    push_subscriptions.append(subscription)
    return {"status": "success"}


# ============ HTML СТОРІНКА ============

@app.get("/", response_class=HTMLResponse)
async def root():
    return HTMLResponse(content='''<!DOCTYPE html>
<html lang="uk">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>AutoTrading Bot v3.0 - Розширений дашборд</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
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
            <button class="nav-btn" data-tab="analysis">📊 Аналіз</button>
            <button class="nav-btn" data-tab="charts">📈 Графіки</button>
            <button class="nav-btn" data-tab="advanced">📐 Покращена аналітика</button>
            <button class="nav-btn" data-tab="settings">⚙️ Налаштування</button>
            <button class="nav-btn" data-tab="logs">📜 Логи</button>
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
            <div class="card"><div class="card-header"><h3>📈 Відкриті позиції</h3></div><div class="table-wrapper"><table><thead><tr><th>Пара</th><th>Сторона</th><th>Вхід</th><th>Кількість</th><th>TP</th><th>SL</th></tr></thead><tbody id="openTradesBody"><tr><td colspan="6" style="text-align:center">Завантаження......</tbody></table></div></div>
            <div class="card"><div class="card-header"><h3>📜 Історія угод</h3></div><div class="table-wrapper"><table><thead><tr><th>Час</th><th>Пара</th><th>Сторона</th><th>Вхід</th><th>Вихід</th><th>PnL</th></tr></thead><tbody id="tradesBody">...</tbody></table></div></div>
            <div class="server-time" id="serverTime"></div>
        </div>

        <!-- ПРОГНОЗИ -->
        <div id="tab-forecasts" class="tab">
            <div class="card"><div class="card-header"><h3>🎯 Прогнози</h3><span style="font-size:12px;color:#888;">Автоматичні прогнози на основі технічного аналізу</span></div><div style="padding:15px;background:rgba(0,255,136,0.1);margin:15px;border-radius:10px;">📊 Прогнози генеруються автоматично при виявленні торгових сигналів.<br>Кожен прогноз діє 4 години.</div></div>
            <div class="card"><div class="card-header"><h3>⏰ Активні прогнози</h3><button class="btn btn-outline btn-sm" onclick="clearAllForecasts()" style="margin:10px;">🗑️ Очистити всі</button></div><div class="table-wrapper"><table><thead><tr><th>Пара</th><th>Тип</th><th>Вхід</th><th>Ціль</th><th>Поточна</th><th>Прибуток</th><th>Впевн.</th><th>Час</th><th>Дії</th></tr></thead><tbody id="forecastsBody">...</tbody></table></div></div>
        </div>

        <!-- АНАЛІЗ -->
        <div id="tab-analysis" class="tab">
            <div class="card"><div class="card-header"><h3>📊 Ринковий аналіз</h3><div style="display:flex;gap:10px;"><select id="analysisPair" style="background:#1a1a2e;border:1px solid #667eea;border-radius:10px;padding:8px;color:white;"><option value="BTCUSDT">BTCUSDT</option><option value="ETHUSDT">ETHUSDT</option><option value="SOLUSDT">SOLUSDT</option></select><select id="analysisTimeframe" style="background:#1a1a2e;border:1px solid #667eea;border-radius:10px;padding:8px;color:white;"><option value="1h">1 година</option><option value="4h">4 години</option><option value="1d">1 день</option></select><button class="btn btn-primary btn-sm" onclick="loadAnalysis()">Аналіз</button></div></div><div id="analysisResult" style="padding:20px;">Виберіть пару</div></div>
        </div>

        <!-- ГРАФІКИ -->
        <div id="tab-charts" class="tab">
            <div class="card"><div class="card-header"><h3>📊 Графік</h3><div style="display:flex;gap:10px;"><select id="chartPair" style="background:#1a1a2e;border:1px solid #667eea;border-radius:10px;padding:8px;color:white;"><option value="BTCUSDT">BTCUSDT</option><option value="ETHUSDT">ETHUSDT</option><option value="SOLUSDT">SOLUSDT</option></select><select id="chartTimeframe" style="background:#1a1a2e;border:1px solid #667eea;border-radius:10px;padding:8px;color:white;"><option value="1h">1 година</option><option value="4h">4 години</option><option value="1d">1 день</option></select><button class="btn btn-primary btn-sm" onclick="loadChart()">Оновити</button></div></div><div class="chart-container" id="priceChart"></div></div>
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

    <div class="notification-permission" onclick="enableNotifications()">🔔 Увімкнути сповіщення</div>

    <script>
        let ws = null;
        let currentLogOffset = 0;
        let hasMoreLogs = true;
        let nextForecastTimer = null;
        let notificationsEnabled = false;

        function connectWebSocket() {
            const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
            ws = new WebSocket(`${protocol}//${window.location.host}/ws`);
            ws.onmessage = function(event) {
                const data = JSON.parse(event.data);
                if (data.type === 'status') {
                    updateDashboard(data);
                }
                if (data.type === 'notification') {
                    showNotification(data.title, data.body);
                }
                if (data.type === 'new_forecast') loadForecasts();
            };
            ws.onclose = function() { setTimeout(connectWebSocket, 3000); };
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
                new Notification(title, { body, icon: '/icon.png' });
            }
        }

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
            });
        });

        async function loadDashboard() {
    try {
        const res = await fetch('/api/status');
        const data = await res.json();
        updateDashboard(data);

        const openRes = await fetch('/api/open_trades');
        const openTrades = await openRes.json();
        const openBody = document.getElementById('openTradesBody');
        if(openBody) openBody.innerHTML = openTrades.length ? openTrades.map(t => `
            <tr style="cursor:pointer;" onclick="showTradeOnChart(${t.id}, '${t.pair}', ${t.entry_price}, '${t.side}', null)">
                <td>${t.pair}</td>
                <td><span class="badge ${t.side === 'BUY' ? 'badge-buy' : 'badge-sell'}">${t.side === 'BUY' ? 'LONG' : 'SHORT'}</span></td>
                <td>$${t.entry_price.toFixed(0)}</td>
                <td>${t.quantity}</td>
                <td>$${t.take_profit?.toFixed(0) || '-'}</td>
                <td>$${t.stop_loss?.toFixed(0) || '-'}</td>
            </tr>
        `).join('') : '<tr><td colspan="6" style="text-align:center">Немає відкритих позицій</td></tr>';

        const tradesRes = await fetch('/api/trades?limit=20');
        const trades = await tradesRes.json();
        const tradesBody = document.getElementById('tradesBody');
        if(tradesBody) tradesBody.innerHTML = trades.length ? trades.map(t => `
            <tr style="cursor:pointer;" onclick="showTradeOnChart(${t.id}, '${t.pair}', ${t.entry_price}, '${t.side}', ${t.exit_price || 'null'})">
                <td style="white-space: nowrap;">${new Date(t.opened_at).toLocaleString()}</td>
                <td>${t.pair}</td>
                <td><span class="badge ${t.side === 'BUY' ? 'badge-buy' : 'badge-sell'}">${t.side === 'BUY' ? 'КУПІВЛЯ' : 'ПРОДАЖ'}</span></td>
                <td>$${t.entry_price.toFixed(0)}</td>
                <td>${t.exit_price ? '$' + t.exit_price.toFixed(0) : '-'}</td>
                <td class="${t.pnl >= 0 ? 'positive' : 'negative'}">${t.pnl >= 0 ? '+' : ''}$${t.pnl.toFixed(2)}</td>
            </tr>
        `).join('') : '<tr><td colspan="6" style="text-align:center">Ще немає угод</td></tr>';
    } catch(e) { console.error(e); }
}

// Функція для показу угоди на графіку
function showTradeOnChart(tradeId, pair, entryPrice, side, exitPrice) {
    // Перемикаємо на вкладку "Графіки"
    document.querySelectorAll('.nav-btn').forEach(btn => {
        if (btn.dataset.tab === 'charts') {
            btn.click();
        }
    });
    
    // Оновлюємо вибрану пару в селекторі
    const chartPairSelect = document.getElementById('chartPair');
    if (chartPairSelect) {
        chartPairSelect.value = pair;
    }
    
    // Завантажуємо графік з підсвічуванням угоди
    loadChartWithTrade(tradeId, pair, entryPrice, side, exitPrice);
}

async function loadChartWithTrade(tradeId, pair, entryPrice, side, exitPrice) {
    const timeframe = document.getElementById('chartTimeframe').value;
    
    try {
        const res = await fetch(`/api/chart/${pair}?timeframe=${timeframe}&highlight_trade=${tradeId}`);
        const data = await res.json();
        Plotly.newPlot('priceChart', data.data, data.layout || {}, { responsive: true });
        
        // Додаємо анотацію на графік
        const annotation = {
            text: `${side === 'BUY' ? '🟢 ВХІД' : '🔴 ВХІД'} $${entryPrice.toFixed(0)}`,
            x: new Date(),
            y: entryPrice,
            xref: 'x',
            yref: 'y',
            showarrow: true,
            arrowhead: 2,
            arrowsize: 1,
            arrowwidth: 2,
            arrowcolor: side === 'BUY' ? '#00ff88' : '#ff4757',
            ax: 0,
            ay: -40,
            bgcolor: 'rgba(0,0,0,0.7)',
            bordercolor: side === 'BUY' ? '#00ff88' : '#ff4757',
            borderwidth: 1,
            borderpad: 4,
            font: { color: 'white', size: 12 }
        };
        
        const annotations = data.layout?.annotations || [];
        annotations.push(annotation);
        
        if (exitPrice) {
            annotations.push({
                text: `🏁 ВИХІД $${exitPrice.toFixed(0)}`,
                x: new Date(),
                y: exitPrice,
                xref: 'x',
                yref: 'y',
                showarrow: true,
                arrowhead: 2,
                arrowsize: 1,
                arrowwidth: 2,
                arrowcolor: 'white',
                ax: 0,
                ay: 40,
                bgcolor: 'rgba(0,0,0,0.7)',
                bordercolor: 'white',
                borderwidth: 1,
                font: { color: 'white', size: 12 }
            });
        }
        
        Plotly.update('priceChart', {}, { annotations: annotations });
        
    } catch(e) {
        console.error('Помилка завантаження графіку з угодою:', e);
        loadChart(); // fallback
    }
}

        async function loadForecasts() {
            try {
                const res = await fetch('/api/forecasts');
                const forecasts = await res.json();
                const body = document.getElementById('forecastsBody');
                if(body) body.innerHTML = forecasts.length ? forecasts.map(f => { const profit = ((f.current_price - f.entry_price) / f.entry_price * 100 * (f.signal_type === 'LONG' ? 1 : -1)).toFixed(1); const hours = Math.floor(f.time_remaining / 3600); const minutes = Math.floor((f.time_remaining % 3600) / 60); return `<tr><td>${f.pair}</td><td><span class="badge ${f.signal_type === 'LONG' ? 'badge-long' : 'badge-short'}">${f.signal_type === 'LONG' ? '📈 LONG' : '📉 SHORT'}</span></td><td>$${f.entry_price.toFixed(0)}</td><td>$${f.target_price.toFixed(0)}</td><td>$${f.current_price.toFixed(0)}</td><td class="${profit >= 0 ? 'positive' : 'negative'}">${profit}%</td><td>${f.confidence}%</td><td class="timer" data-expires="${f.expires_at}">${hours}г ${minutes}хв</td><td><button class="btn btn-outline btn-sm" onclick="deleteForecast(${f.id})">🗑️</button></td></tr>`; }).join('') : '<tr><td colspan="9">Немає активних прогнозів</td></tr>';
                startTimers();
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

        async function loadChart() {
            const pair = document.getElementById('chartPair').value;
            const timeframe = document.getElementById('chartTimeframe').value;
            const res = await fetch(`/api/chart/${pair}?timeframe=${timeframe}`);
            const data = await res.json();
            Plotly.newPlot('priceChart', data.data, data.layout || {}, { responsive: true });
        }

        async function loadPnlChart() {
            try {
                const res = await fetch('/api/chart/pnl');
                const data = await res.json();
                Plotly.newPlot('pnlChart', data.data, data.layout || {}, { responsive: true });
            } catch(e) { document.getElementById('pnlChart').innerHTML = '<div style="padding:50px;text-align:center;">Немає даних</div>'; }
        }

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

        async function loadSettings() {
            const res = await fetch('/api/settings');
            const s = await res.json();
            if(document.getElementById('baseTimeframe')) document.getElementById('baseTimeframe').value = s.trading?.base_timeframe || '5m';
            if(document.getElementById('signalCheckInterval')) document.getElementById('signalCheckInterval').value = s.trading?.signal_check_interval || 30;
            if(document.getElementById('emaFast')) document.getElementById('emaFast').value = s.strategy?.ema_fast || 50;
            if(document.getElementById('emaSlow')) document.getElementById('emaSlow').value = s.strategy?.ema_slow || 200;
            if(document.getElementById('rsiPeriod')) document.getElementById('rsiPeriod').value = s.strategy?.rsi_period || 14;
            if(document.getElementById('rsiMin')) document.getElementById('rsiMin').value = s.strategy?.rsi_min || 40;
            if(document.getElementById('rsiMax')) document.getElementById('rsiMax').value = s.strategy?.rsi_max || 60;
            if(document.getElementById('tpPercent')) document.getElementById('tpPercent').value = s.strategy?.take_profit_percent || 2.0;
            if(document.getElementById('slPercent')) document.getElementById('slPercent').value = s.strategy?.stop_loss_percent || 1.5;
            if(document.getElementById('riskPerTrade')) document.getElementById('riskPerTrade').value = s.risk?.risk_per_trade || 2.0;
            if(document.getElementById('maxOpenTrades')) document.getElementById('maxOpenTrades').value = s.risk?.max_open_trades || 5;
            if(document.getElementById('maxDailyLoss')) document.getElementById('maxDailyLoss').value = s.risk?.max_daily_loss || 5.0;
            if(document.getElementById('useBollinger')) document.getElementById('useBollinger').checked = s.strategy?.use_bollinger || false;
            if(document.getElementById('useKelly')) document.getElementById('useKelly').checked = s.risk?.use_kelly || false;
        }

        async function saveSettings() {
            const settings = {
                trading: { base_timeframe: document.getElementById('baseTimeframe').value, signal_check_interval: parseInt(document.getElementById('signalCheckInterval').value) },
                strategy: { ema_fast: parseInt(document.getElementById('emaFast').value), ema_slow: parseInt(document.getElementById('emaSlow').value), rsi_period: parseInt(document.getElementById('rsiPeriod').value), rsi_min: parseInt(document.getElementById('rsiMin').value), rsi_max: parseInt(document.getElementById('rsiMax').value), take_profit_percent: parseFloat(document.getElementById('tpPercent').value), stop_loss_percent: parseFloat(document.getElementById('slPercent').value), use_bollinger: document.getElementById('useBollinger').checked },
                risk: { risk_per_trade: parseFloat(document.getElementById('riskPerTrade').value), max_open_trades: parseInt(document.getElementById('maxOpenTrades').value), max_daily_loss: parseFloat(document.getElementById('maxDailyLoss').value), use_kelly: document.getElementById('useKelly').checked }
            };
            await fetch('/api/settings', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(settings) });
            alert('Налаштування збережено!');
        }

        async function loadLogs(reset = true) {
            if (reset) { currentLogOffset = 0; hasMoreLogs = true; document.getElementById('loadMoreBtn').style.display = 'none'; }
            const level = document.getElementById('logLevelFilter')?.value || '';
            const res = await fetch(`/api/logs?level=${level}&limit=50&offset=${currentLogOffset}`);
            const data = await res.json();
            const body = document.getElementById('logsBody');
            if (!body) return;
            if (!data.logs || data.logs.length === 0 && currentLogOffset === 0) { body.innerHTML = '<tr><td colspan="4" style="text-align:center">Немає логів</td></tr>'; return; }
            const rows = data.logs.map(l => `<tr><td style="white-space:nowrap;">${new Date(l.timestamp).toLocaleString()}</td><td><span class="log-${l.level.toLowerCase()}">${l.level}</span></td><td>${l.module || '-'}</td><td>${l.message}</td></tr>`).join('');
            if (reset) body.innerHTML = rows; else body.innerHTML += rows;
            hasMoreLogs = data.logs && data.logs.length === 50;
            document.getElementById('loadMoreBtn').style.display = hasMoreLogs ? 'inline-block' : 'none';
            currentLogOffset += data.logs?.length || 0;
        }

        async function loadMoreLogs() { if (hasMoreLogs) await loadLogs(false); }
        async function clearLogs() { if (confirm('Очистити логи?')) { await fetch('/api/logs', { method: 'DELETE' }); loadLogs(true); } }

        connectWebSocket();
        loadDashboard();
        loadPnlChart();
        loadAdvancedStats();
        setInterval(loadDashboard, 5000);
        setInterval(loadForecasts, 10000);
    </script>
</body>
</html>''')


def start_web_server(host="0.0.0.0", port=8000):
    import uvicorn
    uvicorn.run(app, host=host, port=port, log_level="warning")