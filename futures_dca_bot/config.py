import os
from pathlib import Path

# ============================================================
#  Futures DCA Bot — Configuration
# ============================================================
# --- Environment ---
BASE_DIR = Path(__file__).resolve().parent


def _load_dotenv(path: str | Path = ".env") -> None:
    env_path = Path(path)
    if not env_path.is_absolute():
        env_path = BASE_DIR / env_path
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _env_bool(*names: str, default: str = "false") -> bool:
    for name in names:
        value = os.getenv(name)
        if value is not None:
            return value.lower() in ("1", "true", "yes", "on")
    return default.lower() in ("1", "true", "yes", "on")


_load_dotenv()

# --- API Credentials ---
USE_TESTNET = _env_bool("USE_TESTNET", "DELTA_USE_TESTNET", default="true")
if USE_TESTNET:
    API_KEY = os.getenv("DELTA_TESTNET_API_KEY") or os.getenv("DELTA_API_KEY", "")
    API_SECRET = os.getenv("DELTA_TESTNET_API_SECRET") or os.getenv("DELTA_API_SECRET", "")
else:
    API_KEY = os.getenv("DELTA_API_KEY", "")
    API_SECRET = os.getenv("DELTA_API_SECRET", "")

# --- Telegram Alerts ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN", "")  # leave "" to disable
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
# ============================================================
#  Trading Pair
# ============================================================
SYMBOL      = "BTCUSD"
LEVERAGE    = 5
MARGIN_TYPE = "ISOLATED"   # "ISOLATED" or "CROSSED"

# ============================================================
#  DCA Parameters
# ============================================================
PRICE_DEVIATION       = 0.5    # % drop/rise from last order to trigger next DCA
TAKE_PROFIT_PCT       = 1.0    # % above avg entry to close the round (long) / below (short)
STOP_LOSS_PCT         = 2.0    # % below avg entry (long) / above (short) to stop
MAX_DCA_ORDERS        = 8      # max additional DCA entries per round

BASE_ORDER_USDT       = 12.0   # initial order margin (USDT)
DCA_ORDER_USDT        = 12.0   # first DCA order margin (USDT); multiplied each level
DCA_SIZE_MULTIPLIER   = 1.1    # each successive DCA is this × larger
PRICE_DEV_MULTIPLIER  = 1.0    # multiplier on price deviation per level (1 = flat 0.5% always)
WALLET_DEPLOY_PCT     = 0.50   # Max futures wallet balance allotted to one DCA round
MIN_DCA_ORDER_USDT    = 5.0    # Do not place capped leftover DCA orders below this margin

# Optional controls
START_CONDITION       = "INSTANT"  # "INSTANT" or "TRIGGER_PRICE"
START_TRIGGER_PRICE   = None       # e.g. 82500.0; LONG starts at/above, SHORT at/below
STOP_CONDITION        = "NONE"     # "NONE", "END_AFTER_ROUND", or "TRIGGER_PRICE"
STOP_TRIGGER_PRICE    = None       # e.g. 78000.0; LONG stops at/below, SHORT at/above

# ============================================================
#  Entry Signal - EMA + Bollinger squeeze + ATR on closed candles
# ============================================================
EMA_FAST        = 9
EMA_SLOW        = 21
TIMEFRAME       = "4h"          # kline interval for signal
LOOKBACK_CANDLES = 120          # candles to pull for indicator calculation

BB_PERIOD       = 14            # Bollinger Band period for squeeze detection
BB_STD_DEV      = 2.0           # Bollinger Band standard deviations
BB_SQUEEZE_THRESHOLD = 0.015    # bandwidth below this -> wait, no DCA entry

ATR_LENGTH      = 14            # ATR length, calculated for logs/future risk sizing
ATR_MULTIPLIER  = 1.5           # ATR multiplier for signal context

# ============================================================
#  Strategy Switches
# ============================================================
ALLOW_LONG  = True
ALLOW_SHORT = True

# how often (seconds) to poll for a new signal when idle
SIGNAL_POLL_INTERVAL = 60

# how often (seconds) to check the active round for fills
ROUND_POLL_INTERVAL  = 5

# ============================================================
#  Persistence
# ============================================================
DB_PATH        = str(BASE_DIR / "dca_bot.db")
LOG_FILE       = str(BASE_DIR / "dca_bot.log")
