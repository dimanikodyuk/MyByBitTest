import os
from pathlib import Path
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from app.database.db_manager import db
from app.trading.strategy_controller import strategy_controller
from app.config import config
from loguru import logger
import secrets
from datetime import datetime, timedelta
import json

app = FastAPI(title="ByBit Trading Bot")
security = HTTPBasic()

# Створюємо необхідні директорії
BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)


# Функція аутентифікації
def authenticate(credentials: HTTPBasicCredentials = Depends(security)):
    correct_username = secrets.compare_digest(credentials.username, config.WEB_USERNAME)
    correct_password = secrets.compare_digest(credentials.password, config.WEB_PASSWORD)
    if not (correct_username and correct_password):
        raise HTTPException(status_code=401, detail="Unauthorized")
    return credentials.username


# ==================== HTML СТОРІНКА ====================
html_content = """
<!DOCTYPE html>
<html lang="uk">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>ByBit Trading Bot - Професійний Трейдинг</title>
    <!-- TradingView Lightweight Charts -->
    <script src="https://unpkg.com/lightweight-charts/dist/lightweight-charts.standalone.production.js"></script>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: #131722;
            min-height: 100vh;
            color: #d1d4dc;
        }
        .container { max-width: 1600px; margin: 0 auto; padding: 20px; }

        .header {
            background: #1e222d;
            border-radius: 8px;
            padding: 20px;
            margin-bottom: 20px;
            border: 1px solid #2a2e39;
        }
        .header h1 { color: #2962ff; display: flex; align-items: center; gap: 15px; flex-wrap: wrap; font-size: 24px; }
        .status-badge {
            padding: 5px 15px;
            border-radius: 20px;
            font-size: 12px;
            font-weight: bold;
            display: inline-flex;
            align-items: center;
            gap: 8px;
        }
        .status-running { background: #00bcd4; color: #fff; animation: pulse 2s infinite; }
        .status-stopped { background: #ff5252; color: #fff; }
        @keyframes pulse {
            0% { opacity: 1; }
            50% { opacity: 0.6; }
            100% { opacity: 1; }
        }

        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
            gap: 15px;
            margin-bottom: 20px;
        }
        .stat-card {
            background: #1e222d;
            border-radius: 8px;
            padding: 15px;
            text-align: center;
            border: 1px solid #2a2e39;
        }
        .stat-card h3 { color: #787b86; font-size: 11px; margin-bottom: 8px; text-transform: uppercase; }
        .stat-value { font-size: 22px; font-weight: bold; }
        .positive { color: #00bcd4; }
        .negative { color: #ff5252; }

        .controls {
            display: flex;
            gap: 15px;
            margin-bottom: 20px;
            justify-content: center;
            flex-wrap: wrap;
        }
        .btn {
            padding: 10px 24px;
            border: none;
            border-radius: 6px;
            cursor: pointer;
            font-weight: bold;
            font-size: 13px;
            transition: all 0.3s;
            display: inline-flex;
            align-items: center;
            gap: 8px;
        }
        .btn-start { background: #00bcd4; color: white; }
        .btn-stop { background: #ff5252; color: white; }
        .btn-reset { background: #ff9800; color: white; }
        .btn:hover { opacity: 0.8; transform: translateY(-1px); }

        .chart-container {
            background: #1e222d;
            border-radius: 8px;
            padding: 20px;
            margin-bottom: 20px;
            border: 1px solid #2a2e39;
        }
        .chart-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 20px;
            flex-wrap: wrap;
            gap: 15px;
        }
        .chart-controls {
            display: flex;
            gap: 10px;
            align-items: center;
            flex-wrap: wrap;
        }
        .chart-select, .timeframe-btn {
            padding: 6px 12px;
            border-radius: 6px;
            border: 1px solid #2a2e39;
            background: #131722;
            color: #d1d4dc;
            cursor: pointer;
            font-size: 12px;
        }
        .timeframe-btn.active {
            background: #2962ff;
            border-color: #2962ff;
            color: white;
        }
        #chart {
            width: 100%;
            height: 500px;
        }

        .table-container {
            background: #1e222d;
            border-radius: 8px;
            padding: 20px;
            margin-bottom: 20px;
            border: 1px solid #2a2e39;
            overflow-x: auto;
        }
        table { width: 100%; border-collapse: collapse; }
        th, td { padding: 10px; text-align: left; border-bottom: 1px solid #2a2e39; font-size: 12px; }
        th { color: #787b86; font-weight: 600; }
        tr:hover { background: #2a2e39; }

        .badge {
            padding: 2px 6px;
            border-radius: 4px;
            font-size: 10px;
            font-weight: bold;
            display: inline-block;
        }
        .badge-buy { background: rgba(0,188,212,0.2); color: #00bcd4; }
        .badge-sell { background: rgba(255,82,82,0.2); color: #ff5252; }
        .badge-open { background: rgba(255,152,0,0.2); color: #ff9800; }
        .badge-closed { background: rgba(120,123,134,0.2); color: #787b86; }
        .badge-pending { background: rgba(41,98,255,0.2); color: #2962ff; }
        .badge-success { background: rgba(0,188,212,0.2); color: #00bcd4; }
        .badge-failed { background: rgba(255,82,82,0.2); color: #ff5252; }

        .btn-sm { padding: 4px 8px; font-size: 10px; margin: 0 2px; cursor: pointer; border: none; border-radius: 4px; }
        .btn-info { background: #2962ff; color: white; }
        .btn-danger-sm { background: #ff5252; color: white; }

        .modal {
            display: none;
            position: fixed;
            z-index: 1000;
            left: 0;
            top: 0;
            width: 100%;
            height: 100%;
            background: rgba(0,0,0,0.8);
        }
        .modal-content {
            background: #1e222d;
            margin: 5% auto;
            padding: 20px;
            border-radius: 8px;
            width: 90%;
            max-width: 1000px;
            border: 1px solid #2a2e39;
        }
        .close {
            float: right;
            font-size: 24px;
            cursor: pointer;
            color: #787b86;
        }
        .countdown {
            font-family: monospace;
            font-size: 20px;
            font-weight: bold;
            color: #2962ff;
        }

        @media (max-width: 768px) {
            .stats-grid { grid-template-columns: repeat(3, 1fr); }
            .stat-value { font-size: 16px; }
            .btn { padding: 6px 12px; font-size: 11px; }
            th, td { font-size: 10px; padding: 6px; }
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>
                🤖 ByBit Trading Bot 
                <span id="status-badge" class="status-badge status-stopped">⏸️ Зупинено</span>
            </h1>
            <div style="margin-top: 8px; font-size: 11px; color: #787b86;">
                📊 Технічний аналіз | RSI + MACD + MA(20) + MA(50) | 📈 Професійний графік
            </div>
        </div>

        <div class="stats-grid">
            <div class="stat-card"><h3>💰 Баланс</h3><div class="stat-value" id="balance">$0</div></div>
            <div class="stat-card"><h3>📈 PnL</h3><div class="stat-value" id="pnl">$0</div></div>
            <div class="stat-card"><h3>🎯 Win Rate</h3><div class="stat-value" id="winrate">0%</div></div>
            <div class="stat-card"><h3>📊 Угоди</h3><div class="stat-value" id="trades">0</div></div>
            <div class="stat-card"><h3>📈 Відкриті</h3><div class="stat-value" id="open-positions">0</div></div>
            <div class="stat-card"><h3>⏱️ Аналіз</h3><div class="countdown" id="countdown">--:--</div></div>
        </div>

        <div class="controls">
            <button class="btn btn-start" id="main-action-btn" onclick="toggleStrategy()">▶️ Запустити</button>
            <button class="btn btn-reset" onclick="resetSimulation()">🔄 Скинути симуляцію</button>
        </div>

        <div class="chart-container">
            <div class="chart-header">
                <h3>📊 Ринковий аналіз</h3>
                <div class="chart-controls">
                    <select id="symbol-select" class="chart-select" onchange="loadChart()">
                        <option value="BTCUSDT">₿ BTC/USDT</option>
                        <option value="ETHUSDT">⟠ ETH/USDT</option>
                        <option value="SOLUSDT">◎ SOL/USDT</option>
                        <option value="BNBUSDT">🟡 BNB/USDT</option>
                        <option value="ADAUSDT">🔷 ADA/USDT</option>
                    </select>
                    <div class="timeframe-buttons" id="timeframe-buttons">
                        <button class="timeframe-btn" data-tf="1">1м</button>
                        <button class="timeframe-btn" data-tf="5">5м</button>
                        <button class="timeframe-btn" data-tf="15">15м</button>
                        <button class="timeframe-btn active" data-tf="60">1г</button>
                        <button class="timeframe-btn" data-tf="240">4г</button>
                        <button class="timeframe-btn" data-tf="D">1д</button>
                    </div>
                </div>
            </div>
            <div id="chart"></div>
        </div>

        <div class="table-container">
            <h3>📋 Угоди</h3>
            <table style="width:100%"><thead><tr><th>ID</th><th>Час</th><th>Тип</th><th>Пара</th><th>Статус</th><th>Ціна</th><th>К-ть</th><th>PnL</th><th>Дії</th></tr></thead>
            <tbody id="orders-body"></tbody></table>
        </div>

        <div class="table-container">
            <h3>🔮 Прогнози</h3>
            <table style="width:100%"><thead><tr><th>ID</th><th>Пара</th><th>Сигнал</th><th>Ціль</th><th>Перевірка</th><th>Статус</th></tr></thead>
            <tbody id="predictions-body"></tbody></table>
        </div>
    </div>

    <div id="modal" class="modal">
        <div class="modal-content">
            <span class="close" onclick="closeModal()">&times;</span>
            <h3>📈 Деталі угоди</h3>
            <div id="order-chart" style="height: 400px;"></div>
        </div>
    </div>

    <script src="https://code.jquery.com/jquery-3.6.0.min.js"></script>
    <script>
        let chart = null;
        let orderChart = null;
        let currentTimeframe = '60';
        let countdownInterval = null;
        let analysisSecondsLeft = 60;
        let isBotRunning = false;

        $(document).ready(function() {
            loadStats();
            loadOrders();
            loadPredictions();
            loadBotStatus();
            startCountdown();

            setInterval(function() {
                loadStats();
                loadOrders();
                loadPredictions();
                loadBotStatus();
            }, 5000);

            $('.timeframe-btn').click(function() {
                $('.timeframe-btn').removeClass('active');
                $(this).addClass('active');
                currentTimeframe = $(this).data('tf');
                loadChart();
            });
        });

        function closeModal() { $('#modal').hide(); }

        function loadBotStatus() {
            fetch('/api/bot-status').then(r=>r.json()).then(d=>{
                isBotRunning = d.is_running;
                $('#main-action-btn').html(isBotRunning ? '⏸️ Зупинити' : '▶️ Запустити');
                $('#main-action-btn').removeClass(isBotRunning ? 'btn-start' : 'btn-stop').addClass(isBotRunning ? 'btn-stop' : 'btn-start');
                $('#status-badge').html(isBotRunning ? '🟢 АКТИВНИЙ' : '⏸️ Зупинено');
                $('#status-badge').removeClass(isBotRunning ? 'status-stopped' : 'status-running').addClass(isBotRunning ? 'status-running' : 'status-stopped');

                if(isBotRunning) {
                    analysisSecondsLeft = 60;
                }
            });
        }

        function startCountdown() {
            countdownInterval = setInterval(() => {
                if(isBotRunning && analysisSecondsLeft > 0) {
                    analysisSecondsLeft--;
                    const mins = Math.floor(analysisSecondsLeft / 60);
                    const secs = analysisSecondsLeft % 60;
                    $('#countdown').text(`${mins}:${secs.toString().padStart(2,'0')}`);
                } else if(isBotRunning && analysisSecondsLeft === 0) {
                    analysisSecondsLeft = 60;
                    loadChart();
                } else if(!isBotRunning) {
                    $('#countdown').text('--:--');
                }
            }, 1000);
        }

        function toggleStrategy() {
            const action = isBotRunning ? '/api/strategy/stop' : '/api/strategy/start';
            fetch(action, {method:'POST'}).then(r=>r.json()).then(d=>{ 
                if(d.success) {
                    loadBotStatus();
                    if(!isBotRunning) analysisSecondsLeft = 60;
                } else alert('Помилка'); 
            });
        }

        function loadStats() {
            fetch('/api/stats').then(r=>r.json()).then(d=>{
                $('#balance').text('$'+d.balance.toFixed(2));
                $('#pnl').text('$'+d.total_pnl.toFixed(2)).css('color', d.total_pnl>=0?'#00bcd4':'#ff5252');
                $('#winrate').text(d.win_rate.toFixed(1)+'%');
                $('#trades').text(d.total_trades);
                $('#open-positions').text(d.open_positions);
            });
        }

        function loadOrders() {
            fetch('/api/orders/all').then(r=>r.json()).then(d=>{
                let html='';
                d.orders.slice().reverse().slice(0, 50).forEach(o=>{
                    html+=`<tr>
                        <td>${o.id}</td>
                        <td>${new Date(o.opened_at).toLocaleString()}</td>
                        <td><span class="badge badge-${o.side}">${o.side.toUpperCase()}</span></td>
                        <td>${o.symbol}</td>
                        <td><span class="badge badge-${o.status}">${o.status}</span></td>
                        <td>$${o.price.toFixed(2)}</td>
                        <td>${o.quantity}</td>
                        <td class="${o.pnl>=0?'positive':'negative'}">$${o.pnl.toFixed(2)}</td>
                        <td>
                            <button class="btn-sm btn-info" onclick="showOrderChart(${o.id})">📊</button>
                            ${o.status==='open' ? `<button class="btn-sm btn-danger-sm" onclick="closeOrder(${o.id})">❌</button>` : ''}
                          </td>
                    </tr>`;
                });
                $('#orders-body').html(html);
            });
        }

        function loadPredictions() {
            fetch('/api/predictions').then(r=>r.json()).then(d=>{
                let html='';
                d.predictions.slice().reverse().slice(0, 30).forEach(p=>{
                    let badgeClass = p.status==='pending'?'badge-pending':(p.status==='success'?'badge-success':'badge-failed');
                    let badgeText = p.status==='pending'?'⏳ Очікується':(p.status==='success'?'✅ Справдився':'❌ Не справдився');
                    html+=`<tr>
                        <td>${p.id}</td>
                        <td>${p.symbol}</td>
                        <td><span class="badge badge-${p.direction}">${p.direction.toUpperCase()}</span></td>
                        <td>$${p.target_price.toFixed(2)}</td>
                        <td>${new Date(p.check_at).toLocaleString()}</td>
                        <td><span class="badge ${badgeClass}">${badgeText}</span></td>
                    </tr>`;
                });
                $('#predictions-body').html(html);
            });
        }

        function loadChart() {
            const symbol = $('#symbol-select').val();
            fetch(`/api/candlestick-data/${symbol}?timeframe=${currentTimeframe}`)
            .then(r=>r.json())
            .then(d=>{
                if(d.data && d.data.length > 0 && window.LightweightCharts) {
                    if(chart) {
                        chart.removeSeries();
                        chart = null;
                    }

                    chart = LightweightCharts.createChart(document.getElementById('chart'), {
                        width: document.getElementById('chart').clientWidth,
                        height: 500,
                        layout: { backgroundColor: '#1e222d', textColor: '#d1d4dc', fontSize: 11 },
                        grid: { vertLines: { color: '#2a2e39' }, horzLines: { color: '#2a2e39' } },
                        crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
                        priceScale: { borderColor: '#2a2e39' },
                        timeScale: { borderColor: '#2a2e39', timeVisible: true, secondsVisible: false }
                    });

                    // Свічковий графік
                    const candlestickSeries = chart.addCandlestickSeries({
                        upColor: '#00bcd4',
                        downColor: '#ff5252',
                        borderDownColor: '#ff5252',
                        borderUpColor: '#00bcd4',
                        wickDownColor: '#ff5252',
                        wickUpColor: '#00bcd4'
                    });

                    candlestickSeries.setData(d.data);

                    // Ковзні середні
                    if(d.ma20 && d.ma20.length) {
                        const ma20Data = d.ma20.filter(v => v !== null).map((v, i) => ({ time: d.data[i].time, value: v }));
                        if(ma20Data.length) {
                            const ma20Series = chart.addLineSeries({ color: '#2962ff', lineWidth: 1, title: 'MA(20)' });
                            ma20Series.setData(ma20Data);
                        }
                    }

                    if(d.ma50 && d.ma50.length) {
                        const ma50Data = d.ma50.filter(v => v !== null).map((v, i) => ({ time: d.data[i].time, value: v }));
                        if(ma50Data.length) {
                            const ma50Series = chart.addLineSeries({ color: '#ff9800', lineWidth: 1, title: 'MA(50)' });
                            ma50Series.setData(ma50Data);
                        }
                    }

                    // Сигнали
                    if(d.signals && d.signals.length) {
                        const markers = d.signals.map(s => ({
                            time: s.time,
                            position: s.type === 'buy' ? 'belowBar' : 'aboveBar',
                            color: s.type === 'buy' ? '#00bcd4' : '#ff5252',
                            shape: s.type === 'buy' ? 'arrowUp' : 'arrowDown',
                            text: s.type === 'buy' ? 'BUY' : 'SELL'
                        }));
                        candlestickSeries.setMarkers(markers);
                    }

                    chart.timeScale().fitContent();
                }
            })
            .catch(e=>console.error('Chart error:', e));
        }

        function showOrderChart(orderId) {
            fetch(`/api/order-chart-data/${orderId}`).then(r=>r.json()).then(d=>{
                if(d.prices && d.prices.length && window.LightweightCharts) {
                    if(orderChart) {
                        orderChart.removeSeries();
                        orderChart = null;
                    }

                    orderChart = LightweightCharts.createChart(document.getElementById('order-chart'), {
                        width: document.getElementById('order-chart').clientWidth,
                        height: 400,
                        layout: { backgroundColor: '#1e222d', textColor: '#d1d4dc', fontSize: 11 },
                        grid: { vertLines: { color: '#2a2e39' }, horzLines: { color: '#2a2e39' } },
                        timeScale: { borderColor: '#2a2e39', timeVisible: true }
                    });

                    const lineSeries = orderChart.addLineSeries({ color: '#2962ff', lineWidth: 2 });
                    const timeData = d.times.map((t, i) => ({ time: t.replace(' ', 'T'), value: d.prices[i] }));
                    lineSeries.setData(timeData);

                    // Маркери входу/виходу
                    const markers = [];
                    if(d.entryTime) markers.push({ time: d.entryTime.replace(' ', 'T'), position: 'belowBar', color: '#00bcd4', shape: 'arrowUp', text: 'Entry' });
                    if(d.exitTime) markers.push({ time: d.exitTime.replace(' ', 'T'), position: 'aboveBar', color: '#ff5252', shape: 'arrowDown', text: 'Exit' });
                    if(markers.length) lineSeries.setMarkers(markers);

                    orderChart.timeScale().fitContent();
                    $('#modal').show();
                }
            });
        }

        function closeOrder(id) {
            if(confirm('❓ Закрити угоду з поточним PnL?')) {
                fetch(`/api/order/close/${id}`,{method:'POST'}).then(r=>r.json()).then(d=>{
                    if(d.success) { alert('✅ Угоду закрито!'); loadOrders(); loadStats(); }
                    else alert('❌ Помилка: ' + d.error);
                });
            }
        }

        function resetSimulation() {
            if(confirm('⚠️ Скинути симуляцію до $100?')) {
                fetch('/api/simulation/reset',{method:'POST'}).then(r=>r.json()).then(d=>{
                    if(d.success) { alert('✅ Скинуто!'); loadOrders(); loadStats(); }
                    else alert('❌ Помилка');
                });
            }
        }

        $(window).resize(function() {
            if(chart) chart.resize(document.getElementById('chart').clientWidth, 500);
            if(orderChart) orderChart.resize(document.getElementById('order-chart').clientWidth, 400);
        });
    </script>
</body>
</html>
"""


