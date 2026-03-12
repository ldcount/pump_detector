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
    COOLDOWN_TIMES,
    DEFAULT_COOLDOWN_TIME,
    DEFAULT_DUMP_THRESHOLD,
    DEFAULT_DUMP_TIME_WINDOW,
    DEFAULT_PUMP_THRESHOLD,
    DEFAULT_PUMP_TIME_WINDOW,
    DEFAULT_SCAN_FREQUENCY,
    GLOBAL_TICK_INTERVAL,
    MIN_SCAN_FREQUENCY,
    THRESHOLDS,
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
(
    ASK_FREQUENCY,
    ASK_PUMP_THRESHOLD,
    ASK_PUMP_WINDOW,
    ASK_DUMP_THRESHOLD,
    ASK_DUMP_WINDOW,
    ASK_COOLDOWN,
) = range(6)


# ── /start & /param ─────────────────────────────────────


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Begin the setup flow – ask scan frequency."""
    await update.message.reply_text(
        "👋 Welcome to *Pump Detector*!\n\n"
        "Let's configure your alert settings.\n\n"
        "📡 *Step 1/6 – Scan frequency*\n"
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

    buttons = [
        [InlineKeyboardButton(f"{t}%", callback_data=f"pthresh_{t}")]
        for t in THRESHOLDS
    ]
    await update.message.reply_text(
        "📈 *Step 2/6 – Pump threshold*\n"
        "What minimum pump percentage should trigger an alert?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return ASK_PUMP_THRESHOLD


async def ask_pump_threshold(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive pump threshold and ask for pump time window."""
    query = update.callback_query
    await query.answer()

    value = float(query.data.split("_")[1])
    ctx.user_data["pump_threshold"] = value

    buttons = [
        [InlineKeyboardButton(f"{w} min", callback_data=f"pwindow_{w}")]
        for w in TIME_WINDOWS
    ]
    await query.edit_message_text(
        "⏱ *Step 3/6 – Pump time window*\n"
        "Over how many minutes should the pump be measured?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return ASK_PUMP_WINDOW


async def ask_pump_window(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive pump window and ask for dump threshold."""
    query = update.callback_query
    await query.answer()

    ctx.user_data["pump_time_window"] = int(query.data.split("_")[1])

    buttons = [
        [InlineKeyboardButton(f"{t}%", callback_data=f"dthresh_{t}")]
        for t in THRESHOLDS
    ]
    await query.edit_message_text(
        "📉 *Step 4/6 – Dump threshold*\n"
        "What minimum dump percentage should trigger an alert?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return ASK_DUMP_THRESHOLD


async def ask_dump_threshold(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive dump threshold and ask for dump time window."""
    query = update.callback_query
    await query.answer()

    value = float(query.data.split("_")[1])
    ctx.user_data["dump_threshold"] = value

    buttons = [
        [InlineKeyboardButton(f"{w} min", callback_data=f"dwindow_{w}")]
        for w in TIME_WINDOWS
    ]
    await query.edit_message_text(
        "⏱ *Step 5/6 – Dump time window*\n"
        "Over how many minutes should the dump be measured?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return ASK_DUMP_WINDOW


async def ask_dump_window(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive dump window and ask for cooldown time."""
    query = update.callback_query
    await query.answer()

    ctx.user_data["dump_time_window"] = int(query.data.split("_")[1])

    buttons = [
        [InlineKeyboardButton(f"{w} min", callback_data=f"cooldown_{w}")]
        for w in COOLDOWN_TIMES
    ]
    await query.edit_message_text(
        "⏳ *Step 6/6 – Alert cooldown*\n"
        "How many minutes should pass before receiving another alert for the same coin?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return ASK_COOLDOWN


async def ask_cooldown(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive cooldown time, save all settings, finish setup."""
    query = update.callback_query
    await query.answer()

    cooldown = int(query.data.split("_")[1])
    freq = ctx.user_data.get("scan_frequency", DEFAULT_SCAN_FREQUENCY)
    pump_thresh = ctx.user_data.get("pump_threshold", DEFAULT_PUMP_THRESHOLD)
    pump_tw = ctx.user_data.get("pump_time_window", DEFAULT_PUMP_TIME_WINDOW)
    dump_thresh = ctx.user_data.get("dump_threshold", DEFAULT_DUMP_THRESHOLD)
    dump_tw = ctx.user_data.get("dump_time_window", DEFAULT_DUMP_TIME_WINDOW)

    user_id = query.from_user.id
    db.upsert_user(
        user_id,
        scan_frequency=freq,
        pump_threshold=pump_thresh,
        pump_time_window=pump_tw,
        dump_threshold=dump_thresh,
        dump_time_window=dump_tw,
        cooldown_time=cooldown,
        is_paused=0,
        is_setup_complete=1,
    )

    await query.edit_message_text(
        "✅ *Setup complete!*\n\n"
        f"📡 Scan frequency: *{freq}s*\n\n"
        f"📈 Pump threshold: *{pump_thresh}%*\n"
        f"⏱ Pump window: *{pump_tw} min*\n\n"
        f"📉 Dump threshold: *{dump_thresh}%*\n"
        f"⏱ Dump window: *{dump_tw} min*\n\n"
        f"⏳ Cooldown time: *{cooldown} min*\n\n"
        "You will now receive alerts for pumps and dumps.\n"
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
    "when a coin pumps or dumps beyond your configured thresholds within "
    "your chosen time windows.\n\n"
    "*Alert examples:*\n"
    "🟢Pump: PIPPIN: 36.32%\n"
    "🔴Dump: PIPPIN: -28.50%\n\n"
    "*Commands:*\n"
    "/start – Initial setup (frequency, pump & dump settings)\n"
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
        f"📐 *Current Settings*\n\n"
        f"📡 Scan frequency: *{user['scan_frequency']}s*\n\n"
        f"📈 Pump threshold: *{user['pump_threshold']}%*\n"
        f"⏱ Pump window: *{user['pump_time_window']} min*\n\n"
        f"📉 Dump threshold: *{user['dump_threshold']}%*\n"
        f"⏱ Dump window: *{user['dump_time_window']} min*\n\n"
        f"⏳ Cooldown time: *{user.get('cooldown_time', DEFAULT_COOLDOWN_TIME)} min*\n\n"
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

    # Conversation handler for /start and /param (same 5-step flow)
    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CommandHandler("param", start),
        ],
        states={
            ASK_FREQUENCY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, ask_frequency)
            ],
            ASK_PUMP_THRESHOLD: [
                CallbackQueryHandler(ask_pump_threshold, pattern=r"^pthresh_")
            ],
            ASK_PUMP_WINDOW: [
                CallbackQueryHandler(ask_pump_window, pattern=r"^pwindow_")
            ],
            ASK_DUMP_THRESHOLD: [
                CallbackQueryHandler(ask_dump_threshold, pattern=r"^dthresh_")
            ],
            ASK_DUMP_WINDOW: [
                CallbackQueryHandler(ask_dump_window, pattern=r"^dwindow_")
            ],
            ASK_COOLDOWN: [CallbackQueryHandler(ask_cooldown, pattern=r"^cooldown_")],
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
