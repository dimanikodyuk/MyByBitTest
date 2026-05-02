from app.database.db_manager import db
from app.bybit_client.real_client import bybit_real
from loguru import logger
from datetime import datetime
from typing import List
import asyncio


class PredictionValidator:
    """Перевірка справдження прогнозів"""

    async def validate_all_pending(self):
        """Перевірка всіх очікуючих прогнозів"""
        predictions = await db.get_pending_predictions()

        for pred in predictions:
            if datetime.now() >= pred.check_at:
                await self.validate_prediction(pred)

    async def validate_prediction(self, prediction):
        """Перевірка конкретного прогнозу"""
        try:
            current_price = await bybit_real.get_current_price(prediction.symbol)

            if not current_price:
                logger.warning(f"Cannot validate prediction {prediction.id}: no price")
                return

            is_success = False

            if prediction.direction.value == 'buy':
                if current_price >= prediction.target_price:
                    is_success = True
            else:  # sell
                if current_price <= prediction.target_price:
                    is_success = True

            status = 'success' if is_success else 'failed'
            await db.update_prediction_status(prediction.id, status, current_price)

            await db.add_log(
                action=f"PREDICTION_VALIDATION",
                details=f"Prediction #{prediction.id}: {status}, target: ${prediction.target_price}, actual: ${current_price}",
                status=status
            )

            logger.info(f"Prediction #{prediction.id} validated: {status}")

        except Exception as e:
            logger.error(f"Error validating prediction {prediction.id}: {e}")


validator = PredictionValidator()