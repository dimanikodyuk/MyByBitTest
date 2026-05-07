import asyncio
import traceback
from datetime import datetime
from typing import Optional

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command

from utils.config_loader import config
from utils.logger import logger
from core.paper_engine import PaperEngine

from aiogram.types import ReplyKeyboardMarkup, KeyboardButton

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

        try:
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
            await self.send_message("🤖 AutoTrading Bot Started\n\nРежим: Paper Trading\nКоманди: /help")
        except Exception as e:
            logger.error(f"Failed to start Telegram bot: {e}")

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

    async def send_message(self, text: str, parse_mode: str = "Markdown"):
        """Відправка повідомлення адміну з підтримкою форматування"""
        if not self.bot or not self.chat_id:
            return

        # Для Markdown потрібно трохи скоротити через службові символи
        if parse_mode == "Markdown" and len(text) > 4000:
            text = text[:3950] + "..."

        try:
            await self.bot.send_message(
                chat_id=self.chat_id,
                text=text,
                parse_mode=parse_mode if parse_mode else None
            )
        except Exception as e:
            # Якщо Markdown не пройшов — пробуємо без форматування
            if parse_mode == "Markdown":
                logger.warning(f"Markdown failed, sending without formatting: {e}")
                await self.bot.send_message(
                    chat_id=self.chat_id,
                    text=text,
                    parse_mode=None
                )
            else:
                logger.error(f"Failed to send: {e}")

    async def send_important_event(self, event_type: str, data: dict):
        """Відправка важливої події з різними форматами"""

        templates = {
            'new_forecast': (
                "🔮 *НОВИЙ ПРОГНОЗ*\n\n"
                "📊 {pair} | {signal_type}\n"
                "💰 Вхід: ${entry_price:.2f}\n"
                "🎯 Ціль: ${target_price:.2f}\n"
                "📈 Впевненість: {confidence}%\n"
                "💵 Позиція: ${position_usdt:.2f}"
            ),
            'forecast_hit': (
                "🎯 *ПРОГНОЗ ДОСЯГНУТО!*\n\n"
                "📊 {pair} | {signal_type}\n"
                "💰 Вхід: ${entry_price:.2f}\n"
                "🎯 Ціль: ${target_price:.2f}\n"
                "📈 Прибуток: +{profit_pct:.1f}%\n"
                "💵 PnL: ${pnl:.2f}"
            ),
            'daily_limit_hit': (
                "⚠️ *ДЕННИЙ ЛІМІТ ЗБИТКУ ДОСЯГНУТО!*\n\n"
                "📉 Денний PnL: ${daily_pnl:.2f}\n"
                "🎯 Ліміт: {limit}%\n"
                "🛑 Торгівля призупинена до завтра"
            ),
            'new_listing_trade': (
                "🆕 *НОВА МОНЕТА НА БІРЖІ!*\n\n"
                "🪙 Монета: {symbol}\n"
                "💰 Вхід: ${entry_price:.4f}\n"
                "💵 Позиція: ${position_usdt:.2f}\n"
                "🔥 Ліквідність: {liquidity:.1f}%\n"
                "⏰ Час: {time}"
            ),
            'news_trade': (
                "📰 *УГОДА ЗА НОВИНОЮ*\n\n"
                "📌 {title}\n"
                "📊 {pair} | {side}\n"
                "💰 Вхід: ${entry_price:.2f}\n"
                "🎭 Тональність: {sentiment:.1f}\n"
                "💵 Позиція: ${position_usdt:.2f}"
            ),
            'bot_start': (
                "✅ *БОТ ЗАПУЩЕНО*\n\n"
                "🤖 AutoTrading Bot v3.0\n"
                "📊 Режим: Paper Trading\n"
                "🌐 Web UI: http://localhost:8000"
            ),
            'bot_stop': (
                "🛑 *БОТ ЗУПИНЕНО*\n\n"
                "Торгівля призупинена\n"
                "Для запуску використовуйте /start"
            ),
            'error': (
                "🚨 *ПОМИЛКА БОТА*\n\n"
                "⚠️ Тип: {error_type}\n"
                "📝 {message}"
            )
        }

        template = templates.get(event_type)
        if not template:
            return

        try:
            text = template.format(**data)
            await self.send_message(text)
        except Exception as e:
            logger.error(f"Помилка форматування сповіщення {event_type}: {e}")

    async def send_trade_notification(self, trade_data: dict):
        """Сповіщення про відкриття угоди (розширене)"""
        emoji = "🟢" if trade_data.get('side') == "BUY" else "🔴"
        side_text = "LONG" if trade_data.get('side') == "BUY" else "SHORT"

        text = f"""{emoji} *НОВА УГОДА ВІДКРИТА* {emoji}

    📊 {trade_data.get('pair')} | {side_text}
    💰 Вхід: ${trade_data.get('entry_price', 0):.2f}
    📦 Кількість: {trade_data.get('quantity', 0):.6f}
    🎯 TP: ${trade_data.get('tp', 0):.2f}
    🛑 SL: ${trade_data.get('sl', 0):.2f}
    💵 Баланс: ${trade_data.get('balance', 0):.2f}
    ⏰ Час: {datetime.now().strftime('%H:%M:%S')}"""

        await self.send_message(text)

    async def send_close_notification(self, trade_data: dict):
        """Сповіщення про закриття угоди (розширене)"""
        pnl = trade_data.get('pnl', 0)
        emoji = "✅" if pnl > 0 else "❌"
        sign = "+" if pnl > 0 else ""

        reason_map = {
            'TAKE_PROFIT': '🎯 Тейк-профіт',
            'STOP_LOSS': '🛑 Стоп-лос',
            'TIME_EXIT': '⏰ Час вийшов',
            'REVERSE_SIGNAL': '🔄 Протилежний сигнал',
            'MANUAL': '✋ Ручне закриття'
        }
        reason_text = reason_map.get(trade_data.get('reason', ''), trade_data.get('reason', 'Невідомо'))

        text = f"""{emoji} *УГОДУ ЗАКРИТО* {emoji}

    📊 {trade_data.get('pair')} | {trade_data.get('side')}
    💰 Вхід: ${trade_data.get('entry_price', 0):.2f}
    💸 Вихід: ${trade_data.get('exit_price', 0):.2f}
    📈 PnL: {sign}{pnl:.2f} USDT ({sign}{trade_data.get('pnl_percent', 0):.2f}%)
    📋 Причина: {reason_text}
    💵 Новий баланс: ${trade_data.get('balance', 0):.2f}"""

        await self.send_message(text)


    async def send_error(self, error_msg: str, error_type: str = "ERROR", traceback_info: str = None):
        """Сповіщення про помилку"""
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        max_len = 3500
        if len(error_msg) > max_len:
            error_msg = error_msg[:max_len] + "..."

        text = f"""🚨 ПОМИЛКА БОТА 🚨

📅 Час: {current_time}
⚠️ Тип: {error_type}

📝 Повідомлення:
{error_msg}"""

        if traceback_info and len(traceback_info) < 1000:
            text += f"\n\n📚 Stack trace:\n{traceback_info[:500]}"

        await self.send_message(text)

    async def send_error_from_exception(self, exception: Exception, context: str = ""):
        """Відправка помилки з Exception об'єкта"""
        error_msg = str(exception)
        traceback_str = traceback.format_exc()
        if context:
            error_msg = f"[{context}] {error_msg}"
        await self.send_error(error_msg, type(exception).__name__, traceback_str)

    async def send_daily_report(self, stats: dict):
        """Щоденний звіт"""
        text = f"""📊 DAILY REPORT

Total Trades: {stats.get('total_trades', 0)}
Win Rate: {stats.get('win_rate', 0):.1f}%
Total PnL: {stats.get('total_pnl', 0):.2f} USDT
Best Trade: {stats.get('best_trade', 0):.2f}
Worst Trade: {stats.get('worst_trade', 0):.2f}
Current Balance: ${stats.get('balance', 0):.2f}

Status: {'🟢 Active' if not stats.get('stopped') else '🔴 Stopped'}"""

        await self.send_message(text)

    # === COMMAND HANDLERS ===

    async def cmd_start(self, message: types.Message):
        """/start - запуск торгівлі"""
        try:
            if self.order_manager:
                self.order_manager.running = True
                balance = self.order_manager.db.get_balance("USDT", True)
                await message.reply(f"✅ Trading started\n\nРежим: Paper Trading\nБаланс: ${balance:.2f}")
            else:
                await message.reply("✅ Bot is running\n\nUse /help for commands")
        except Exception as e:
            await self.send_error_from_exception(e, "cmd_start")
            await message.reply("❌ Помилка при запуску торгівлі")

    async def cmd_stop(self, message: types.Message):
        """/stop - аварійна зупинка"""
        try:
            if self.order_manager:
                self.order_manager.running = False
                await message.reply("🛑 Emergency stop activated\n\nAll trading halted")

                open_trades = self.order_manager.db.get_open_trades(is_paper=True)
                if open_trades:
                    await message.reply(f"⚠️ {len(open_trades)} positions are still open\nUse /close_all to close them")
            else:
                await message.reply("🛑 Bot stopped")
        except Exception as e:
            await self.send_error_from_exception(e, "cmd_stop")
            await message.reply("❌ Помилка при зупинці бота")

    async def cmd_close_all(self, message: types.Message):
        """/close_all - закрити всі відкриті позиції"""
        try:
            if not self.order_manager:
                await message.reply("Bot not initialized")
                return

            open_trades = self.order_manager.db.get_open_trades(is_paper=True)
            if not open_trades:
                await message.reply("No open positions")
                return

            await message.reply(f"Closing {len(open_trades)} positions...")

            closed_count = 0
            for trade in open_trades:
                try:
                    current_price = self.order_manager.exchange.get_current_price(trade.pair)
                    if current_price:
                        result = self.order_manager.paper_engine.execute_sell(trade.id, current_price)
                        if result:
                            closed_count += 1
                            await message.reply(f"✅ Closed {trade.pair} | PnL: {result['pnl']:.2f} USDT")
                except Exception as e:
                    await self.send_error_from_exception(e, f"close_trade_{trade.id}")

            await message.reply(f"Closed {closed_count}/{len(open_trades)} positions")
        except Exception as e:
            await self.send_error_from_exception(e, "cmd_close_all")
            await message.reply("❌ Помилка при закритті позицій")

    async def cmd_status(self, message: types.Message):
        """/status - статус бота"""
        try:
            if not self.order_manager:
                await message.reply("🤖 Bot is running")
                return

            db = self.order_manager.db
            balance = db.get_balance("USDT", True)
            open_trades = len(db.get_open_trades(is_paper=True))

            risk_mgr = self.order_manager.risk_manager
            daily_stats = risk_mgr.get_daily_stats()

            text = f"""📊 BOT STATUS

Mode: Paper Trading
Status: {'RUNNING' if self.order_manager.running else 'STOPPED'}

💰 Balance: ${balance:.2f}
📈 Open Trades: {open_trades}/{self.order_manager.risk_manager.max_open_trades}
📉 Daily PnL: {daily_stats['daily_pnl']:.2f} USDT ({daily_stats['daily_pnl_percent']:.1f}%)
🎯 Daily Limit: {daily_stats['daily_limit']}%
⚠️ Limit Reached: {'Yes' if daily_stats['limit_reached'] else 'No'}

🔄 Last Update: {datetime.now().strftime('%H:%M:%S')}"""

            await message.reply(text)
        except Exception as e:
            await self.send_error_from_exception(e, "cmd_status")
            await message.reply("❌ Помилка при отриманні статусу")

    async def cmd_balance(self, message: types.Message):
        """/balance - поточний баланс"""
        try:
            if not self.order_manager:
                await message.reply("Bot not initialized")
                return

            db = self.order_manager.db
            balance = db.get_balance("USDT", True)
            stats = db.get_stats(is_paper=True)

            text = f"""💰 PAPER BALANCE

Current Balance: ${balance:.2f}
Total PnL: ${stats['total_pnl']:.2f}
Total Trades: {stats['total_trades']}
Win Rate: {stats['win_rate']:.1f}%

Commands:
/reset - Reset to $100
/close_all - Close all positions"""

            await message.reply(text)
        except Exception as e:
            await self.send_error_from_exception(e, "cmd_balance")
            await message.reply("❌ Помилка при отриманні балансу")

    async def cmd_trades(self, message: types.Message):
        """/trades - історія угод"""
        try:
            if not self.order_manager:
                await message.reply("Bot not initialized")
                return

            db = self.order_manager.db
            trades = db.get_trades_history(limit=10, is_paper=True)

            if not trades:
                await message.reply("No trades yet")
                return

            text = "📜 LAST 10 TRADES\n\n"
            for trade in trades[:5]:
                emoji = "✅" if trade.pnl > 0 else "❌" if trade.pnl < 0 else "⚪"
                sign = "+" if trade.pnl > 0 else ""
                text += f"{emoji} {trade.pair} | {trade.side.value}\n"
                text += f"   Entry: ${trade.entry_price:.0f} | Exit: ${trade.exit_price:.0f if trade.exit_price else 0:.0f}\n"
                text += f"   PnL: {sign}{trade.pnl:.2f} ({sign}{trade.pnl_percent:.1f}%)\n"
                text += f"   {trade.opened_at.strftime('%H:%M %d/%m')}\n\n"

            if len(trades) > 5:
                text += f"...and {len(trades) - 5} more"

            await message.reply(text)
        except Exception as e:
            await self.send_error_from_exception(e, "cmd_trades")
            await message.reply("❌ Помилка при отриманні історії угод")

    async def cmd_mode(self, message: types.Message):
        """/mode - поточний режим"""
        try:
            current_mode = config.bot_mode
            text = f"""⚙️ TRADING MODE

Current Mode: {current_mode.upper()}

Available modes:
- paper (Paper Trading - Virtual)
- real (Real Trading - Requires API)

To switch mode, edit .env file and restart bot"""

            await message.reply(text)
        except Exception as e:
            await self.send_error_from_exception(e, "cmd_mode")
            await message.reply("❌ Помилка при отриманні режиму")

    async def send_error_to_admin(self, error_msg: str, error_type: str = "ERROR",
                                  traceback_info: str = None, component: str = None):
        """Відправка помилки адміну"""
        if not self.bot or not self.chat_id:
            logger.warning("Cannot send error - Telegram not configured")
            return

        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        text = f"""🚨 *СИСТЕМНА ПОМИЛКА* 🚨

    📅 Час: {current_time}
    ⚠️ Тип: {error_type}
    🔧 Компонент: {component or 'Unknown'}

    📝 Повідомлення:
    `{error_msg[:300]}`"""

        if traceback_info:
            tb_short = traceback_info[:400] if len(traceback_info) > 400 else traceback_info
            text += f"\n\n📚 Stack trace:\n`{tb_short}`"

        try:
            await self.bot.send_message(
                chat_id=self.chat_id,
                text=text,
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.error(f"Failed to send error to Telegram: {e}")

    async def cmd_reset(self, message: types.Message):
        """/reset - скидання paper балансу"""
        try:
            if not self.order_manager:
                await message.reply("Bot not initialized")
                return

            open_trades = self.order_manager.db.get_open_trades(is_paper=True)
            for trade in open_trades:
                try:
                    current_price = self.order_manager.exchange.get_current_price(trade.pair)
                    if current_price:
                        self.order_manager.paper_engine.execute_sell(trade.id, current_price)
                except Exception as e:
                    await self.send_error_from_exception(e, f"reset_close_trade_{trade.id}")

            self.order_manager.paper_engine.reset()
            await message.reply("✅ Paper trading reset to $100\nAll positions closed")
        except Exception as e:
            await self.send_error_from_exception(e, "cmd_reset")
            await message.reply("❌ Помилка при скиданні")

    async def cmd_ping(self, message: types.Message):
        """Перевірка, чи бот працює"""
        await message.reply("🏓 Pong! Bot is alive")

    async def cmd_stats(self, message: types.Message):
        """/stats - детальна статистика"""
        try:
            if not self.order_manager:
                await message.reply("Bot not initialized")
                return

            db = self.order_manager.db
            stats = db.get_stats(is_paper=True)
            daily = self.order_manager.risk_manager.get_daily_stats()

            text = f"""📈 TRADING STATISTICS

All Time:
Total Trades: {stats['total_trades']}
Win Rate: {stats['win_rate']:.1f}%
Total PnL: ${stats['total_pnl']:.2f}
Avg PnL: ${stats['avg_pnl']:.2f}
Max Drawdown: ${stats['max_drawdown']:.2f}

Today:
Daily PnL: ${daily['daily_pnl']:.2f}
Daily PnL %: {daily['daily_pnl_percent']:.1f}%
Open Positions: {daily['open_trades']}
Daily Limit: {daily['daily_limit']}%"""

            await message.reply(text)
        except Exception as e:
            await self.send_error_from_exception(e, "cmd_stats")
            await message.reply("❌ Помилка при отриманні статистики")

    async def cmd_help(self, message: types.Message):
        keyboard = ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="/status"), KeyboardButton(text="/balance")],
                [KeyboardButton(text="/trades"), KeyboardButton(text="/stats")],
                [KeyboardButton(text="/reset"), KeyboardButton(text="/close_all")],
                [KeyboardButton(text="/start"), KeyboardButton(text="/stop")],
            ],
            resize_keyboard=True
        )
        await message.reply("🤖 AVAILABLE COMMANDS\n\nTap any button:", reply_markup=keyboard)