# ==================== API ENDPOINTS ====================

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, username: str = Depends(authenticate)):
    return HTMLResponse(html_content)


@app.get("/api/bot-status")
async def get_bot_status(username: str = Depends(authenticate)):
    state = await db.get_strategy_state()
    return {"is_running": state.is_running, "mode": state.mode}


@app.get("/api/stats")
async def get_stats(username: str = Depends(authenticate)):
    balance = await db.get_simulation_balance()
    orders = await db.get_all_orders_sim()
    total_pnl = sum(o.pnl for o in orders if o.status.value == 'closed')
    closed = [o for o in orders if o.status.value == 'closed']
    win_rate = (len([o for o in closed if o.pnl > 0]) / len(closed) * 100) if closed else 0
    open_positions = len([o for o in orders if o.status.value == 'open'])
    return {"balance": balance, "total_pnl": total_pnl, "win_rate": win_rate, "total_trades": len(orders),
            "open_positions": open_positions}


@app.get("/api/orders/all")
async def get_all_orders(username: str = Depends(authenticate)):
    orders = await db.get_all_orders_sim()
    return {"orders": [
        {"id": o.id, "symbol": o.symbol, "side": o.side.value, "status": o.status.value, "price": o.price,
         "quantity": o.quantity, "pnl": o.pnl, "opened_at": o.opened_at.isoformat()} for o in orders]}


