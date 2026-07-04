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
import trading
from config import (
    BOT_TOKEN,
    BYBIT_TRADE_URL,
    COOLDOWN_TIMES,
    COINGLASS_URL,
    DEFAULT_COOLDOWN_TIME,
    DEFAULT_DUMP_THRESHOLD,
    DEFAULT_DUMP_TIME_WINDOW,
    DEFAULT_PUMP_THRESHOLD,
    DEFAULT_PUMP_TIME_WINDOW,
    GLOBAL_TICK_INTERVAL,
    THRESHOLDS,
    TIME_WINDOWS,
    ADMIN_TELEGRAM_ID,
    OFFSETS,
    SHORT_SIZES,
    TP_SIZES,
    ORDER_TTLS,
    SL_SIZES,
    MAX_POSITIONS_CHOICES,
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
MARKET_CALLBACK_PREFIX = "market_"
MARKET_DEFAULT_WINDOW = 15


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


def _market_keyboard(current_window: int) -> InlineKeyboardMarkup | None:
    """Build the inline keyboard for alternate market windows."""
    buttons: list[InlineKeyboardButton] = []
    for window, label in ((15, "15m"), (30, "30m"), (60, "1h")):
        if window != current_window:
            buttons.append(
                InlineKeyboardButton(
                    label,
                    callback_data=f"{MARKET_CALLBACK_PREFIX}{window}",
                )
            )

    if not buttons:
        return None
    return InlineKeyboardMarkup([buttons])


def _format_market_message(window_minutes: int) -> str:
    """Build the /market leaderboard message."""
    movers = collector.get_top_market_pumps(window_minutes)
    heading = (
        f"📈 *Market Pulse*\n\n"
        f"Top 10 Bybit USDT perps by pump over the last *{_format_window_label(window_minutes)}*."
    )
    if not movers:
        return (
            f"{heading}\n\n"
            "Not enough price history is available yet. Try again in a few minutes."
        )

    lines = [heading, ""]
    for index, mover in enumerate(movers, start=1):
        coin = mover.symbol.replace("USDT", "")
        bybit_url = BYBIT_TRADE_URL.format(symbol=mover.symbol)
        coinglass_url = COINGLASS_URL.format(symbol=mover.symbol)
        lines.append(
            f"{index}. [${coin}]({coinglass_url}) • [Bybit]({bybit_url}) • *{mover.pump_pct}%*"
        )
    return "\n".join(lines)


def _format_window_label(window_minutes: int) -> str:
    """Render a market window in human-friendly form."""
    return "1 hour" if window_minutes == 60 else f"{window_minutes} minutes"


# ── Conversation states ──────────────────────────────────
(
    ASK_PUMP_THRESHOLD,
    ASK_PUMP_WINDOW,
    ASK_DUMP_THRESHOLD,
    ASK_DUMP_WINDOW,
    ASK_COOLDOWN,
    ASK_OFFSET,
    ASK_SHORT_SIZE,
    ASK_TP_SIZE,
    ASK_ORDER_TTL,
    ASK_SL_SIZE,
    ASK_MAX_OPEN_POSITIONS,
) = range(11)


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


async def market_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Send the current top pump leaderboard for the default market window."""
    text = await asyncio.to_thread(_format_market_message, MARKET_DEFAULT_WINDOW)
    await update.message.reply_text(
        text,
        parse_mode="Markdown",
        disable_web_page_preview=True,
        reply_markup=_market_keyboard(MARKET_DEFAULT_WINDOW),
    )


async def market_window_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a new market leaderboard message for the selected window."""
    query = update.callback_query
    await query.answer()

    window_minutes = int(query.data.removeprefix(MARKET_CALLBACK_PREFIX))
    text = await asyncio.to_thread(_format_market_message, window_minutes)
    await query.message.reply_text(
        text,
        parse_mode="Markdown",
        disable_web_page_preview=True,
        reply_markup=_market_keyboard(window_minutes),
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


# ── Trading setup conversation callbacks ─────────────────

async def start_trading(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Begin the trading setup flow – ask for OFFSET."""
    if update.effective_user.id != ADMIN_TELEGRAM_ID:
        await update.message.reply_text("⛔ Access denied.")
        return ConversationHandler.END

    buttons = [
        [InlineKeyboardButton(f"{o}%", callback_data=f"toffset_{o}")]
        for o in OFFSETS
    ]
    await update.message.reply_text(
        "💼 *Bybit Short Trading Setup*\n\n"
        "📈 *Step 1/6 – OFFSET (Max Slippage)*\n"
        "What maximum slippage (offset below trigger price) is acceptable?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return ASK_OFFSET


async def ask_offset(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    value = float(query.data.split("_")[1])
    ctx.user_data["offset"] = value

    buttons = [
        [InlineKeyboardButton(f"{s} USDT", callback_data=f"tshortsize_{s}")]
        for s in SHORT_SIZES
    ]
    await query.edit_message_text(
        "💰 *Step 2/6 – SHORT_SIZE*\n"
        "What notional size per trade (in USDT) should the bot open?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return ASK_SHORT_SIZE


async def ask_short_size(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    value = float(query.data.split("_")[1])
    ctx.user_data["short_size"] = value

    buttons = [
        [InlineKeyboardButton(f"{tp}%", callback_data=f"ttpsize_{tp}")]
        for tp in TP_SIZES
    ]
    await query.edit_message_text(
        "🎯 *Step 3/6 – TP_SIZE (Take Profit)*\n"
        "What distance below the entry price should the Take Profit trigger?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return ASK_TP_SIZE


async def ask_tp_size(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    value = float(query.data.split("_")[1])
    ctx.user_data["tp_size"] = value

    buttons = [
        [InlineKeyboardButton(f"{t} min", callback_data=f"torderttl_{t}")]
        for t in ORDER_TTLS
    ]
    await query.edit_message_text(
        "⏳ *Step 4/6 – ORDER_TTL (Unfilled TTL)*\n"
        "After how many minutes should an unfilled entry order be cancelled?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return ASK_ORDER_TTL


async def ask_order_ttl(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    value = int(query.data.split("_")[1])
    ctx.user_data["order_ttl"] = value

    buttons = [
        [InlineKeyboardButton(f"{sl}%", callback_data=f"tslsize_{sl}")]
        for sl in SL_SIZES
    ]
    await query.edit_message_text(
        "🛡 *Step 5/6 – SL_SIZE (Stop Loss)*\n"
        "What distance above the entry price should the Stop Loss trigger?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return ASK_SL_SIZE


async def ask_sl_size(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    value = float(query.data.split("_")[1])
    ctx.user_data["sl_size"] = value

    buttons = [
        [InlineKeyboardButton(f"{m}", callback_data=f"tmaxpos_{m}")]
        for m in MAX_POSITIONS_CHOICES
    ]
    await query.edit_message_text(
        "📊 *Step 6/6 – MAX_OPEN_POSITIONS*\n"
        "What is the maximum allowed open positions (bot + manual) on the account?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return ASK_MAX_OPEN_POSITIONS


async def ask_max_open_positions(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    max_pos = int(query.data.split("_")[1])
    offset = ctx.user_data.get("offset", 1.0)
    short_size = ctx.user_data.get("short_size", 100.0)
    tp_size = ctx.user_data.get("tp_size", 5.0)
    order_ttl = ctx.user_data.get("order_ttl", 10)
    sl_size = ctx.user_data.get("sl_size", 5.0)

    user_id = query.from_user.id
    db.upsert_user(
        user_id,
        offset=offset,
        short_size=short_size,
        tp_size=tp_size,
        order_ttl=order_ttl,
        sl_size=sl_size,
        max_open_positions=max_pos,
        trading_enabled=1,
    )

    await query.edit_message_text(
        "✅ *Trading Configuration Saved & Enabled!*\n\n"
        f"📐 Offset (Max Slippage): *{offset}%*\n"
        f"💰 Short Notional Size: *{short_size} USDT*\n"
        f"🎯 Take Profit (TP): *-{tp_size}%*\n"
        f"⏳ Entry Order TTL: *{order_ttl} min*\n"
        f"🛡 Stop Loss (SL): *+{sl_size}%*\n"
        f"📊 Max Positions Cap: *{max_pos}*\n\n"
        "The bot will now open short positions on Bybit when pump signals trigger.",
        parse_mode="Markdown",
    )
    return ConversationHandler.END


async def cancel_trading_setup(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Setup cancelled. Use /start_trading to try again.")
    return ConversationHandler.END


# ── Stop & Status Commands ──────────────────────────────

async def stop_trading_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_TELEGRAM_ID:
        await update.message.reply_text("⛔ Access denied.")
        return
    
    db.upsert_user(update.effective_user.id, trading_enabled=0)
    await update.message.reply_text(
        "⏸ *Trading Stopped*\n\n"
        "New trades will not be opened. Pending orders and open positions stay active and continue to be monitored.",
        parse_mode="Markdown"
    )


async def trading_status_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_TELEGRAM_ID:
        await update.message.reply_text("⛔ Access denied.")
        return

    user = db.get_user(update.effective_user.id)
    if not user:
        await update.message.reply_text("⚠️ No configuration found. Use /start_trading to configure parameters.")
        return

    bot_positions = await asyncio.to_thread(trading.get_bot_positions_info)
    today_pnl = await asyncio.to_thread(db.get_realized_pnl_today)

    status_str = "🟢 Enabled" if user.get("trading_enabled") else "⏸ Disabled/Paused"
    
    params_str = (
        f"📐 Offset (Max Slippage): *{user.get('offset', 1.0)}%*\n"
        f"💰 Short Size: *{user.get('short_size', 100.0)} USDT*\n"
        f"🎯 TP size: *{user.get('tp_size', 5.0)}%*\n"
        f"⏳ Order TTL: *{user.get('order_ttl', 10)} min*\n"
        f"🛡 SL size: *{user.get('sl_size', 5.0)}%*\n"
        f"📊 Max Positions: *{user.get('max_open_positions', 5)}*"
    )

    pos_lines = []
    if bot_positions:
        for pos in bot_positions:
            pnl_sign = "+" if pos["unrealised_pnl"] >= 0 else ""
            coin = pos["symbol"].replace("USDT", "")
            pos_lines.append(
                f"• *{coin}* (Short): Size *{pos['size']}* | Entry: *{pos['avg_price']}* | Mark: *{pos['current_price']}*\n"
                f"  Unrealised PnL: *{pnl_sign}{pos['unrealised_pnl']:.4f} USDT* (TP: {pos['tp_price']} | SL: {pos['sl_price']})"
            )
    else:
        pos_lines.append("No active bot positions.")

    pnl_sign = "+" if today_pnl >= 0 else ""
    msg = (
        f"📊 *Bybit Trading Status*\n\n"
        f"Trading State: *{status_str}*\n\n"
        f"⚙️ *Trading Parameters:*\n{params_str}\n\n"
        f"💼 *Active Bot Positions:*\n" + "\n".join(pos_lines) + f"\n\n"
        f"💰 *Realised PnL (Today UTC):* *{pnl_sign}{today_pnl:.4f} USDT*"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


# ── Periodic job callback ───────────────────────────────


async def tick(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Called every GLOBAL_TICK_INTERVAL seconds by the job queue."""
    await collector.collect_and_alert(ctx.bot)
    await asyncio.to_thread(collector.cleanup_cooldown_cache)
    await trading.manage_active_trades(ctx.bot)


# ── Main ─────────────────────────────────────────────────


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN not set in .env")

    db.init_db()
    logger.info("Database initialised")

    async def post_init(application: Application) -> None:
        logger.info("Running startup trade reconciliation...")
        await trading.manage_active_trades(application.bot)
        logger.info("Startup trade reconciliation complete.")

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

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

    trading_conv = ConversationHandler(
        entry_points=[
            CommandHandler("start_trading", start_trading),
        ],
        states={
            ASK_OFFSET: [
                CallbackQueryHandler(ask_offset, pattern=r"^toffset_")
            ],
            ASK_SHORT_SIZE: [
                CallbackQueryHandler(ask_short_size, pattern=r"^tshortsize_")
            ],
            ASK_TP_SIZE: [
                CallbackQueryHandler(ask_tp_size, pattern=r"^ttpsize_")
            ],
            ASK_ORDER_TTL: [
                CallbackQueryHandler(ask_order_ttl, pattern=r"^torderttl_")
            ],
            ASK_SL_SIZE: [
                CallbackQueryHandler(ask_sl_size, pattern=r"^tslsize_")
            ],
            ASK_MAX_OPEN_POSITIONS: [
                CallbackQueryHandler(ask_max_open_positions, pattern=r"^tmaxpos_")
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_trading_setup)],
        per_message=False,
    )
    app.add_handler(trading_conv)
    app.add_handler(CommandHandler("stop_trading", stop_trading_cmd))
    app.add_handler(CommandHandler("trading_status", trading_status_cmd))

    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("market", market_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("testalert", testalert_cmd))
    app.add_handler(CommandHandler("pause", pause_cmd))
    app.add_handler(CommandHandler("resume", resume_cmd))
    app.add_handler(CallbackQueryHandler(pause_select, pattern=r"^pause_"))
    app.add_handler(
        CallbackQueryHandler(market_window_cmd, pattern=r"^market_(15|30|60)$")
    )

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
