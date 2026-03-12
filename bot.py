"""Pump Detector Telegram Bot – entry point."""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

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
    GLOBAL_TICK_INTERVAL,
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

PAUSE_1H = "pause_60"
PAUSE_8H = "pause_480"
PAUSE_TOMORROW = "pause_tomorrow"
PAUSE_FOREVER = "pause_forever"


def _format_pause_state(user: dict) -> str:
    """Render the user's pause state for /status."""
    if user["is_paused"]:
        return "⏸ Paused until /resume"

    paused_until_ts = user.get("paused_until_ts")
    if paused_until_ts and paused_until_ts > datetime.now(timezone.utc).timestamp():
        paused_until = datetime.fromtimestamp(paused_until_ts, tz=timezone.utc)
        return f"⏸ Paused until {paused_until.strftime('%Y-%m-%d %H:%M UTC')}"

    return "▶️ Active"


def _pause_selection_to_deadline(selection: str) -> tuple[float, str]:
    """Map a pause callback value to a UTC timestamp and label."""
    now = datetime.now(timezone.utc)
    if selection == PAUSE_1H:
        return (now + timedelta(hours=1)).timestamp(), "1 hour"
    if selection == PAUSE_8H:
        return (now + timedelta(hours=8)).timestamp(), "8 hours"
    if selection == PAUSE_TOMORROW:
        tomorrow = datetime.combine(
            (now + timedelta(days=1)).date(),
            datetime.min.time(),
            tzinfo=timezone.utc,
        )
        return tomorrow.timestamp(), "until tomorrow UTC"
    raise ValueError(f"Unexpected pause selection: {selection}")


# ── Conversation states ──────────────────────────────────
(
    ASK_PUMP_THRESHOLD,
    ASK_PUMP_WINDOW,
    ASK_DUMP_THRESHOLD,
    ASK_DUMP_WINDOW,
    ASK_COOLDOWN,
) = range(5)


# ── /start & /param ─────────────────────────────────────


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Begin the setup flow – ask for pump threshold."""
    buttons = [
        [InlineKeyboardButton(f"{t}%", callback_data=f"pthresh_{t}")]
        for t in THRESHOLDS
    ]
    await update.message.reply_text(
        "👋 Welcome to *Pump Detector*!\n\n"
        "Let's configure your alert settings.\n\n"
        "📈 *Step 1/5 – Pump threshold*\n"
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
        "⏱ *Step 2/5 – Pump time window*\n"
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
        "📉 *Step 3/5 – Dump threshold*\n"
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
        "⏱ *Step 4/5 – Dump time window*\n"
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
        "⏳ *Step 5/5 – Alert cooldown*\n"
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
    pump_thresh = ctx.user_data.get("pump_threshold", DEFAULT_PUMP_THRESHOLD)
    pump_tw = ctx.user_data.get("pump_time_window", DEFAULT_PUMP_TIME_WINDOW)
    dump_thresh = ctx.user_data.get("dump_threshold", DEFAULT_DUMP_THRESHOLD)
    dump_tw = ctx.user_data.get("dump_time_window", DEFAULT_DUMP_TIME_WINDOW)

    user_id = query.from_user.id
    db.upsert_user(
        user_id,
        pump_threshold=pump_thresh,
        pump_time_window=pump_tw,
        dump_threshold=dump_thresh,
        dump_time_window=dump_tw,
        cooldown_time=cooldown,
        is_paused=0,
        paused_until_ts=None,
        is_setup_complete=1,
    )

    await query.edit_message_text(
        "✅ *Setup complete!*\n\n"
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
    "/start – Initial setup (pump & dump thresholds/windows & cooldown)\n"
    "/param – Change your alert parameters\n"
    "/status – View your current settings\n"
    "/testalert – Send yourself a sample alert\n"
    "/pause – Pause alerts for 1h, 8h, until tomorrow, or until resume\n"
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

    state = _format_pause_state(user)
    await update.message.reply_text(
        f"📐 *Current Settings*\n\n"
        f"📡 Scan interval: *{GLOBAL_TICK_INTERVAL}s*\n"
        "💹 Price basis: *Bybit mark price*\n"
        "🧠 Trigger logic: *current vs. lowest/highest price seen inside the selected window*\n\n"
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
    user = db.get_user(update.effective_user.id)
    if not user or not user["is_setup_complete"]:
        await update.message.reply_text(
            "⚠️ You haven't set up the bot yet. Use /start to begin."
        )
        return

    buttons = [
        [
            InlineKeyboardButton("1 hour", callback_data=PAUSE_1H),
            InlineKeyboardButton("8 hours", callback_data=PAUSE_8H),
        ],
        [InlineKeyboardButton("Until tomorrow UTC", callback_data=PAUSE_TOMORROW)],
        [InlineKeyboardButton("Until /resume", callback_data=PAUSE_FOREVER)],
    ]
    await update.message.reply_text(
        "⏸ Choose how long to pause alerts:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def pause_select(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Apply a pause duration selected from the pause keyboard."""
    query = update.callback_query
    await query.answer()

    uid = query.from_user.id
    if query.data == PAUSE_FOREVER:
        db.upsert_user(uid, is_paused=1, paused_until_ts=None)
        logger.info("User %s paused alerts indefinitely", uid)
        await query.edit_message_text("⏸ Alerts paused until you use /resume.")
        return

    paused_until_ts, label = _pause_selection_to_deadline(query.data)
    db.upsert_user(uid, is_paused=0, paused_until_ts=paused_until_ts)
    logger.info("User %s paused alerts until %s", uid, paused_until_ts)
    await query.edit_message_text(f"⏸ Alerts paused for *{label}*.", parse_mode="Markdown")


