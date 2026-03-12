"""Global constants and predefined option lists."""

import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")

# ── Database ──────────────────────────────────────────────
DB_PATH = "pump_detector.db"
RETENTION_HOURS = 72

# ── Scan ──────────────────────────────────────────────────
MIN_SCAN_FREQUENCY = 30  # seconds
DEFAULT_SCAN_FREQUENCY = 30  # seconds
GLOBAL_TICK_INTERVAL = 30  # seconds – collector always runs at this pace

# ── Thresholds (%) – same choices for pump & dump ────────
THRESHOLDS = [10, 20, 30, 50, 80]
DEFAULT_PUMP_THRESHOLD = 10.0
DEFAULT_DUMP_THRESHOLD = 10.0

# ── Time windows (minutes) – same choices for pump & dump
TIME_WINDOWS = [5, 10, 15, 20, 30, 40, 60]
DEFAULT_PUMP_TIME_WINDOW = 15
DEFAULT_DUMP_TIME_WINDOW = 15

# ── Alert cooldown (minutes) ─────────────────────────────
COOLDOWN_TIMES = [15, 30, 45, 60, 120, 240]
DEFAULT_COOLDOWN_TIME = 30

# ── URL templates ───────────────────────────────────────
BYBIT_TRADE_URL = "https://www.bybit.com/trade/usdt/{symbol}"
COINGLASS_URL = "https://www.coinglass.com/tv/Bybit_{symbol}"
