"""Pump Detector Telegram Bot – entry point."""

import logging

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import collector
import db
from config import (
    BOT_TOKEN,
    DEFAULT_PUMP_THRESHOLD,
    DEFAULT_SCAN_FREQUENCY,
    DEFAULT_TIME_WINDOW,
    GLOBAL_TICK_INTERVAL,
    MIN_SCAN_FREQUENCY,
    PUMP_THRESHOLDS,
    TIME_WINDOWS,
)

# ── Logging (errors + lifecycle only) ────────────────────
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.WARNING,
)
logger = logging.getLogger("pump_detector")
logger.setLevel(logging.INFO)


# ── Conversation states ──────────────────────────────────
ASK_FREQUENCY, ASK_THRESHOLD, ASK_WINDOW = range(3)


# ── /start ───────────────────────────────────────────────


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Begin the setup flow – ask scan frequency."""
    await update.message.reply_text(
        "👋 Welcome to *Pump Detector*!\n\n"
        "Let's configure your alert settings.\n\n"
        "📡 *Step 1/3 – Scan frequency*\n"
        "How often (in seconds) should market prices be checked?\n"
        f"Minimum is *{MIN_SCAN_FREQUENCY}* seconds.\n\n"
        "Type a number:",
        parse_mode="Markdown",
    )
    return ASK_FREQUENCY


async def ask_frequency(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive scan frequency and ask for pump threshold."""
    text = update.message.text.strip()
    try:
        freq = int(text)
    except ValueError:
        await update.message.reply_text("❌ Please enter a valid whole number.")
        return ASK_FREQUENCY

    if freq < MIN_SCAN_FREQUENCY:
        await update.message.reply_text(
            f"❌ Minimum scan frequency is {MIN_SCAN_FREQUENCY} seconds. Try again."
        )
        return ASK_FREQUENCY

    ctx.user_data["scan_frequency"] = freq

    # Build threshold keyboard
    buttons = [
        [InlineKeyboardButton(f"{t}%", callback_data=f"thresh_{t}")]
        for t in PUMP_THRESHOLDS
    ]
    await update.message.reply_text(
        "📈 *Step 2/3 – Pump threshold*\n"
        "What minimum pump percentage should trigger an alert?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return ASK_THRESHOLD


async def ask_threshold(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive pump threshold callback and ask for time window."""
    query = update.callback_query
    await query.answer()

    value = float(query.data.split("_")[1])
    ctx.user_data["pump_threshold"] = value

    buttons = [
        [InlineKeyboardButton(f"{w} min", callback_data=f"window_{w}")]
        for w in TIME_WINDOWS
    ]
    await query.edit_message_text(
        "⏱ *Step 3/3 – Time window*\n"
        "Over how many minutes should the pump be measured?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return ASK_WINDOW


async def ask_window(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive time window callback, save settings, finish setup."""
    query = update.callback_query
    await query.answer()

    window = int(query.data.split("_")[1])
    freq = ctx.user_data.get("scan_frequency", DEFAULT_SCAN_FREQUENCY)
    threshold = ctx.user_data.get("pump_threshold", DEFAULT_PUMP_THRESHOLD)

    user_id = query.from_user.id
    db.upsert_user(
        user_id,
        scan_frequency=freq,
        pump_threshold=threshold,
        time_window=window,
        is_paused=0,
        is_setup_complete=1,
    )

    await query.edit_message_text(
        "✅ *Setup complete!*\n\n"
        f"📡 Scan frequency: *{freq}s*\n"
        f"📈 Pump threshold: *{threshold}%*\n"
        f"⏱ Time window: *{window} min*\n\n"
        "You will now receive alerts when a coin pumps above your threshold.\n"
        "Use /help to see all available commands.",
        parse_mode="Markdown",
    )
    return ConversationHandler.END


async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel the conversation."""
    await update.message.reply_text(
        "Setup cancelled. Use /start or /param to try again."
    )
    return ConversationHandler.END


# ── /help ────────────────────────────────────────────────

HELP_TEXT = (
    "ℹ️ *Pump Detector – Help*\n\n"
    "This bot monitors all Bybit USDT perpetual futures and alerts you "
    "when a coin pumps above your configured threshold within your chosen "
    "time window.\n\n"
    "*Commands:*\n"
    "/start – Initial setup (scan frequency, threshold, time window)\n"
    "/param – Change your alert parameters\n"
    "/status – View your current settings\n"
    "/pause – Pause alerts\n"
    "/resume – Resume alerts\n"
    "/help – Show this message\n"
)


async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT, parse_mode="Markdown")


# ── /status ──────────────────────────────────────────────


async def status_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user = db.get_user(update.effective_user.id)
    if not user or not user["is_setup_complete"]:
        await update.message.reply_text(
            "⚠️ You haven't set up the bot yet. Use /start to begin."
        )
        return

    state = "⏸ Paused" if user["is_paused"] else "▶️ Active"
    await update.message.reply_text(
        f"📊 *Your Settings*\n\n"
        f"📡 Scan frequency: *{user['scan_frequency']}s*\n"
        f"📈 Pump threshold: *{user['pump_threshold']}%*\n"
        f"⏱ Time window: *{user['time_window']} min*\n"
        f"Status: *{state}*",
        parse_mode="Markdown",
    )


# ── /pause & /resume ────────────────────────────────────


async def pause_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    db.upsert_user(uid, is_paused=1)
    logger.info("User %s paused alerts", uid)
    await update.message.reply_text("⏸ Alerts paused. Use /resume to restart.")


async def resume_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    db.upsert_user(uid, is_paused=0)
    logger.info("User %s resumed alerts", uid)
    await update.message.reply_text("▶️ Alerts resumed!")


# ── Periodic job callback ───────────────────────────────


async def tick(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Called every GLOBAL_TICK_INTERVAL seconds by the job queue."""
    await collector.collect_and_alert(ctx.bot)
    collector.cleanup_cooldown_cache()


# ── Main ─────────────────────────────────────────────────


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN not set in .env")

    db.init_db()
    logger.info("Database initialised")

    app = Application.builder().token(BOT_TOKEN).build()

    # Conversation handler for /start and /param (same flow)
    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CommandHandler("param", start),
        ],
        states={
            ASK_FREQUENCY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, ask_frequency)
            ],
            ASK_THRESHOLD: [CallbackQueryHandler(ask_threshold, pattern=r"^thresh_")],
            ASK_WINDOW: [CallbackQueryHandler(ask_window, pattern=r"^window_")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=False,
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("pause", pause_cmd))
    app.add_handler(CommandHandler("resume", resume_cmd))

    # Schedule the global collector tick
    app.job_queue.run_repeating(tick, interval=GLOBAL_TICK_INTERVAL, first=5)

    logger.info("Bot starting – polling…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