@app.get("/api/predictions")
async def get_predictions(username: str = Depends(authenticate)):
    predictions = await db.get_all_predictions()
    return {"predictions": [{"id": p.id, "symbol": p.symbol, "timeframe": p.timeframe, "direction": p.direction.value,
                             "target_price": p.target_price, "check_at": p.check_at.isoformat(),
                             "status": p.status.value} for p in predictions[-50:]]}


@app.get("/api/candlestick-data/{symbol}")
async def get_candlestick_data(symbol: str, timeframe: str = "60", username: str = Depends(authenticate)):
    try:
        from app.bybit_client.real_client import bybit_real
        from app.strategy.indicators import ta

        klines = await bybit_real.get_klines(symbol, timeframe, 100)

        if not klines:
            return {"data": []}

        # Форматуємо дані для Lightweight Charts
        data = []
        for k in klines:
            data.append({
                "time": datetime.fromtimestamp(k['timestamp'] / 1000).strftime('%Y-%m-%d'),
                "open": k['open'],
                "high": k['high'],
                "low": k['low'],
                "close": k['close']
            })

        # Розрахунок ковзних середніх
        closes = [k['close'] for k in klines]
        ma20 = [None] * len(closes)
        ma50 = [None] * len(closes)

        for i in range(20, len(closes)):
            ma20[i] = ta.calculate_sma(closes[i - 20:i], 20)
        for i in range(50, len(closes)):
            ma50[i] = ta.calculate_sma(closes[i - 50:i], 50)

        # Пошук сигналів
        signals = []
        if len(closes) > 30:
            rsi = ta.calculate_rsi(closes[-30:])
            macd, sig, hist = ta.calculate_macd(closes[-30:])
            last_candle = data[-1]

            if rsi < 30 and macd > sig:
                signals.append({"type": "buy", "time": last_candle['time'], "price": last_candle['close']})
            elif rsi > 70 and macd < sig:
                signals.append({"type": "sell", "time": last_candle['time'], "price": last_candle['close']})

        return {"data": data, "ma20": ma20, "ma50": ma50, "signals": signals}

    except Exception as e:
        logger.error(f"Candlestick data error: {e}")
        return {"data": []}


