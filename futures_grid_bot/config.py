"""
╔══════════════════════════════════════════════════════════════════╗
║     ADVANCED FUTURES GRID CONTROLLER — CONFIG                    ║
║     Edit ONLY this file to configure the entire system           ║
╚══════════════════════════════════════════════════════════════════╝
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def _load_local_env() -> None:
    """Load futures_grid_bot/.env without overriding already exported values."""
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


_load_local_env()

# ─── API ────────────────────────────────────────────────────────────
TESTNET    = _env_bool("USE_TESTNET", _env_bool("DELTA_TESTNET", True))
DELTA_BASE_URL = os.environ.get(
    "DELTA_BASE_URL",
    "https://cdn-ind.testnet.deltaex.org" if TESTNET else "https://api.india.delta.exchange",
)
API_KEY    = os.environ.get("DELTA_TESTNET_API_KEY" if TESTNET else "DELTA_API_KEY", "") or os.environ.get("DELTA_API_KEY", "")
API_SECRET = os.environ.get("DELTA_TESTNET_API_SECRET" if TESTNET else "DELTA_API_SECRET", "") or os.environ.get("DELTA_API_SECRET", "")

# ─── SYMBOL ─────────────────────────────────────────────────────────
SYMBOL       = "ETHUSD"            # Delta India perpetual
QUOTE_ASSET  = "USD"              # Delta India futures margin asset

# ─── CAPITAL MANAGEMENT ─────────────────────────────────────────────
CAPITAL_DEPLOY_PCT = 0.50         # Deploy 50% of futures wallet balance in grid

# ─── REGIME DETECTION THRESHOLDS ────────────────────────────────────
EMA_FAST_PERIOD    = 9            # fast EMA for direction
EMA_SLOW_PERIOD    = 21           # slow EMA for direction
BB_SQUEEZE_THRESHOLD = 0.015      # Bandwidth below this → squeeze (pre-breakout)
BB_PERIOD        = 14             # Bollinger Band period for squeeze detection
BB_STD_DEV       = 2.0            # Bollinger Band standard deviations
ATR_LENGTH       = 14             # ATR length for grid upper/lower range
ATR_MULTIPLIER   = 1.5            # ATR multiplier for stop/range lines
KLINE_INTERVAL     = "4h"         # Candle interval for EMA direction
LOOKBACK_CANDLES   = 120          # How many candles to pull for indicator calc

# ─── GRID PARAMETERS ────────────────────────────────────────────────
GRID_MODE          = "ARITHMETIC" # "ARITHMETIC" | "GEOMETRIC"
LEVERAGE_NEUTRAL   = 5            # Leverage for neutral grid (safest)
LEVERAGE_LONG      = 5            # Leverage for long grid
LEVERAGE_SHORT     = 5            # Leverage for short grid

#  Range sizing  ─ set by regime, in % of current price
RANGE_PCT_NEUTRAL  = 0.06         # ±3% each side (6% total)
RANGE_PCT_TRENDING = 0.08         # ±4% each side (8% total) for directional

#  Grids count  ─ set by volatility regime
GRIDS_HIGH_VOL     = 24           # High volatility: more grids
GRIDS_LOW_VOL      = 12           # Low volatility: fewer grids
VOL_HIGH_THRESHOLD = 0.02         # 24h ATR/price > 2% = high volatility


# ─── TRAILING ────────────────────────────────────────────────────────
TRAILING_ENABLED        = True
TRAILING_TRIGGER_PCT    = 0.025   # Start trailing when price moves 2.5% past range
TRAILING_STEP_PCT       = 0.015   # Shift grid by 1.5% steps when trailing

# ─── TAKE PROFIT / STOP LOSS ─────────────────────────────────────────
TP_ENABLED             = True
TP_PCT_ABOVE_UPPER     = 0.015    # TP = 1.5% beyond grid range
SL_ENABLED             = True
SL_PCT_BELOW_LOWER     = 0.015    # SL = 1.5% beyond grid range
# For SHORT grid: SL is above upper, TP is below lower by the same buffers

# ─── GRID TRIGGER ────────────────────────────────────────────────────
GRID_TRIGGER_ENABLED   = False
# False = start immediately like creating an exchange grid with "Open a position
# on creation" enabled. Set True if you want to wait for a trigger zone.

# ─── POSITION LIFECYCLE ──────────────────────────────────────────────
OPEN_ON_CREATION       = True     # Open initial position at market on start
CLOSE_ALL_ON_STOP      = True     # Close all positions when grid stops

# ─── TIMING ──────────────────────────────────────────────────────────
POLL_INTERVAL_SEC      = 15       # Main loop cadence
SIGNAL_CHECK_INTERVAL  = 60       # Re-check EMA cross

# ─── TELEGRAM ────────────────────────────────────────────────────────
TELEGRAM_TOKEN         = os.environ.get("TELEGRAM_BOT_TOKEN", os.environ.get("TELEGRAM_TOKEN", ""))
TELEGRAM_CHAT_ID       = os.environ.get("TELEGRAM_CHAT_ID", "")
LOG_FILE               = str(BASE_DIR / "grid_controller.log")

