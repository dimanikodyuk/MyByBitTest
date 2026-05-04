import sqlite3
conn = sqlite3.connect('data/trading.db')
cursor = conn.cursor()

# Додаємо відсутні колонки
try:
    cursor.execute('ALTER TABLE forecasts ADD COLUMN position_quantity REAL DEFAULT 0')
    print('✓ Додано колонку position_quantity')
except: print('position_quantity вже існує')

try:
    cursor.execute('ALTER TABLE forecasts ADD COLUMN position_usdt REAL DEFAULT 0')
    print('✓ Додано колонку position_usdt')
except: print('position_usdt вже існує')

try:
    cursor.execute('ALTER TABLE forecasts ADD COLUMN current_pnl REAL DEFAULT 0')
    print('✓ Додано колонку current_pnl')
except: print('current_pnl вже існує')

try:
    cursor.execute('ALTER TABLE forecasts ADD COLUMN closed_pnl REAL DEFAULT 0')
    print('✓ Додано колонку closed_pnl')
except: print('closed_pnl вже існує')

conn.commit()
conn.close()
print('✅ Всі колонки додано успішно!')

