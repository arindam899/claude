"""
database.py — SQLite persistence layer for the Futures DCA Bot.

Tables
──────
sessions : one row per DCA round (open or closed)
dca_fills : individual fill records (base + each DCA entry)
"""

import sqlite3
from config import DB_PATH


# ──────────────────────────────────────────────────────────────
#  Schema
# ──────────────────────────────────────────────────────────────

_CREATE_SESSIONS = """
CREATE TABLE IF NOT EXISTS sessions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol           TEXT    NOT NULL,
    direction        TEXT    NOT NULL,   -- LONG | SHORT
    start_time       TEXT    NOT NULL,
    end_time         TEXT,
    status           TEXT    DEFAULT 'RUNNING',  -- RUNNING | CLOSED
    base_entry_price REAL,
    avg_entry_price  REAL,
    total_quantity   REAL,
    total_margin     REAL,
    dca_count        INTEGER DEFAULT 0,
    realized_pnl     REAL    DEFAULT 0.0,
    close_reason     TEXT    -- TP | SL | MANUAL
);
"""

_CREATE_DCA_FILLS = """
CREATE TABLE IF NOT EXISTS dca_fills (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER NOT NULL,
    fill_type   TEXT,   -- BASE | DCA_1 … DCA_8
    binance_oid TEXT,
    fill_price  REAL,
    quantity    REAL,
    margin_usdt REAL,
    timestamp   TEXT,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);
"""


def init_db():
    """Create tables if they don't exist."""
    with _conn() as conn:
        conn.execute(_CREATE_SESSIONS)
        conn.execute(_CREATE_DCA_FILLS)


def _conn():
    return sqlite3.connect(DB_PATH)


# ──────────────────────────────────────────────────────────────
#  Sessions helpers
# ──────────────────────────────────────────────────────────────

def insert_session(symbol, direction, start_time,
                   base_price, qty, margin):
    with _conn() as conn:
        cur = conn.execute(
            """INSERT INTO sessions
               (symbol, direction, start_time, status,
                base_entry_price, avg_entry_price,
                total_quantity, total_margin, dca_count)
               VALUES (?,?,?,?,?,?,?,?,0)""",
            (symbol, direction, start_time, 'RUNNING',
             base_price, base_price, qty, margin)
        )
        return cur.lastrowid


def update_session(session_id, **kwargs):
    """Update arbitrary columns on a session row."""
    if not kwargs:
        return
    cols = ", ".join(f"{k}=?" for k in kwargs)
    vals = list(kwargs.values()) + [session_id]
    with _conn() as conn:
        conn.execute(f"UPDATE sessions SET {cols} WHERE id=?", vals)


def close_session(session_id, end_time, pnl, reason):
    update_session(session_id,
                   status='CLOSED',
                   end_time=end_time,
                   realized_pnl=pnl,
                   close_reason=reason)


def get_all_sessions():
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM sessions ORDER BY start_time DESC"
        ).fetchall()
    return [dict(r) for r in rows]


# ──────────────────────────────────────────────────────────────
#  DCA fill helpers
# ──────────────────────────────────────────────────────────────

def insert_fill(session_id, fill_type, binance_oid,
                fill_price, quantity, margin_usdt, timestamp):
    with _conn() as conn:
        conn.execute(
            """INSERT INTO dca_fills
               (session_id, fill_type, binance_oid,
                fill_price, quantity, margin_usdt, timestamp)
               VALUES (?,?,?,?,?,?,?)""",
            (session_id, fill_type, binance_oid,
             fill_price, quantity, margin_usdt, timestamp)
        )


def get_fills(session_id):
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM dca_fills WHERE session_id=? ORDER BY id",
            (session_id,)
        ).fetchall()
    return [dict(r) for r in rows]
