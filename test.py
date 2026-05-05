# check_trades.py
from db.database import SessionLocal
from db.models import Trade, OrderStatus

db = SessionLocal()

print("=" * 50)
print("ВІДКРИТІ УГОДИ:")
open_trades = db.query(Trade).filter(Trade.status == OrderStatus.PENDING).all()
for t in open_trades:
    print(f"  ID={t.id}, Pair={t.pair}, Side={t.side}, Entry={t.entry_price}")

print(f"\nВсього відкритих: {len(open_trades)}")

print("\n" + "=" * 50)
print("ОСТАННІ 5 УГОД:")
recent = db.query(Trade).order_by(Trade.id.desc()).limit(5).all()
for t in recent:
    print(f"  ID={t.id}, Pair={t.pair}, Status={t.status}, PnL={t.pnl}")

db.close()