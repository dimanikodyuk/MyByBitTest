# check_db.py
from db.database import SessionLocal
from db.models import Balance
from sqlalchemy import inspect, text

db = SessionLocal()

print("=" * 60)
print("ДІАГНОСТИКА БАЗИ ДАНИХ")
print("=" * 60)

# 1. Перевіряємо структуру таблиці balances
print("\n1. СТРУКТУРА ТАБЛИЦІ balances:")
inspector = inspect(db.bind)
columns = inspector.get_columns('balances')
for col in columns:
    print(f"   - {col['name']}: {col['type']}")

# 2. Дивимось всі записи в balances
print("\n2. ВСІ ЗАПИСИ В balances:")
balances = db.query(Balance).all()
if balances:
    for b in balances:
        print(f"   ID: {b.id}, Asset: {b.asset}, Amount: {b.amount}, is_paper: {b.is_paper}")
else:
    print("   Немає записів!")

# 3. Перевіряємо чи є запис для USDT
print("\n3. ПЕРЕВІРКА USDT BALANCE:")
usdt_balance = db.query(Balance).filter(
    Balance.asset == "USDT",
    Balance.is_paper == 1
).first()
if usdt_balance:
    print(f"   ✅ Знайдено: {usdt_balance.amount}")
else:
    print("   ❌ НЕМАЄ запису для USDT paper balance!")

    # Спробуємо створити
    print("\n   Створюємо новий запис...")
    new_balance = Balance(asset="USDT", amount=100.0, is_paper=1)
    db.add(new_balance)
    db.commit()
    print(f"   ✅ Створено: 100.0 USDT")

# 4. Перевіряємо raw SQL запит
print("\n4. RAW SQL ПЕРЕВІРКА:")
try:
    result = db.execute(text("SELECT * FROM balances")).fetchall()
    for row in result:
        print(f"   {row}")
except Exception as e:
    print(f"   Помилка: {e}")

db.close()

print("\n" + "=" * 60)
print("Перевірте вміст файлу: data/trading.db")
print("=" * 60)