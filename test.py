# remove_test_coin.py
from db.database import SessionLocal
from db.models import ListingTrade, ListingBalance

db = SessionLocal()

# Видаляємо тестові угоди
deleted = db.query(ListingTrade).filter(
    (ListingTrade.symbol == 'TEST') | (ListingTrade.pair == 'TESTUSDT')
).delete()
print(f"Видалено {deleted} тестових угод")

# Скидаємо баланс (опціонально)
balance = db.query(ListingBalance).first()
if balance:
    balance.amount = 100.0
    balance.total_pnl = 0
    balance.total_trades = 0
    balance.win_trades = 0
    print("Баланс скинуто до $100")

db.commit()
db.close()
print("Готово!")