"""
bot.py ─ Core strategy & execution engine.

Strategy (Negative Funding Rate Arbitrage):
  • Negative funding → shorts PAY longs.
  • Position: LONG Futures Perpetual + SHORT Spot (cross-margin).
  • Entry: 5 minutes before the next funding settlement.
  • Exit:  current spread ≤ EXIT_SPREAD_THRESHOLD  AND
           the first funding settlement has already passed
           (i.e., at least one funding payment collected).
  • Leverage: 1× on both legs (delta-neutral, no directional risk).

Capital allocation:
  • total_usdt = spot_free_USDT + futures_available_USDT
  • per_coin   = total_usdt / MAX_POSITIONS   (= /10 by default)

References:
  https://www.binance.com/en/support/faq/detail/f330e17d6fc04679b9b21d6f9350e787
  https://www.binance.com/en-IN/blog/tech/3611863022773164727
  https://www.binance.com/en/support/faq/detail/2c65b90111e14be6b0156d32e0ff94d9
"""

import time
import logging
import threading
from datetime import datetime
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler

from config import Config
from api_client import BinanceClient
from database import Database

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────


class ArbitrageBot:
    """Manages data fetching, order scheduling, monitoring, and position exit."""

    def __init__(self):
        self.client     = BinanceClient()
        self.db         = Database()
        self.scheduler  = BackgroundScheduler(timezone="UTC")
        self.is_running = False

        # Live data caches (updated each loop)
        self._funding_data: list = []          # all top-N opps
        self._last_balances: dict = {}
        self._lock = threading.Lock()

        # Reload open positions from DB on restart
        self._restore_positions()

    # ── Startup / Shutdown ────────────────────────────────────────────────────

    def _restore_positions(self):
        """Re-populate in-memory set from DB (bot restart recovery)."""
        open_rows = self.db.get_open_positions()
        logger.info(f"Restored {len(open_rows)} open position(s) from DB.")

    def start(self):
        self.is_running = True
        self.scheduler.start()
        logger.info("ArbitrageBot started.")
        self._main_loop()

    def stop(self):
        self.is_running = False
        try:
            self.scheduler.shutdown(wait=False)
        except Exception:
            pass
        logger.info("ArbitrageBot stopped.")

    # ── Market Data ───────────────────────────────────────────────────────────

    def get_funding_data(self) -> list:
        """
        Fetch all USDT-M perpetual funding data, compute spread,
        and return sorted by most-negative funding rate.

        next_funding_rate is approximated from lastFundingRate (Binance
        does not expose the true estimate via public REST; the websocket
        markPriceStream carries 'estimatedSettlePriceForNextFundingRate'
        which this bot does not yet consume).
        """
        raw = self.client.get_premium_index()          # list of all perps
        if not raw:
            return []

        # Build price maps
        f_tickers_raw = self.client.get_futures_book_ticker() or []
        s_tickers_raw = self.client.get_spot_book_ticker()    or []

        f_tickers = {t["symbol"]: t for t in f_tickers_raw}
        s_tickers = {t["symbol"]: t for t in s_tickers_raw}

        results = []
        for item in raw:
            sym = item.get("symbol", "")
            if not sym.endswith("USDT"):
                continue
            if sym not in f_tickers or sym not in s_tickers:
                continue

            funding_rate   = float(item.get("lastFundingRate", 0))
            next_fund_ms   = int(item.get("nextFundingTime", 0))
            mark_price     = float(item.get("markPrice",  0))
            index_price    = float(item.get("indexPrice", 0))

            if index_price <= 0:
                continue

            # Best ask for entry (we buy futures ask, sell spot bid)
            f_ask = float(f_tickers[sym]["askPrice"])
            s_bid = float(s_tickers[sym]["bidPrice"])

            if f_ask <= 0 or s_bid <= 0:
                continue

            # Spread: positive ⟹ futures at premium
            spread_pct = (f_ask - s_bid) / s_bid * 100.0

            # Annualise: 3 settlements/day × 365
            apr_pct = funding_rate * 3 * 365 * 100.0

            # Breakeven holding days (how long to recoup entry spread via funding)
            daily_rate = abs(funding_rate) * 3
            breakeven_days = (abs(spread_pct) / 100.0 / daily_rate) if daily_rate else 999

            # Recommended min hold (in seconds): at least until next settlement
            # plus any extra buffer; mirrors Binance's "Recommended min holding period"
            min_hold_s = max(
                next_fund_ms / 1000 - time.time() + Config.MIN_HOLD_EXTRA_SECONDS,
                0,
            )

            results.append({
                "symbol":           sym,
                "base":             sym.replace("USDT", ""),
                "funding_rate":     funding_rate,
                "funding_rate_pct": funding_rate * 100.0,
                "apr_pct":          apr_pct,
                "next_funding_ms":  next_fund_ms,
                "spread_pct":       spread_pct,
                "mark_price":       mark_price,
                "index_price":      index_price,
                "futures_ask":      f_ask,
                "spot_bid":         s_bid,
                "breakeven_days":   breakeven_days,
                "min_hold_s":       min_hold_s,
            })

        # Sort: most negative first
        results.sort(key=lambda x: x["funding_rate"])
        return results

    def get_top_opportunities(self) -> list:
        """Filter to top N tradable opportunities (negative rate, not already open)."""
        open_syms = {p["symbol"] for p in self.db.get_open_positions()}
        return [
            d for d in self._funding_data
            if d["funding_rate"] < Config.MIN_FUNDING_RATE
            and d["symbol"] not in open_syms
        ][: Config.MAX_POSITIONS]

    def refresh_balances(self) -> dict:
        raw = self.client.get_usdt_balances()
        total = raw["total"]
        raw["per_position"] = total / Config.MAX_POSITIONS if total > 0 else 0.0
        self._last_balances = raw
        return raw

    # ── Scheduling ────────────────────────────────────────────────────────────

    def schedule_entries(self):
        """Schedule one BUY job per opportunity, firing 5 min before funding."""
        now_ms  = int(time.time() * 1000)
        buffer  = Config.ENTRY_BEFORE_SECONDS * 1000   # ms

        for opp in self.get_top_opportunities():
            entry_ms = opp["next_funding_ms"] - buffer
            if entry_ms <= now_ms:
                continue                                # window already past

            entry_dt = datetime.utcfromtimestamp(entry_ms / 1000)
            job_id   = f"entry_{opp['symbol']}_{opp['next_funding_ms']}"

            if not self.scheduler.get_job(job_id):
                self.scheduler.add_job(
                    self._open_position,
                    "date",
                    run_date=entry_dt,
                    args=[opp],
                    id=job_id,
                    misfire_grace_time=120,
                )
                logger.info(
                    f"⏰ Scheduled entry for {opp['symbol']} "
                    f"at {entry_dt.strftime('%H:%M:%S UTC')} "
                    f"(funding rate {opp['funding_rate_pct']:.4f}%, "
                    f"spread {opp['spread_pct']:.4f}%)"
                )

    # ── Entry ─────────────────────────────────────────────────────────────────

    def _open_position(self, opp: dict):
        """Execute both legs: long futures + short spot margin."""
        sym = opp["symbol"]

        if self.db.symbol_is_open(sym):
            logger.warning(f"Skipping {sym}: already open.")
            return

        balances = self.refresh_balances()
        size_usdt = balances["per_position"]

        if size_usdt < Config.MIN_POSITION_USDT:
            logger.warning(f"Insufficient balance ({size_usdt:.2f} USDT) for {sym}.")
            return

        # Refresh prices at execution time
        f_ticker = self.client.get_futures_book_ticker(sym)
        s_ticker = self.client.get_spot_book_ticker(sym)
        if not f_ticker or not s_ticker:
            logger.error(f"Could not get tickers for {sym}.")
            return

        f_ask = float(f_ticker["askPrice"])
        s_bid = float(s_ticker["bidPrice"])

        if f_ask <= 0 or s_bid <= 0:
            return

        entry_spread = (f_ask - s_bid) / s_bid * 100.0

        # Quantity per leg
        qty_f = self.client.get_futures_qty(sym, size_usdt, f_ask)
        qty_s = self.client.get_spot_qty(sym,    size_usdt, s_bid)

        if qty_f <= 0:
            logger.error(f"Zero quantity for {sym}. Skipping.")
            return

        # ── Set 1× leverage ───────────────────────────────────────────────────
        self.client.set_leverage(sym, Config.DEFAULT_LEVERAGE)

        # ── Leg 1: Long Futures ───────────────────────────────────────────────
        f_order = self.client.futures_order(sym, "BUY", qty_f)
        if not f_order:
            logger.error(f"Futures order failed for {sym}. Aborting.")
            return

        logger.info(f"✅ Futures LONG {qty_f} {sym} @ ~{f_ask:.6f}")

        # ── Leg 2: Short Spot (cross-margin) ──────────────────────────────────
        m_order = None
        qty_spot_actual = 0.0
        if Config.SPOT_MODE == "margin" and qty_s > 0:
            m_order = self.client.margin_order(sym, "SELL", qty_s,
                                               side_effect="AUTO_BORROW_REPAY")
            if m_order:
                qty_spot_actual = qty_s
                logger.info(f"✅ Spot SHORT {qty_s} {sym} @ ~{s_bid:.6f} (cross-margin)")
            else:
                logger.warning(f"Margin short failed for {sym}. Futures-only mode.")

        # ── Persist ───────────────────────────────────────────────────────────
        self.db.open_position({
            "symbol":              sym,
            "entry_time":          time.time(),
            "next_funding_time":   opp["next_funding_ms"] / 1000,
            "entry_spread":        entry_spread,
            "position_usdt":       size_usdt,
            "qty_futures":         qty_f,
            "qty_spot":            qty_spot_actual,
            "futures_entry_price": f_ask,
            "spot_entry_price":    s_bid,
            "futures_order_id":    f_order.get("orderId", ""),
            "margin_order_id":     m_order.get("orderId", "") if m_order else "",
        })
        self.db.log("INFO",
            f"OPEN {sym} | spread {entry_spread:.4f}% | size {size_usdt:.2f} USDT "
            f"| funding {opp['funding_rate_pct']:.4f}%"
        )
        logger.info(
            f"🟢 Position opened: {sym} | entry_spread={entry_spread:.4f}% "
            f"| size={size_usdt:.2f} USDT"
        )

    # ── Monitoring & Exit ─────────────────────────────────────────────────────

    def monitor_positions(self):
        """Called every loop. Checks exit conditions for each open position."""
        positions = self.db.get_open_positions()
        if not positions:
            return

        now = time.time()

        for pos in positions:
            sym = pos["symbol"]

            # ── Refresh prices ─────────────────────────────────────────────
            f_ticker = self.client.get_futures_book_ticker(sym)
            s_ticker = self.client.get_spot_book_ticker(sym)
            if not f_ticker or not s_ticker:
                continue

            f_bid = float(f_ticker["bidPrice"])  # we SELL futures at bid to close
            s_ask = float(s_ticker["askPrice"])  # we BUY spot at ask to close

            current_spread = (f_bid - s_ask) / s_ask * 100.0

            # ── Funding collected so far ────────────────────────────────────
            entry_ms = int(pos["entry_time"] * 1000)
            income   = self.client.get_funding_income(sym, start_time_ms=entry_ms) or []
            funding_collected = sum(float(r.get("income", 0)) for r in income)

            # ── Current funding rate ────────────────────────────────────────
            pi = self.client.get_premium_index(sym)
            current_funding = float(pi.get("lastFundingRate", 0)) if pi else 0.0

            # ── Update DB live fields ───────────────────────────────────────
            self.db.update_live(sym, current_spread, current_funding, funding_collected)
            self.db.record_spread(sym, current_spread)

            # ── Min holding period check ────────────────────────────────────
            # Must hold until AFTER the first funding settlement following entry
            min_hold_end = pos["next_funding_time"] + Config.MIN_HOLD_EXTRA_SECONDS
            if now < min_hold_end:
                remaining = int(min_hold_end - now)
                logger.debug(
                    f"{sym}: min hold {remaining}s remaining "
                    f"| spread={current_spread:.4f}%"
                )
                continue   # Do NOT exit yet

            # ── Exit conditions ─────────────────────────────────────────────
            reason = None

            if current_spread <= Config.EXIT_SPREAD_THRESHOLD:
                reason = "spread_closed"
                logger.info(
                    f"📉 {sym}: spread={current_spread:.4f}% ≤ "
                    f"{Config.EXIT_SPREAD_THRESHOLD}%. Closing."
                )

            elif current_funding > 0.0001:
                reason = "funding_flipped_positive"
                logger.info(f"🔄 {sym}: funding turned positive. Closing.")

            elif self._unrealised_loss_pct(pos, f_bid, s_ask) > Config.STOP_LOSS_PCT:
                reason = "stop_loss"
                logger.warning(f"🛑 {sym}: stop-loss triggered. Closing.")

            if reason:
                self._close_position(pos, reason, f_bid, s_ask,
                                     current_spread, funding_collected)

    def _unrealised_loss_pct(self, pos: dict, f_bid: float, s_ask: float) -> float:
        """Estimate unrealised loss % on the futures leg only (margin-to-price)."""
        entry = pos["futures_entry_price"]
        if entry <= 0:
            return 0.0
        return max(0.0, (entry - f_bid) / entry * 100.0)

    def _close_position(self, pos: dict, reason: str,
                         f_bid: float, s_ask: float,
                         current_spread: float, funding_collected: float):
        sym = pos["symbol"]

        # ── Leg 1: Close futures long ──────────────────────────────────────
        qty_f = pos["qty_futures"]
        self.client.futures_order(sym, "SELL", qty_f)
        logger.info(f"Futures SELL {qty_f} {sym}")

        # ── Leg 2: Close margin short ──────────────────────────────────────
        qty_s = pos.get("qty_spot", 0)
        if qty_s > 0 and Config.SPOT_MODE == "margin":
            self.client.margin_order(sym, "BUY", qty_s,
                                     side_effect="AUTO_BORROW_REPAY")
            logger.info(f"Spot BUY-BACK {qty_s} {sym}")

        # ── P&L ───────────────────────────────────────────────────────────
        # Spread P&L: we entered at entry_spread premium and exit at current_spread
        # If entry_spread > exit_spread → spread compressed → profit on spread leg
        spread_pnl = (pos["entry_spread"] - current_spread) / 100.0 * pos["position_usdt"]
        total_pnl  = funding_collected + spread_pnl

        self.db.close_position(sym, {
            "exit_time":         time.time(),
            "exit_spread":       current_spread,
            "spread_pnl":        spread_pnl,
            "funding_collected": funding_collected,
            "total_pnl":         total_pnl,
            "reason":            reason,
        })
        self.db.log("INFO",
            f"CLOSE {sym} | reason={reason} | "
            f"entry_spread={pos['entry_spread']:.4f}% exit_spread={current_spread:.4f}% | "
            f"funding={funding_collected:.4f} | total_pnl={total_pnl:.4f} USDT"
        )
        logger.info(
            f"🔴 Closed {sym} [{reason}] | "
            f"spread P&L={spread_pnl:.4f} | funding={funding_collected:.4f} | "
            f"total={total_pnl:.4f} USDT"
        )

    # ── Main Loop ─────────────────────────────────────────────────────────────

    def _main_loop(self):
        logger.info("Main loop running.")
        snapshot_counter = 0

        while self.is_running:
            try:
                # 1. Refresh market data
                self._funding_data = self.get_funding_data()

                # 2. Refresh balances
                self.refresh_balances()

                # 3. Schedule entries for best opportunities
                self.schedule_entries()

                # 4. Monitor open positions (check exit conditions)
                self.monitor_positions()

                snapshot_counter += 1

            except Exception as exc:
                logger.error(f"Main loop error: {exc}", exc_info=True)

            time.sleep(Config.LOOP_INTERVAL_SECONDS)

    # ── Public accessors (for dashboard) ──────────────────────────────────────

    def get_live_funding_table(self) -> list:
        """Top-10 opportunities with computed countdown."""
        now_ms = int(time.time() * 1000)
        open_syms = {p["symbol"] for p in self.db.get_open_positions()}
        rows = []
        for d in self._funding_data[:10]:
            secs_to_fund = max(0, (d["next_funding_ms"] - now_ms) / 1000)
            rows.append({
                **d,
                "secs_to_funding": secs_to_fund,
                "is_open":         d["symbol"] in open_syms,
            })
        return rows

    @property
    def balances(self) -> dict:
        return self._last_balances
