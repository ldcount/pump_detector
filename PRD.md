# Pump Detector – PRD

A Python Telegram bot that monitors **all Bybit USDT-margined perpetual futures** for extreme pump and dump events, alerting users in real time.

## Core functionality

1. **Global price scanning** – The bot fetches mark prices for every USDT perp on Bybit every 30 seconds (global tick). Prices are logged in a local SQLite database.
2. **Pump & dump detection** – After each scan, the bot computes the percentage price change over each user's configured time window. Positive moves trigger pump alerts; negative moves trigger dump alerts.
3. **Per-user alerting** – Each user configures their own thresholds independently. Alerts are sent only when a symbol's move exceeds the user's threshold for the corresponding direction.

## Alert format

```
🟢Pump - <time_window>m: <COIN_LINK>: <percentage>%
🔴Dump - <time_window>m: <COIN_LINK>: <percentage>%
```

- `<COIN_LINK>` is a clickable link to the symbol's Bybit perp trading page (no link preview).
- `<time_window>` reflects the user's configured pump or dump time window.
- Each qualifying symbol is sent as a separate message.

## User parameters (per user, set via 5-step setup)

| # | Parameter | Options | Default |
|---|-----------|---------|---------|
| 1 | Scan frequency (seconds) | Any integer ≥ 30 | 30 |
| 2 | Pump threshold (%) | 10, 20, 30, 50, 80 | 10 |
| 3 | Pump time window (min) | 5, 10, 15, 20, 30, 40, 60 | 15 |
| 4 | Dump threshold (%) | 10, 20, 30, 50, 80 | 10 |
| 5 | Dump time window (min) | 5, 10, 15, 20, 30, 40, 60 | 15 |

## Bot commands

| Command | Description |
|---------|-------------|
| `/start` | 5-step initial setup (frequency → pump threshold → pump window → dump threshold → dump window) |
| `/param` | Re-configure all parameters (same flow as /start) |
| `/status` | Display current settings |
| `/pause` | Pause alerts for the user |
| `/resume` | Resume alerts |
| `/help` | Show help text |

## Technical guidelines

- **Price basis:** mark price via Bybit API.
- **Formula:** `change_pct = (current_price / price_X_minutes_ago - 1) * 100`
- **Alert condition:** pump if `change_pct >= pump_threshold`; dump if `change_pct <= -dump_threshold`.
- **Architecture:** single global collector → compute price changes per symbol/window → fan-out alerts to matching users.
- **Alert cooldown:** 5 minutes per user/symbol/direction to avoid duplicates.
- **Storage:** SQLite (WAL mode). Price history retained for 72 hours, then purged.
- **Logging:** errors, start/stop, pause/resume only.

## Tech stack

- `python-telegram-bot[job-queue]` – Telegram integration
- `pybit` – Bybit API access
- `python-dotenv` – secrets management
- SQLite – local database
- Python venv (no Docker); intended for systemd deployment

## Secrets

Bot API token stored in `.env` (not committed to git).
