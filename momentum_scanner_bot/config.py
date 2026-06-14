# ─────────────────────────────────────────────
#  momentum_scanner / config.py
# ─────────────────────────────────────────────

# ── Binance credentials (Futures read-only key is enough) ──
API_KEY    = "YOUR_API_KEY"
API_SECRET = "YOUR_API_SECRET"

# ── Telegram ──
TELEGRAM_BOT_TOKEN = "YOUR_BOT_TOKEN"
TELEGRAM_CHAT_ID   = "YOUR_CHAT_ID"

# ── REST base ──
BASE_URL  = "https://fapi.binance.com"
WS_BASE   = "wss://fstream.binance.com"

# ── Coin whitelist (your 533-coin list; leave empty [] to scan ALL perp futures) ──
WHITELIST: list[str] = []          # e.g. ["BTCUSDT", "ETHUSDT", ...]

# ────────────────────────────────────────────
#  SIGNAL THRESHOLDS  (tune after live testing)
# ────────────────────────────────────────────

# Signal 1 – OI spike
OI_CHANGE_THRESHOLD_PCT     = 3.0    # % OI change in one 5-min candle
OI_LOOKBACK_CANDLES         = 1      # compare current vs N candles ago

# Signal 1 – Funding rate shift (paired with OI)
FUNDING_SHIFT_THRESHOLD     = 0.0001  # absolute change in funding rate

# Signal 2 – Liquidation cascade
LIQ_WINDOW_SECONDS          = 60
LIQ_VALUE_USD_THRESHOLD     = 50_000  # $ liquidated in window → trigger

# Signal 3 – Order book thinning
DEPTH_LEVELS                = 20      # top-N ask levels to sum
DEPTH_THIN_RATIO            = 0.60    # current/avg_10m < this → thin
DEPTH_HISTORY_MINUTES       = 10

# Signal 4 – Taker buy/sell imbalance
TAKER_BUY_BULL_THRESHOLD    = 0.65   # ratio > this  → bullish
TAKER_BUY_BEAR_THRESHOLD    = 0.35   # ratio < this  → bearish
TAKER_PERIOD                = "5m"

# Signal 5 – Volume vs rolling avg
VOLUME_SPIKE_MULTIPLIER     = 2.5    # current_vol / avg_20 > this
VOLUME_LOOKBACK_CANDLES     = 21     # 20 historical + 1 current

# Signal 6 – Price velocity (rate of change)
ROC_CANDLES                 = 3      # look back N 5m candles
ROC_THRESHOLD_PCT           = 1.5    # % price change over N candles
ROC_ACCELERATION_REQUIRED   = True   # each candle must be > previous

# ── Confluence ──
MIN_SIGNALS_FOR_ALERT       = 3      # fire when this many signals agree
SIGNAL_WINDOW_SECONDS       = 120    # signals must fire within this window

# ── Scanning ──
POLL_INTERVAL_SECONDS       = 30     # REST poll cadence (per coin)
MAX_CONCURRENT_SCANS        = 20     # parallel aiohttp requests
KLINE_INTERVAL              = "5m"

# ── Risk / Position ──
LEVERAGE                    = 5
CAPITAL_PER_TRADE_USD       = 100    # per signal alert
STOP_LOSS_PCT               = 1.5    # % below entry
TAKE_PROFIT_PCT             = 3.0    # % above entry
