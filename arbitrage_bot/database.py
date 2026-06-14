"""
database.py ─ SQLite persistence layer.
Thread-safe access via a Lock; all state survives bot restarts.
"""
import sqlite3
import threading
import time
import logging
from datetime import datetime
from config import Config

logger = logging.getLogger(__name__)


class Database:
    def __init__(self):
        self.path  = Config.DB_PATH
        self._lock = threading.Lock()
        self._init()

    # ── Schema ────────────────────────────────────────────────────────────────

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self):
        with self._lock, self._conn() as conn:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS positions (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol              TEXT    NOT NULL,
                status              TEXT    DEFAULT 'open',

                -- Entry
                entry_time          REAL,
                next_funding_time   REAL,   -- epoch seconds of first funding after entry
                entry_spread        REAL,   -- (futures - spot) / spot * 100
                position_usdt       REAL,   -- notional size
                qty_futures         REAL,
                qty_spot            REAL    DEFAULT 0,
                futures_entry_price REAL,
                spot_entry_price    REAL,
                futures_order_id    TEXT,
                margin_order_id     TEXT,

                -- Live (updated during monitoring)
                current_spread      REAL,
                current_funding_rate REAL,
                funding_collected   REAL    DEFAULT 0,

                -- Exit
                exit_time           REAL,
                exit_spread         REAL,
                spread_pnl          REAL    DEFAULT 0,
                total_pnl           REAL    DEFAULT 0,
                close_reason        TEXT
            );

            CREATE TABLE IF NOT EXISTS spread_history (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol    TEXT,
                spread    REAL,
                ts        REAL
            );

            CREATE TABLE IF NOT EXISTS bot_log (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                level     TEXT,
                message   TEXT,
                ts        REAL
            );
            """)

    # ── Positions ─────────────────────────────────────────────────────────────

    def open_position(self, p: dict):
        sql = """INSERT INTO positions
            (symbol, entry_time, next_funding_time, entry_spread,
             position_usdt, qty_futures, qty_spot,
             futures_entry_price, spot_entry_price,
             futures_order_id, margin_order_id,
             current_spread, current_funding_rate)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"""
        vals = (
            p["symbol"], p["entry_time"], p["next_funding_time"], p["entry_spread"],
            p["position_usdt"], p["qty_futures"], p.get("qty_spot", 0),
            p["futures_entry_price"], p.get("spot_entry_price", 0),
            str(p.get("futures_order_id", "")),
            str(p.get("margin_order_id", "")),
            p["entry_spread"], 0.0,
        )
        with self._lock, self._conn() as conn:
            conn.execute(sql, vals)

    def update_live(self, symbol: str, current_spread: float,
                    current_funding_rate: float, funding_collected: float):
        sql = """UPDATE positions
                 SET current_spread=?, current_funding_rate=?, funding_collected=?
                 WHERE symbol=? AND status='open'"""
        with self._lock, self._conn() as conn:
            conn.execute(sql, (current_spread, current_funding_rate,
                               funding_collected, symbol))

    def close_position(self, symbol: str, d: dict):
        sql = """UPDATE positions
                 SET status='closed', exit_time=?, exit_spread=?,
                     spread_pnl=?, funding_collected=?, total_pnl=?,
                     close_reason=?
                 WHERE symbol=? AND status='open'"""
        with self._lock, self._conn() as conn:
            conn.execute(sql, (
                d["exit_time"], d["exit_spread"],
                d["spread_pnl"], d["funding_collected"], d["total_pnl"],
                d.get("reason", "spread_closed"), symbol,
            ))

    def get_open_positions(self) -> list:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM positions WHERE status='open' ORDER BY entry_time"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_closed_positions(self, limit: int = 50) -> list:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM positions WHERE status='closed' ORDER BY exit_time DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def symbol_is_open(self, symbol: str) -> bool:
        with self._conn() as conn:
            r = conn.execute(
                "SELECT 1 FROM positions WHERE symbol=? AND status='open'", (symbol,)
            ).fetchone()
        return r is not None

    def get_stats(self) -> dict:
        with self._conn() as conn:
            total_pnl = conn.execute(
                "SELECT COALESCE(SUM(total_pnl),0) FROM positions WHERE status='closed'"
            ).fetchone()[0]
            total_funding = conn.execute(
                "SELECT COALESCE(SUM(funding_collected),0) FROM positions"
            ).fetchone()[0]
            n_closed = conn.execute(
                "SELECT COUNT(*) FROM positions WHERE status='closed'"
            ).fetchone()[0]
            n_open = conn.execute(
                "SELECT COUNT(*) FROM positions WHERE status='open'"
            ).fetchone()[0]
        return {
            "total_pnl": total_pnl,
            "total_funding": total_funding,
            "n_closed": n_closed,
            "n_open": n_open,
        }

    # ── Spread History ────────────────────────────────────────────────────────

    def record_spread(self, symbol: str, spread: float):
        with self._lock, self._conn() as conn:
            conn.execute(
                "INSERT INTO spread_history (symbol, spread, ts) VALUES (?,?,?)",
                (symbol, spread, time.time()),
            )

    def get_spread_history(self, hours: float = 24, symbol: str = None) -> list:
        since = time.time() - hours * 3600
        with self._conn() as conn:
            if symbol:
                rows = conn.execute(
                    "SELECT symbol, spread, ts FROM spread_history "
                    "WHERE symbol=? AND ts>? ORDER BY ts",
                    (symbol, since),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT symbol, spread, ts FROM spread_history "
                    "WHERE ts>? ORDER BY ts",
                    (since,),
                ).fetchall()
        return [
            {
                "symbol": r["symbol"],
                "spread": r["spread"],
                "dt":     datetime.fromtimestamp(r["ts"]).strftime("%H:%M:%S"),
            }
            for r in rows
        ]

    # ── Logging ───────────────────────────────────────────────────────────────

    def log(self, level: str, message: str):
        with self._lock, self._conn() as conn:
            conn.execute(
                "INSERT INTO bot_log (level, message, ts) VALUES (?,?,?)",
                (level, message, time.time()),
            )

    def get_recent_logs(self, limit: int = 100) -> list:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT level, message, ts FROM bot_log ORDER BY ts DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "level":   r["level"],
                "message": r["message"],
                "time":    datetime.fromtimestamp(r["ts"]).strftime("%H:%M:%S"),
            }
            for r in rows
        ]
