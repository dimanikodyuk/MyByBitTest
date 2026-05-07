# update_forecast_db.py - запустити один раз

from db.database import engine, SessionLocal
from sqlalchemy import text


def update_forecasts_table():
    db = SessionLocal()
    try:
        # Отримуємо список існуючих колонок
        result = db.execute(text("PRAGMA table_info(forecasts)")).fetchall()
        columns = [col[1] for col in result]

        # Додаємо нові колонки, якщо їх немає
        new_columns = [
            ("max_price_reached", "FLOAT"),
            ("min_price_reached", "FLOAT"),
            ("target_hit_time", "DATETIME"),
            ("hit_percentage", "FLOAT DEFAULT 0.0"),
            ("actual_profit_pct", "FLOAT DEFAULT 0.0"),
            ("quality_score", "FLOAT DEFAULT 0.0")
        ]

        for col_name, col_type in new_columns:
            if col_name not in columns:
                print(f"Додаємо колонку {col_name}...")
                db.execute(text(f"ALTER TABLE forecasts ADD COLUMN {col_name} {col_type}"))
                db.commit()
                print(f"✅ Колонку {col_name} додано")
            else:
                print(f"ℹ️ Колонка {col_name} вже існує")

        # Додаємо індекси
        db.execute(text("CREATE INDEX IF NOT EXISTS idx_forecasts_result ON forecasts(result)"))
        db.execute(text("CREATE INDEX IF NOT EXISTS idx_forecasts_quality ON forecasts(quality_score)"))
        db.commit()

        print("✅ Оновлення таблиці forecasts завершено!")

    except Exception as e:
        print(f"❌ Помилка: {e}")
        db.rollback()
    finally:
        db.close()


if __name__ == "__main__":
    update_forecasts_table()