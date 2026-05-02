// Global variables
let currentOrderChart = null;
let currentChartType = 'candlestick';

// Initialize
$(document).ready(function() {
    loadStats();
    loadOrders();
    loadPredictions();
    loadChart();

    // Auto-refresh every 5 seconds
    setInterval(function() {
        loadStats();
        loadOrders();
        loadPredictions();
    }, 5000);
});

// Load statistics
async function loadStats() {
    try {
        const response = await fetch('/api/stats');
        const data = await response.json();

        $('#total-balance').text(`$${data.simulation_balance.toFixed(2)}`);
        $('#total-pnl').text(`$${data.total_pnl.toFixed(2)}`);
        $('#total-pnl').css('color', data.total_pnl >= 0 ? '#27ae60' : '#e74c3c');
        $('#win-rate').text(`${data.win_rate.toFixed(1)}%`);
        $('#total-trades').text(data.total_trades);

        const pnlChange = $('#total-pnl');
        if (data.total_pnl >= 0) {
            pnlChange.html(`📈 +$${data.total_pnl.toFixed(2)}`);
        } else {
            pnlChange.html(`📉 $${data.total_pnl.toFixed(2)}`);
        }
    } catch(e) {
        console.error('Error loading stats:', e);
    }
}

// Load orders
async function loadOrders() {
    try {
        const response = await fetch('/api/orders/all');
        const data = await response.json();

        if(data.orders.length === 0) {
            $('#orders-table-body').html('<tr><td colspan="9" style="text-align: center;">Немає угод</td></tr>');
            return;
        }

        let html = '';
        for(let order of data.orders) {
            const pnlClass = order.pnl >= 0 ? 'positive' : 'negative';
            html += `
                <tr>
                    <td>${order.id}</td>
                    <td>${new Date(order.opened_at).toLocaleString()}</td>
                    <td><span class="badge badge-${order.side}">${order.side.toUpperCase()}</span></td>
                    <td>${order.symbol}</td>
                    <td><span class="badge badge-${order.status}">${order.status}</span></td>
                    <td>$${order.price.toFixed(2)}</td>
                    <td>${order.quantity}</td>
                    <td class="${pnlClass}">$${order.pnl.toFixed(2)}</td>
                    <td>
                        <button class="btn btn-primary btn-sm" onclick="showOrderChart(${order.id})">📊 Графік</button>
                        ${order.status === 'open' ? `<button class="btn btn-danger btn-sm" onclick="closeOrder(${order.id})">❌ Закрити</button>` : ''}
                    </td>
                </tr>
            `;
        }
        $('#orders-table-body').html(html);
    } catch(e) {
        console.error('Error loading orders:', e);
    }
}

// Load predictions
async function loadPredictions() {
    try {
        const response = await fetch('/api/predictions');
        const data = await response.json();

        if(data.predictions.length === 0) {
            $('#predictions-table-body').html('<tr><td colspan="7" style="text-align: center;">Немає прогнозів</td></tr>');
            return;
        }

        let html = '';
        for(let pred of data.predictions) {
            let statusBadge = '';
            if(pred.status === 'pending') statusBadge = '<span class="badge badge-open">Очікується</span>';
            else if(pred.status === 'success') statusBadge = '<span class="badge badge-buy">✅ Справдився</span>';
            else statusBadge = '<span class="badge badge-sell">❌ Не справдився</span>';

            html += `
                <tr>
                    <td>${pred.id}</td>
                    <td>${pred.symbol}</td>
                    <td>${pred.timeframe}</td>
                    <td><span class="badge badge-${pred.direction}">${pred.direction.toUpperCase()}</span></td>
                    <td>$${pred.target_price.toFixed(2)}</td>
                    <td>${new Date(pred.check_at).toLocaleString()}</td>
                    <td>${statusBadge}</td>
                </tr>
            `;
        }
        $('#predictions-table-body').html(html);
    } catch(e) {
        console.error('Error loading predictions:', e);
    }
}

// Load main chart
async function loadChart() {
    const symbol = $('#chart-symbol').val();
    const response = await fetch(`/api/chart/${symbol}`);
    const data = await response.json();

    if(data.chart) {
        Plotly.newPlot('price-chart', JSON.parse(data.chart).data, JSON.parse(data.chart).layout);
    }
}

// Show order chart modal
async function showOrderChart(orderId) {
    const response = await fetch(`/api/order/chart/${orderId}`);
    const data = await response.json();

    if(data.chart) {
        $('#order-chart-container').html('');
        Plotly.newPlot('order-chart-container', JSON.parse(data.chart).data, JSON.parse(data.chart).layout);
        $('#orderChartModal').show();
    }
}

// Close order
async function closeOrder(orderId) {
    if(confirm('Ви впевнені, що хочете закрити цю угоду?')) {
        const response = await fetch(`/api/order/close/${orderId}`, { method: 'POST' });
        const data = await response.json();

        if(data.success) {
            alert('Угоду закрито!');
            loadOrders();
            loadStats();
        } else {
            alert('Помилка закриття угоди: ' + data.error);
        }
    }
}

// Start strategy
async function startStrategy() {
    const response = await fetch('/api/strategy/start', { method: 'POST' });
    const data = await response.json();
    if(data.success) {
        alert('Стратегію запущено!');
        loadStats();
    } else {
        alert('Помилка запуску');
    }
}

// Stop strategy
async function stopStrategy() {
    const response = await fetch('/api/strategy/stop', { method: 'POST' });
    const data = await response.json();
    if(data.success) {
        alert('Стратегію зупинено!');
        loadStats();
    } else {
        alert('Помилка зупинки');
    }
}

// Reset simulation
async function resetSimulation() {
    if(confirm('Скинути симуляцію до $100? Всі угоди будуть видалені.')) {
        const response = await fetch('/api/simulation/reset', { method: 'POST' });
        const data = await response.json();
        if(data.success) {
            alert('Симуляцію скинуто!');
            loadStats();
            loadOrders();
        } else {
            alert('Помилка скидання');
        }
    }
}

// Close modal
$('.close, .modal').click(function(e) {
    if(e.target === this) {
        $('#orderChartModal').hide();
    }
});