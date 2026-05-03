import asyncio
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.enums import ParseMode  # ← ЗМІНЕНО
from datetime import datetime
from typing import Optional
from utils.config_loader import config
from utils.logger import logger
from db.operations import DatabaseOperations
from db.database import SessionLocal
from core.paper_engine import PaperEngine
from core.risk_manager import RiskManager


class TelegramBot:
    """Telegram бот для керування та моніторингу"""

    def __init__(self, paper_engine: PaperEngine = None, order_manager=None):
        self.token = config.telegram_token
        self.chat_id = config.telegram_chat_id
        self.bot: Optional[Bot] = None
        self.dispatcher: Optional[Dispatcher] = None
        self.paper_engine = paper_engine
        self.order_manager = order_manager
        self.running = False

    async def start(self):
        """Запуск бота"""
        if not self.token:
            logger.warning("Telegram token not configured")
            return

        self.bot = Bot(token=self.token)
        self.dispatcher = Dispatcher()

        # Реєстрація команд
        self.dispatcher.message.register(self.cmd_start, Command("start"))
        self.dispatcher.message.register(self.cmd_stop, Command("stop"))
        self.dispatcher.message.register(self.cmd_status, Command("status"))
        self.dispatcher.message.register(self.cmd_balance, Command("balance"))
        self.dispatcher.message.register(self.cmd_trades, Command("trades"))
        self.dispatcher.message.register(self.cmd_mode, Command("mode"))
        self.dispatcher.message.register(self.cmd_reset, Command("reset"))
        self.dispatcher.message.register(self.cmd_stats, Command("stats"))
        self.dispatcher.message.register(self.cmd_help, Command("help"))
        self.dispatcher.message.register(self.cmd_close_all, Command("close_all"))

        self.running = True

        # Запуск polling
        asyncio.create_task(self._polling())
        logger.info("Telegram bot started")

        # Відправка привітання
        await self.send_message("🤖 *AutoTrading Bot Started*\n\nРежим: Paper Trading\nКоманди: /help",
                                parse_mode=ParseMode.MARKDOWN)

    async def _polling(self):
        """Polling для отримання повідомлень"""
        try:
            await self.dispatcher.start_polling(self.bot)
        except Exception as e:
            logger.error(f"Telegram polling error: {e}")
            self.running = False

    async def stop(self):
        """Зупинка бота"""
        if self.bot:
            await self.bot.session.close()
        self.running = False
        logger.info("Telegram bot stopped")

    async def send_message(self, text: str, parse_mode: ParseMode = None):
        """Відправка повідомлення адміну"""
        if not self.bot or not self.chat_id:
            return

        try:
            await self.bot.send_message(
                chat_id=self.chat_id,
                text=text,
                parse_mode=parse_mode
            )
        except Exception as e:
            logger.error(f"Failed to send telegram message: {e}")

    async def send_trade_notification(self, trade_data: dict):
        """Сповіщення про відкриття угоди"""
        emoji = "🟢" if trade_data['side'] == "BUY" else "🔴"
        text = f"""{emoji} *NEW TRADE OPENED*

Pair: `{trade_data['pair']}`
Side: *{trade_data['side']}*
Quantity: {trade_data['quantity']}
Entry Price: ${trade_data['entry_price']:.2f}
TP: ${trade_data.get('tp', 0):.2f}
SL: ${trade_data.get('sl', 0):.2f}
Balance: ${trade_data.get('balance', 0):.2f}
Time: {datetime.now().strftime('%H:%M:%S')}"""

        await self.send_message(text, parse_mode=ParseMode.MARKDOWN)


    async def send_close_notification(self, trade_data: dict):
        """Сповіщення про закриття угоди"""
        emoji = "✅" if trade_data['pnl'] > 0 else "❌"
        sign = "+" if trade_data['pnl'] > 0 else ""

        text = f"""{emoji} *TRADE CLOSED*

Pair: `{trade_data['pair']}`
Side: {trade_data['side']}
Entry: ${trade_data['entry_price']:.2f}
Exit: ${trade_data['exit_price']:.2f}
PnL: {sign}{trade_data['pnl']:.2f} USDT ({sign}{trade_data['pnl_percent']:.2f}%)
Reason: {trade_data.get('reason', 'TP/SL')}
Balance: ${trade_data.get('balance', 0):.2f}"""

        await self.send_message(text, parse_mode=ParseMode.MARKDOWN)

    async def send_error(self, error_msg: str):
        """Сповіщення про помилку"""
        text = f"⚠️ *ERROR*\n\n`{error_msg}`"
        await self.send_message(text, parse_mode=ParseMode.MARKDOWN)

    async def send_daily_report(self, stats: dict):
        """Щоденний звіт"""
        text = f"""📊 *DAILY REPORT*

Total Trades: {stats['total_trades']}
Win Rate: {stats['win_rate']:.1f}%
Total PnL: {stats['total_pnl']:.2f} USDT
Best Trade: {stats.get('best_trade', 0):.2f}
Worst Trade: {stats.get('worst_trade', 0):.2f}
Current Balance: ${stats['balance']:.2f}

Status: {'🟢 Active' if not stats.get('stopped') else '🔴 Stopped'}"""

        await self.send_message(text, parse_mode=ParseMode.MARKDOWN)

    # === COMMAND HANDLERS ===

    async def cmd_start(self, message: types.Message):
        """/start - запуск торгівлі"""
        if self.order_manager:
            self.order_manager.running = True
            await message.reply("✅ Trading started\n\nРежим: Paper Trading\nБаланс: $" +
                                str(self.order_manager.db.get_balance("USDT", True)))
        else:
            await message.reply("✅ Bot is running\n\nUse /help for commands")

    async def cmd_stop(self, message: types.Message):
        """/stop - аварійна зупинка"""
        if self.order_manager:
            self.order_manager.running = False
            await message.reply("🛑 Emergency stop activated\n\nAll trading halted")

            # Закриваємо всі відкриті позиції
            open_trades = self.order_manager.db.get_open_trades(is_paper=True)
            if open_trades:
                await message.reply(f"⚠️ {len(open_trades)} positions are still open\nUse /close_all to close them")
        else:
            await message.reply("🛑 Bot stopped")

    async def cmd_close_all(self, message: types.Message):
        """/close_all - закрити всі відкриті позиції"""
        if not self.order_manager:
            await message.reply("Bot not initialized")
            return

        open_trades = self.order_manager.db.get_open_trades(is_paper=True)
        if not open_trades:
            await message.reply("No open positions")
            return

        await message.reply(f"Closing {len(open_trades)} positions...")

        current_price = self.order_manager.exchange.get_current_price("BTCUSDT")
        for trade in open_trades:
            result = self.order_manager.paper_engine.execute_sell(trade.id, current_price)
            if result:
                await message.reply(f"✅ Closed {trade.pair} | PnL: {result['pnl']:.2f} USDT")

        await message.reply("All positions closed")

    async def cmd_status(self, message: types.Message):
        """/status - статус бота"""
        if not self.order_manager:
            await message.reply("🤖 Bot is running")
            return

        db = self.order_manager.db
        balance = db.get_balance("USDT", True)
        open_trades = len(db.get_open_trades(is_paper=True))

        risk_mgr = self.order_manager.risk_manager
        daily_stats = risk_mgr.get_daily_stats()

        text = f"""📊 *BOT STATUS*

Mode: `Paper Trading`
Status: {'🟢 RUNNING' if self.order_manager.running else '🔴 STOPPED'}

💰 Balance: ${balance:.2f}
📈 Open Trades: {open_trades}/3
📉 Daily PnL: {daily_stats['daily_pnl']:.2f} USDT ({daily_stats['daily_pnl_percent']:.1f}%)
🎯 Daily Limit: {daily_stats['daily_limit']}%
⚠️ Limit Reached: {'Yes' if daily_stats['limit_reached'] else 'No'}

🔄 Last Update: {datetime.now().strftime('%H:%M:%S')}"""

        await message.reply(text, parse_mode=ParseMode.MARKDOWN)

    async def cmd_balance(self, message: types.Message):
        """/balance - поточний баланс"""
        if not self.order_manager:
            await message.reply("Bot not initialized")
            return

        db = self.order_manager.db
        balance = db.get_balance("USDT", True)

        # Отримуємо статистику
        stats = db.get_stats(is_paper=True)

        text = f"""💰 *PAPER BALANCE*

Current Balance: `${balance:.2f}`
Total PnL: `${stats['total_pnl']:.2f}`
Total Trades: {stats['total_trades']}
Win Rate: {stats['win_rate']:.1f}%

Commands:
/reset - Reset to $100
/close_all - Close all positions"""

        await message.reply(text, parse_mode=ParseMode.MARKDOWN)

    async def cmd_trades(self, message: types.Message):
        """/trades - історія угод"""
        if not self.order_manager:
            await message.reply("Bot not initialized")
            return

        db = self.order_manager.db
        trades = db.get_trades_history(limit=10, is_paper=True)

        if not trades:
            await message.reply("No trades yet")
            return

        text = "📜 *LAST 10 TRADES*\n\n"
        for trade in trades[:5]:  # Показуємо 5 для читабельності
            emoji = "✅" if trade.pnl > 0 else "❌" if trade.pnl < 0 else "⚪"
            sign = "+" if trade.pnl > 0 else ""
            text += f"{emoji} {trade.pair} | {trade.side.value}\n"
            text += f"   Entry: ${trade.entry_price:.0f} | Exit: ${trade.exit_price:.0f if trade.exit_price else 0:.0f}\n"
            text += f"   PnL: {sign}{trade.pnl:.2f} ({sign}{trade.pnl_percent:.1f}%)\n"
            text += f"   {trade.opened_at.strftime('%H:%M %d/%m')}\n\n"

        if len(trades) > 5:
            text += f"_...and {len(trades) - 5} more_"

        await message.reply(text, parse_mode=ParseMode.MARKDOWN)

    async def cmd_mode(self, message: types.Message):
        """/mode - поточний режим"""
        current_mode = config.bot_mode
        text = f"""⚙️ *TRADING MODE*

Current Mode: **{current_mode.upper()}**

Available modes:
- paper (Paper Trading - Virtual)
- real (Real Trading - Requires API)

To switch mode, edit .env file and restart bot"""

        await message.reply(text, parse_mode=ParseMode.MARKDOWN)

    async def cmd_reset(self, message: types.Message):
        """/reset - скидання paper балансу"""
        if not self.order_manager:
            await message.reply("Bot not initialized")
            return

        # Закриваємо всі відкриті позиції
        open_trades = self.order_manager.db.get_open_trades(is_paper=True)
        for trade in open_trades:
            await self.order_manager.paper_engine.execute_sell(trade.id, trade.entry_price)

        # Скидаємо баланс
        self.order_manager.paper_engine.reset()

        await message.reply("✅ Paper trading reset to $100\nAll positions closed")

    async def cmd_stats(self, message: types.Message):
        """/stats - детальна статистика"""
        if not self.order_manager:
            await message.reply("Bot not initialized")
            return

        db = self.order_manager.db
        stats = db.get_stats(is_paper=True)
        daily = self.order_manager.risk_manager.get_daily_stats()

        text = f"""📈 *TRADING STATISTICS*

*All Time:*
Total Trades: {stats['total_trades']}
Win Rate: {stats['win_rate']:.1f}%
Total PnL: ${stats['total_pnl']:.2f}
Avg PnL: ${stats['avg_pnl']:.2f}
Max Drawdown: ${stats['max_drawdown']:.2f}

*Today:*
Daily PnL: ${daily['daily_pnl']:.2f}
Daily PnL %: {daily['daily_pnl_percent']:.1f}%
Open Positions: {daily['open_trades']}
Daily Limit: {daily['daily_limit']}%"""

        await message.reply(text, parse_mode=ParseMode.MARKDOWN)

    async def cmd_help(self, message: types.Message):
        """/help - список команд"""
        text = """🤖 *AVAILABLE COMMANDS*

/start - Start trading
/stop - Emergency stop
/status - Bot status
/balance - Current balance
/trades - Trade history
/mode - Current mode
/reset - Reset paper balance
/stats - Trading statistics
/help - This message

*Auto Notifications:*
- New trade opened
- Trade closed
- Daily report
- Errors"""

        await message.reply(text, parse_mode=ParseMode.MARKDOWN)