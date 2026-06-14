"""
main.py ─ Entry point.

Starts the ArbitrageBot in a daemon thread, then launches the Dash dashboard
on the main thread (blocking).

Usage:
    python main.py
    
Dashboard will be accessible at http://localhost:8050
"""

import sys
import logging
import threading

from config import Config
from bot import ArbitrageBot
from dashboard import app, inject_bot

# ─────────────────────────────────────────────────────────────────────────────
# Logging setup
# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(Config.LOG_FILE, encoding="utf-8"),
    ],
)

logger = logging.getLogger("main")

# ─────────────────────────────────────────────────────────────────────────────


def main():
    if not Config.API_KEY or not Config.API_SECRET:
        logger.error(
            "BINANCE_API_KEY and BINANCE_API_SECRET must be set in .env  "
            "(copy .env.example -> .env and fill in your keys)."
        )
        sys.exit(1)

    logger.info("=" * 60)
    logger.info("   Funding Rate Arbitrage Bot")
    logger.info(f"   Mode      : {'TESTNET' if Config.USE_TESTNET else 'LIVE'}")
    logger.info(f"   Spot mode : {Config.SPOT_MODE}")
    logger.info(f"   Max coins : {Config.MAX_POSITIONS}")
    logger.info(f"   Leverage  : {Config.DEFAULT_LEVERAGE}x")
    logger.info(f"   Entry     : {Config.ENTRY_BEFORE_SECONDS // 60} min before funding")
    logger.info(f"   Exit at   : spread <= {Config.EXIT_SPREAD_THRESHOLD}%")
    logger.info("=" * 60)

    bot = ArbitrageBot()
    inject_bot(bot)

    # Run bot in a background daemon thread
    bot_thread = threading.Thread(target=bot.start, name="ArbitrageBot", daemon=True)
    bot_thread.start()
    logger.info("Bot thread started.")

    # Start dashboard (blocking main thread)
    logger.info(f"Dashboard -> http://localhost:{Config.DASHBOARD_PORT}")
    app.run_server(
        debug=False,
        host="0.0.0.0",
        port=Config.DASHBOARD_PORT,
        use_reloader=False,   # must be False when embedding in a thread
    )


if __name__ == "__main__":
    main()