@app.get("/api/order-chart-data/{order_id}")
async def get_order_chart_data(order_id: int, username: str = Depends(authenticate)):
    try:
        from app.bybit_client.real_client import bybit_real
        orders = await db.get_all_orders_sim()
        order = next((o for o in orders if o.id == order_id), None)
        if not order:
            return {"prices": [], "times": []}

        klines = await bybit_real.get_klines(order.symbol, "60", 50)
        if not klines:
            return {"prices": [], "times": []}

        prices = [k['close'] for k in klines]
        times = [datetime.fromtimestamp(k['timestamp'] / 1000).strftime('%Y-%m-%d %H:%M') for k in klines]

        result = {"prices": prices, "times": times, "entryPrice": order.price,
                  "entryTime": order.opened_at.strftime('%Y-%m-%d %H:%M')}

        if order.status.value == 'closed' and order.closed_at:
            exit_price = order.price + (order.pnl / order.quantity) if order.quantity > 0 else order.price
            result["exitPrice"] = exit_price
            result["exitTime"] = order.closed_at.strftime('%Y-%m-%d %H:%M')

        return result
    except Exception as e:
        return {"prices": [], "times": []}


@app.post("/api/order/close/{order_id}")
async def close_order(order_id: int, username: str = Depends(authenticate)):
    try:
        from app.bybit_client.simulation_client import simulation_client
        from app.bybit_client.real_client import bybit_real

        orders = await db.get_open_orders_sim()
        order = next((o for o in orders if o.id == order_id), None)
        if not order:
            return {"success": False, "error": "Order not found"}

        current_price = await bybit_real.get_current_price(order.symbol)
        if not current_price:
            return {"success": False, "error": "Cannot get price"}

        success = await simulation_client.close_order(order_id, current_price)
        return {"success": success}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.post("/api/strategy/start")
async def start_strategy_endpoint(username: str = Depends(authenticate)):
    success = await strategy_controller.start()
    return {"success": success}


@app.post("/api/strategy/stop")
async def stop_strategy_endpoint(username: str = Depends(authenticate)):
    success = await strategy_controller.stop()
    return {"success": success}


@app.post("/api/simulation/reset")
async def reset_simulation_endpoint(username: str = Depends(authenticate)):
    success = await strategy_controller.reset_simulation()
    return {"success": success}