async def resume_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    db.upsert_user(uid, is_paused=0, paused_until_ts=None)
    logger.info("User %s resumed alerts", uid)
    await update.message.reply_text("▶️ Alerts resumed!")


async def testalert_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a sample alert using the user's current settings."""
    user = db.get_user(update.effective_user.id)
    if not user or not user["is_setup_complete"]:
        await update.message.reply_text(
            "⚠️ You haven't set up the bot yet. Use /start to begin."
        )
        return

    pump_tw = user["pump_time_window"]
    pump_threshold = user["pump_threshold"]
    text = (
        "🧪 *Sample alert*\n\n"
        f"🏦[ByBit](https://www.bybit.com/trade/usdt/BTCUSDT) – {pump_tw}m – "
        "[BTC](https://www.coinglass.com/tv/Bybit_BTCUSDT)\n"
        f"🟢*Pump*: *{pump_threshold + 1:.2f}%*\n"
        "#️⃣Signal 24h: sample"
    )
    await update.message.reply_text(
        text,
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )


# ── Periodic job callback ───────────────────────────────


async def tick(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Called every GLOBAL_TICK_INTERVAL seconds by the job queue."""
    await collector.collect_and_alert(ctx.bot)
    await asyncio.to_thread(collector.cleanup_cooldown_cache)


# ── Main ─────────────────────────────────────────────────


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN not set in .env")

    db.init_db()
    logger.info("Database initialised")

    app = Application.builder().token(BOT_TOKEN).build()

    # Conversation handler for /start and /param (5-step flow)
    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CommandHandler("param", start),
        ],
        states={
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
    app.add_handler(CommandHandler("testalert", testalert_cmd))
    app.add_handler(CommandHandler("pause", pause_cmd))
    app.add_handler(CommandHandler("resume", resume_cmd))
    app.add_handler(CallbackQueryHandler(pause_select, pattern=r"^pause_"))

    # Schedule the global collector tick
    app.job_queue.run_repeating(
        tick,
        interval=GLOBAL_TICK_INTERVAL,
        first=5,
        job_kwargs={"max_instances": 1, "coalesce": True},
    )

    logger.info("Bot starting – polling…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
