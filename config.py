"""Global constants and predefined option lists."""

import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")

# ── Database ──────────────────────────────────────────────
DB_PATH = "pump_detector.db"
RETENTION_HOURS = 72

# ── Scan ──────────────────────────────────────────────────
MIN_SCAN_FREQUENCY = 30          # seconds
DEFAULT_SCAN_FREQUENCY = 30      # seconds
GLOBAL_TICK_INTERVAL = 30        # seconds – collector always runs at this pace

# ── Pump thresholds (%) ──────────────────────────────────
PUMP_THRESHOLDS = [10, 20, 30, 50, 80]
DEFAULT_PUMP_THRESHOLD = 10.0

# ── Time windows (minutes) ───────────────────────────────
TIME_WINDOWS = [5, 10, 15, 20, 30, 40, 60]
DEFAULT_TIME_WINDOW = 15

# ── Alert cooldown ───────────────────────────────────────
ALERT_COOLDOWN_SECONDS = 300     # 5 min – suppress repeat alerts per user/symbol/window

# ── Bybit URL template ──────────────────────────────────
BYBIT_TRADE_URL = "https://www.bybit.com/trade/usdt/{symbol}"
