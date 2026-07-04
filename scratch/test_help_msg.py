import os
import asyncio
from dotenv import load_dotenv
from telegram import Bot

load_dotenv()

bot_token = os.getenv("BOT_TOKEN")
admin_id = os.getenv("ADMIN_TELEGRAM_ID")

HELP_TEXT = (
    "ℹ️ *Pump Detector – Help*\n\n"
    "This bot monitors all Bybit USDT perpetual futures and alerts you "
    "when a coin pumps or dumps beyond your configured thresholds within "
    "your chosen time windows.\n\n"
    "*Alert examples:*\n"
    "🟢Pump: PIPPIN: 36.32%\n"
    "🔴Dump: PIPPIN: -28.50%\n\n"
    "*Alert Commands:*\n"
    "/start – Initial setup (thresholds, time, cooldown)\n"
    "/param – Change your alert parameters\n"
    "/status – View your current settings\n"
    "/market – Show the strongest pumps over the default 15m window\n"
    "/testalert – Send yourself a sample alert\n"
    "/pause – Pause alerts for 1h, 8h, until tomorrow, or until resume\n"
    "/resume – Resume alerts\n"
    "/help – Show this message\n\n"
    "*Trading Commands (Admin Only):*\n"
    "/start_trading – Configure and enable trading\n"
    "/stop_trading – Stop bot trading activity (existing positions/orders remain active)\n"
    "/trading_status – View parameters, open positions and today's PnL\n"
)

async def test_send():
    if not bot_token or not admin_id:
        print("Missing credentials.")
        return
    bot = Bot(token=bot_token)
    try:
        print("Sending help message to Telegram...")
        await bot.send_message(chat_id=int(admin_id), text=HELP_TEXT, parse_mode="Markdown")
        print("Message sent successfully!")
    except Exception as e:
        print("Error sending message:", e)

if __name__ == "__main__":
    asyncio.run(test_send())
