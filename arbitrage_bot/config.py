"""
config.py ─ All configurable parameters for the Funding Rate Arbitrage Bot.
Edit these or override via .env file.
"""
import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    # ── API ───────────────────────────────────────────────────────────────────
    API_KEY    = os.getenv("BINANCE_API_KEY", "")
    API_SECRET = os.getenv("BINANCE_API_SECRET", "")
    USE_TESTNET = os.getenv("USE_TESTNET", "false").lower() == "true"

    # Endpoints (auto-selected based on testnet flag)
    FUTURES_BASE = ("https://testnet.binancefuture.com"
                    if USE_TESTNET else "https://fapi.binance.com")
    SPOT_BASE    = ("https://testnet.binance.vision"
                    if USE_TESTNET else "https://api.binance.com")

    # ── Strategy ──────────────────────────────────────────────────────────────
    MAX_POSITIONS             = 10          # Top N coins to trade
    DEFAULT_LEVERAGE          = 1           # 1x for both legs
    ENTRY_BEFORE_SECONDS      = 300         # Enter 5 min before funding (seconds)
    MIN_FUNDING_RATE          = -0.0001     # Only trade if next rate < −0.01 %
    EXIT_SPREAD_THRESHOLD     = 0.10        # Exit when spread ≤ 0.10 % (≈ 0)
    STOP_LOSS_PCT             = 5.0         # Close if unrealised loss > 5 %
    MIN_POSITION_USDT         = 15.0        # Minimum notional per coin

    # After the first funding settlement you may exit if spread closes
    # (this is the "Recommended min holding period" logic).
    # We hold until the NEXT funding timestamp has passed (≥ 1 full cycle).
    MIN_HOLD_EXTRA_SECONDS    = 0           # Extra buffer after first funding

    # ── Spot Mode ─────────────────────────────────────────────────────────────
    # 'margin'       → Long futures + Short spot via cross-margin (delta-neutral)
    # 'futures_only' → Long futures only (simpler, not delta-neutral)
    SPOT_MODE = os.getenv("SPOT_MODE", "margin")

    # ── Timing ────────────────────────────────────────────────────────────────
    LOOP_INTERVAL_SECONDS    = 30           # Main monitoring loop cadence
    SPREAD_SNAPSHOT_INTERVAL = 60           # How often to record spread to DB

    # ── Dashboard ─────────────────────────────────────────────────────────────
    DASHBOARD_PORT           = int(os.getenv("DASHBOARD_PORT", 8050))
    DASHBOARD_REFRESH_MS     = 10_000       # Auto-refresh interval

    # ── Persistence ───────────────────────────────────────────────────────────
    DB_PATH = "arbitrage_bot.db"
    LOG_FILE = "arbitrage_bot.log"
