"""SQLite database helpers – price logs + per-user settings."""

import sqlite3
import time
from config import DB_PATH, RETENTION_HOURS


def _conn() -> sqlite3.Connection:
    """Return a new connection with WAL mode for concurrent reads."""
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA journal_mode=WAL")
    con.row_factory = sqlite3.Row
    return con


# ── Schema ────────────────────────────────────────────────


def init_db() -> None:
    """Create tables and indices if they don't exist."""
    with _conn() as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS price_log (
                symbol    TEXT    NOT NULL,
                price     REAL    NOT NULL,
                timestamp REAL    NOT NULL
            )
        """
        )
        con.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_price_log_sym_ts
            ON price_log (symbol, timestamp)
        """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id            INTEGER PRIMARY KEY,
                scan_frequency     INTEGER DEFAULT 30,
                pump_threshold     REAL    DEFAULT 10.0,
                pump_time_window   INTEGER DEFAULT 15,
                dump_threshold     REAL    DEFAULT 10.0,
                dump_time_window   INTEGER DEFAULT 15,
                is_paused          INTEGER DEFAULT 0,
                is_setup_complete  INTEGER DEFAULT 0
            )
        """
        )

        # ── Migrate old schema if needed ──────────────────
        # Check existing columns
        cols = {
            row[1] for row in con.execute("PRAGMA table_info(user_settings)").fetchall()
        }
        migrations = {
            "dump_threshold": "ALTER TABLE user_settings ADD COLUMN dump_threshold    REAL    DEFAULT 10.0",
            "dump_time_window": "ALTER TABLE user_settings ADD COLUMN dump_time_window  INTEGER DEFAULT 15",
            "pump_time_window": "ALTER TABLE user_settings ADD COLUMN pump_time_window  INTEGER DEFAULT 15",
        }
        for col, sql in migrations.items():
            if col not in cols:
                con.execute(sql)

        # Rename old 'time_window' → copy value to 'pump_time_window' if it exists
        if "time_window" in cols and "pump_time_window" in cols:
            con.execute(
                """
                UPDATE user_settings
                SET pump_time_window = time_window
                WHERE pump_time_window = 15 AND time_window != 15
            """
            )


# ── Price Log CRUD ────────────────────────────────────────


def save_prices(prices: list[tuple[str, float]]) -> None:
    """Bulk-insert (symbol, price) rows with the current timestamp."""
    ts = time.time()
    rows = [(sym, px, ts) for sym, px in prices]
    with _conn() as con:
        con.executemany(
            "INSERT INTO price_log (symbol, price, timestamp) VALUES (?, ?, ?)",
            rows,
        )


def get_price_at(symbol: str, target_ts: float) -> float | None:
    """Return the price closest to *target_ts* for *symbol* (within ±120 s)."""
    with _conn() as con:
        row = con.execute(
            """
            SELECT price FROM price_log
            WHERE symbol = ? AND timestamp BETWEEN ? AND ?
            ORDER BY ABS(timestamp - ?) ASC
            LIMIT 1
            """,
            (symbol, target_ts - 120, target_ts + 120, target_ts),
        ).fetchone()
    return row["price"] if row else None


def get_all_symbols_price_at(target_ts: float) -> dict[str, float]:
    """Return {symbol: price} closest to *target_ts* for every symbol."""
    with _conn() as con:
        rows = con.execute(
            """
            SELECT symbol, price,
                   MIN(ABS(timestamp - ?)) AS dist
            FROM price_log
            WHERE timestamp BETWEEN ? AND ?
            GROUP BY symbol
            """,
            (target_ts, target_ts - 120, target_ts + 120),
        ).fetchall()
    return {r["symbol"]: r["price"] for r in rows}


def purge_old(hours: int = RETENTION_HOURS) -> int:
    """Delete price_log rows older than *hours*. Return deleted count."""
    cutoff = time.time() - hours * 3600
    with _conn() as con:
        cur = con.execute("DELETE FROM price_log WHERE timestamp < ?", (cutoff,))
        return cur.rowcount


# ── User Settings CRUD ───────────────────────────────────


def upsert_user(user_id: int, **kwargs) -> None:
    """Insert or update user settings.  Pass only the columns to set."""
    with _conn() as con:
        # Ensure the row exists
        con.execute(
            "INSERT OR IGNORE INTO user_settings (user_id) VALUES (?)",
            (user_id,),
        )
        if kwargs:
            sets = ", ".join(f"{k} = ?" for k in kwargs)
            vals = list(kwargs.values()) + [user_id]
            con.execute(
                f"UPDATE user_settings SET {sets} WHERE user_id = ?",
                vals,
            )


def get_user(user_id: int) -> dict | None:
    """Return user settings as a dict, or None."""
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM user_settings WHERE user_id = ?", (user_id,)
        ).fetchone()
    return dict(row) if row else None


def get_all_active_users() -> list[dict]:
    """Return all users where paused=0 and setup complete."""
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM user_settings WHERE is_paused = 0 AND is_setup_complete = 1"
        ).fetchall()
    return [dict(r) for r in rows]
