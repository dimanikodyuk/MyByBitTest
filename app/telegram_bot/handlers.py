from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from app.trading.strategy_controller import strategy_controller
from app.database.db_manager import db
from app.config import config
from loguru import logger


class TelegramBot:
    def __init__(self):
        self.application = None
        self.is_running = False

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /start"""
        keyboard = [
            [InlineKeyboardButton("📊 Статус", callback_data="status")],
            [InlineKeyboardButton("💰 Баланс", callback_data="balance")],
            [InlineKeyboardButton("▶️ Старт", callback_data="start"),
             InlineKeyboardButton("⏸️ Стоп", callback_data="stop")],
            [InlineKeyboardButton("🔄 Скинути симуляцію", callback_data="reset")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            "🤖 *ByBit Trading Bot*\n\n"
            "Я бот для автоматичної торгівлі на ByBit\n"
            "Використовуйте кнопки нижче для керування:",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )

    async def button_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обробка натискань кнопок"""
        query = update.callback_query
        await query.answer()

        if query.data == "status":
            state = await db.get_strategy_state()
            balance = await db.get_simulation_balance()
            text = f"📊 *Статус бота*\n\n"
            text += f"Стан: {'🟢 Активний' if state.is_running else '🔴 Зупинено'}\n"
            text += f"Режим: {state.mode}\n"
            text += f"Баланс симуляції: ${balance:.2f}\n"
            text += f"Останній запуск: {state.last_run or 'Ніколи'}"
            await query.edit_message_text(text, parse_mode='Markdown')

        elif query.data == "balance":
            balance = await db.get_simulation_balance()
            text = f"💰 *Симуляція*\nБаланс: ${balance:.2f}\nПочатковий: ${config.SIMULATION_START_BALANCE}"
            await query.edit_message_text(text, parse_mode='Markdown')

        elif query.data == "start":
            success = await strategy_controller.start()
            text = "✅ Стратегію запущено!" if success else "❌ Помилка запуску"
            await query.edit_message_text(text)

        elif query.data == "stop":
            success = await strategy_controller.stop()
            text = "⏸️ Стратегію зупинено!" if success else "❌ Помилка зупинки"
            await query.edit_message_text(text)

        elif query.data == "reset":
            success = await strategy_controller.reset_simulation()
            text = "🔄 Симуляцію скинуто до $100!" if success else "❌ Помилка скидання"
            await query.edit_message_text(text)

    async def status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /status"""
        state = await db.get_strategy_state()
        balance = await db.get_simulation_balance()

        text = f"📊 *Статус бота*\n\n"
        text += f"Режим: {state.mode}\n"
        text += f"Стан: {'Активний' if state.is_running else 'Зупинено'}\n"
        text += f"Баланс симуляції: ${balance:.2f}\n"
        text += f"Моніторинг: {', '.join(config.MONITORED_SYMBOLS)}"

        await update.message.reply_text(text, parse_mode='Markdown')

    async def run(self):
        """Запуск Telegram бота"""
        if not config.TELEGRAM_BOT_TOKEN:
            logger.warning("Telegram bot token not configured, skipping...")
            return

        if self.is_running:
            logger.warning("Telegram bot already running")
            return

        try:
            self.application = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()

            # Реєстрація команд
            self.application.add_handler(CommandHandler("start", self.start_command))
            self.application.add_handler(CommandHandler("status", self.status_command))
            self.application.add_handler(CallbackQueryHandler(self.button_callback))

            # Запуск
            await self.application.initialize()
            await self.application.start()
            await self.application.updater.start_polling(allowed_updates=["message", "callback_query"])

            self.is_running = True
            logger.success("Telegram bot started")

        except Exception as e:
            logger.error(f"Failed to start Telegram bot: {e}")


telegram_bot = TelegramBot()