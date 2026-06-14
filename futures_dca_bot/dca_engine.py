"""
dca_engine.py — Core Futures DCA bot logic.

Implements Futures DCA bot behaviour:

  1. On signal → market order for BASE_ORDER_USDT margin
  2. Pre-place MAX_DCA_ORDERS limit orders at compounding
     PRICE_DEVIATION steps below entry (long) / above (short).
  3. Pre-place a reduce-only TAKE PROFIT limit order and optional STOP_MARKET.
  4. When any DCA limit fills → recalculate avg entry →
     cancel old TP/SL → re-place TP/SL at new prices.
  5. When position = 0 → round closed; back to signal scanning.

DCA size schedule  (i = 0-indexed DCA order number)
────────────────────────────────────────────────────
  margin_i = DCA_ORDER_USDT × DCA_SIZE_MULTIPLIER^i

Price level schedule
──────────────────────
  price_0  = entry_price
  dev_i    = sum(PRICE_DEVIATION × PRICE_DEV_MULTIPLIER^n for n=0..i)
  price_i  = entry_price × (1 - dev_i / 100)   [LONG]
  price_i  = entry_price × (1 + dev_i / 100)   [SHORT]
"""

from __future__ import annotations

import time
import logging
import requests
from typing import Optional
from datetime import datetime, timezone

from delta_client import DeltaClient, DeltaAuthError
from signal_engine import Signal, get_signal
import database as db
from config import (
    API_KEY, API_SECRET, USE_TESTNET,
    TELEGRAM_TOKEN, TELEGRAM_CHAT_ID,
    SYMBOL, LEVERAGE, MARGIN_TYPE,
    PRICE_DEVIATION, TAKE_PROFIT_PCT, STOP_LOSS_PCT,
    MAX_DCA_ORDERS, BASE_ORDER_USDT, DCA_ORDER_USDT,
    DCA_SIZE_MULTIPLIER, PRICE_DEV_MULTIPLIER,
    WALLET_DEPLOY_PCT, MIN_DCA_ORDER_USDT,
    START_CONDITION, START_TRIGGER_PRICE,
    STOP_CONDITION, STOP_TRIGGER_PRICE,
    ALLOW_LONG, ALLOW_SHORT,
    SIGNAL_POLL_INTERVAL, ROUND_POLL_INTERVAL,
)

logger = logging.getLogger(__name__)

ORDER_MARGIN_SAFETY_PCT = 0.98


# ──────────────────────────────────────────────────────────────
#  Telegram helper
# ──────────────────────────────────────────────────────────────

def _tg(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as e:
        logger.warning(f"Telegram send failed: {e}")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ──────────────────────────────────────────────────────────────
#  DCA Round state object
# ──────────────────────────────────────────────────────────────

class DCASession:
    """
    Holds in-memory state for one active DCA round.
    Tracks one active DCA round.
    """

    def __init__(self, session_id: int, direction: str,
                 entry_price: float, entry_qty: float, entry_margin: float):
        self.session_id    = session_id
        self.direction     = direction           # 'LONG' | 'SHORT'
        self.dca_count     = 0                   # DCA orders filled so far
        self.initial_entry = entry_price
        self.avg_entry     = entry_price
        self.total_qty     = entry_qty
        self.total_margin  = entry_margin

        # Exchange order IDs
        self.tp_order_id   = None
        self.sl_order_id   = None
        self.dca_order_ids: list = []   # open (unfilled) DCA limit orders
        self.dca_order_margins: dict = {}  # order id -> actual reserved margin target

    # ── price level maths ─────────────────────────────────────

    def _dca_deviation_pct(self, level: int) -> float:
        """
        Cumulative deviation from the base order.
        Example: deviation=1, multiplier=2 -> 1%, 3%, 7%.
        """
        return sum(
            PRICE_DEVIATION * (PRICE_DEV_MULTIPLIER ** i)
            for i in range(level + 1)
        )

    def _dca_price(self, level: int) -> float:
        """
        Price for absolute DCA level `level` (0-indexed from first DCA).
        DCA gaps are cumulative price change from the base order.
        """
        pct = self._dca_deviation_pct(level) / 100
        if self.direction == "LONG":
            return self.initial_entry * (1 - pct)
        else:
            return self.initial_entry * (1 + pct)

    def _dca_margin(self, level: int) -> float:
        """Margin (USDT) for DCA order at absolute level (0-indexed from first DCA)."""
        return DCA_ORDER_USDT * (DCA_SIZE_MULTIPLIER ** level)

    def tp_price(self) -> float:
        pct = TAKE_PROFIT_PCT / 100
        if self.direction == "LONG":
            return self.avg_entry * (1 + pct)
        else:
            return self.avg_entry * (1 - pct)

    def sl_price(self) -> float:
        pct = STOP_LOSS_PCT / 100
        if self.direction == "LONG":
            return self.initial_entry * (1 - pct)
        else:
            return self.initial_entry * (1 + pct)

    # ── helpers ───────────────────────────────────────────────

    def close_side(self) -> str:
        return "SELL" if self.direction == "LONG" else "BUY"

    def open_side(self) -> str:
        return "BUY" if self.direction == "LONG" else "SELL"

    def remaining_dca_slots(self) -> int:
        return MAX_DCA_ORDERS - self.dca_count


# ──────────────────────────────────────────────────────────────
#  Main bot class
# ──────────────────────────────────────────────────────────────

class DCABot:
    def __init__(self):
        self.client: DeltaClient = DeltaClient(API_KEY, API_SECRET, USE_TESTNET)
        self.session: Optional[DCASession] = None
        self.running  = True
        self.paused   = False
        self.startup_error = None
        self.round_margin_budget = 0.0
        self.round_margin_used = 0.0

        # lifetime stats
        self.total_realized_pnl = 0.0
        self.rounds_completed   = 0
        self.wins               = 0

        db.init_db()

    @staticmethod
    def _trade_direction(signal: Signal | None) -> Optional[str]:
        if signal is None:
            return None
        if signal.direction in ("LONG", "SHORT"):
            return signal.direction
        logger.info(
            "No DCA entry while signal is %s | squeeze=%s | BB bandwidth=%.4f | ATR%%=%.4f",
            signal.direction,
            signal.is_squeeze,
            signal.bb_bandwidth,
            signal.atr_pct,
        )
        return None

    # ──────────────────────────────────────────────────────────
    #  Setup
    # ──────────────────────────────────────────────────────────

    def _setup_symbol(self):
        if not API_KEY or not API_SECRET:
            raise RuntimeError(
                "Missing Delta Exchange India credentials. Set DELTA_API_KEY and "
                "DELTA_API_SECRET environment variables."
            )

        try:
            auth_ok = self.client.verify_futures_access()
        except DeltaAuthError as e:
            if e.code == "ip_not_whitelisted_for_api_key" and e.client_ip:
                raise RuntimeError(
                    "Delta Exchange India API key rejected this machine's public IP. "
                    f"Add {e.client_ip} to the API key IP whitelist, then restart the bot."
                ) from e
            raise RuntimeError(
                "Delta Exchange India API authentication failed. Check that the "
                "key is active, Trading permission is enabled, and any IP "
                "whitelist includes this machine's public IP."
            ) from e

        if not auth_ok:
            raise RuntimeError(
                "Delta Exchange India API authentication failed. Check that the "
                "key is active, Trading permission is enabled, and any IP "
                "whitelist includes this machine's public IP."
            )

        margin_ok = self.client.set_margin_type(SYMBOL, MARGIN_TYPE)
        lev_ok = self.client.set_leverage(SYMBOL, LEVERAGE)
        if not (margin_ok and lev_ok):
            raise RuntimeError("Delta Exchange India symbol setup failed (margin/leverage).")

    # ──────────────────────────────────────────────────────────
    #  Round lifecycle
    # ──────────────────────────────────────────────────────────

    def _start_round(self, direction: str):
        logger.info(f"▶ Starting {direction} round on {SYMBOL}")

        price = self.client.get_mark_price(SYMBOL)
        wallet_balance = self.client.get_usdt_balance()
        self.round_margin_budget = max(0.0, wallet_balance * WALLET_DEPLOY_PCT)
        self.round_margin_used = 0.0
        planned_margin = self._planned_round_margin()
        logger.info(
            "DCA round margin budget: %.4f (%.0f%% of futures wallet %.4f); planned full schedule: %.4f",
            self.round_margin_budget,
            WALLET_DEPLOY_PCT * 100,
            wallet_balance,
            planned_margin,
        )
        if self.round_margin_budget < planned_margin:
            logger.info(
                "Round budget is below the full DCA schedule by %.4f; later DCA orders may be skipped.",
                planned_margin - self.round_margin_budget,
            )
        base_margin = self._wallet_limited_margin(BASE_ORDER_USDT)
        if base_margin <= 0:
            logger.warning("Skipping round start: no available wallet margin for base order.")
            self.round_margin_budget = 0.0
            return
        qty = (base_margin * LEVERAGE) / price

        # 1. Market order — base entry
        side = "BUY" if direction == "LONG" else "SELL"
        try:
            order = self.client.market_order(SYMBOL, side, qty)
        except Exception as e:
            margin_context = self._insufficient_margin_context(e)
            retry_margin = self._reduced_margin_from_error(base_margin, margin_context)
            if retry_margin <= 0 or retry_margin >= base_margin:
                raise
            retry_qty = (retry_margin * LEVERAGE) / price
            logger.warning(
                "Base order retried with wallet margin %.4f after insufficient_margin",
                retry_margin,
            )
            try:
                order = self.client.market_order(SYMBOL, side, retry_qty)
            except Exception as retry_error:
                logger.error("Base order retry failed: %s", retry_error)
                self.round_margin_budget = 0.0
                self.round_margin_used = 0.0
                return
            base_margin = retry_margin
            qty = retry_qty
        self._record_budget_use(base_margin)
        fill_price = float(order.get("avgPrice") or price)
        fill_qty   = float(order.get("executedQty") or qty)
        if fill_price <= 0:
            fill_price = price
        if fill_qty <= 0:
            fill_qty = qty

        # 2. Persist session
        start_ts   = _now_iso()
        session_id = db.insert_session(
            SYMBOL, direction, start_ts,
            fill_price, fill_qty, base_margin
        )
        db.insert_fill(session_id, "BASE", order.get("orderId"),
                       fill_price, fill_qty, base_margin, start_ts)

        # 3. Build in-memory state
        self.session = DCASession(session_id, direction, fill_price, fill_qty, base_margin)

        # 4. Place all DCA limit orders
        self._place_dca_orders()

        # 5. Place TP + SL
        self._place_tp_sl()

        _tg(
            f"🚀 <b>DCA Round Started</b>\n"
            f"Symbol   : {SYMBOL}\n"
            f"Direction: {direction}\n"
            f"Entry    : {fill_price:.2f} USDT\n"
            f"Qty      : {fill_qty:.5f}\n"
            f"Margin   : {base_margin:.2f} USDT  ({LEVERAGE}×)\n"
            f"TP → {self.session.tp_price():.2f}  |  SL → {self.session.sl_price():.2f}"
        )
        logger.info(
            f"Round open | session={session_id} entry={fill_price:.2f} "
            f"TP={self.session.tp_price():.2f} SL={self.session.sl_price():.2f}"
        )

    def _place_dca_orders(self):
        """
        Pre-place all remaining DCA limit orders.
        Called at round start and NOT called again after fills
        (DCA orders auto-cancel when TP/SL is hit).
        """
        s = self.session
        remaining = s.remaining_dca_slots()
        if remaining <= 0:
            return

        new_ids = []

        for i in range(remaining):
            abs_level   = s.dca_count + i          # absolute DCA level (0-indexed)
            dca_price   = s._dca_price(abs_level)
            planned_margin = s._dca_margin(abs_level)
            dca_margin  = self._wallet_limited_margin(
                planned_margin,
                purpose=f"DCA#{abs_level + 1}",
                min_margin=MIN_DCA_ORDER_USDT,
            )
            if dca_margin <= 0:
                break
            dca_qty     = (dca_margin * LEVERAGE) / dca_price

            try:
                order = self.client.limit_order(
                    SYMBOL, s.open_side(), dca_price, dca_qty
                )
                new_ids.append(order["orderId"])
                s.dca_order_margins[order["orderId"]] = dca_margin
                self._record_budget_use(dca_margin)
                logger.debug(
                    f"DCA#{abs_level+1} limit placed @ {dca_price:.2f}  "
                    f"qty={dca_qty:.5f} margin={dca_margin:.4f}"
                )

            except Exception as e:
                margin_context = self._insufficient_margin_context(e)
                retry_margin = self._reduced_margin_from_error(dca_margin, margin_context)
                if retry_margin > 0 and retry_margin < dca_margin:
                    retry_qty = (retry_margin * LEVERAGE) / dca_price
                    try:
                        order = self.client.limit_order(
                            SYMBOL, s.open_side(), dca_price, retry_qty
                        )
                        new_ids.append(order["orderId"])
                        s.dca_order_margins[order["orderId"]] = retry_margin
                        self._record_budget_use(retry_margin)
                        logger.warning(
                            "DCA#%s retried with wallet margin %.4f after insufficient_margin",
                            abs_level + 1,
                            retry_margin,
                        )
                        continue
                    except Exception as retry_error:
                        logger.error(
                            "_place_dca_orders level %s retry failed: %s",
                            abs_level,
                            retry_error,
                        )
                logger.error(f"_place_dca_orders level {abs_level}: {e}")
                break

        s.dca_order_ids.extend(new_ids)
        logger.info(f"Placed {len(new_ids)} DCA limit orders (total open: {len(s.dca_order_ids)})")

    @staticmethod
    def _insufficient_margin_context(exc) -> Optional[dict]:
        payload = exc.args[0] if getattr(exc, "args", None) else None
        if isinstance(payload, dict) and payload.get("code") == "insufficient_margin":
            return payload.get("context") or {}
        return None

    def _wallet_limited_margin(
        self,
        requested_margin: float,
        purpose: str = "order",
        min_margin: float = 0.0,
    ) -> float:
        available = self.client.get_usdt_balance()
        remaining_budget = max(0.0, self.round_margin_budget - self.round_margin_used)
        usable = min(max(0.0, available * ORDER_MARGIN_SAFETY_PCT), remaining_budget)
        if usable <= 0:
            logger.warning("No available wallet margin or DCA budget for next order.")
            return 0.0
        if requested_margin > usable:
            if usable < min_margin:
                logger.info(
                    "Skipping %s: requested margin %.4f, available %.4f, budget_left %.4f is below min %.4f",
                    purpose,
                    requested_margin,
                    available,
                    remaining_budget,
                    min_margin,
                )
                return 0.0
            logger.warning(
                "%s margin capped by wallet/budget: requested %.4f, available %.4f, budget_left %.4f, using %.4f",
                purpose,
                requested_margin,
                available,
                remaining_budget,
                usable,
            )
            return usable
        return requested_margin

    @staticmethod
    def _planned_round_margin() -> float:
        dca_total = sum(
            DCA_ORDER_USDT * (DCA_SIZE_MULTIPLIER ** level)
            for level in range(MAX_DCA_ORDERS)
        )
        return BASE_ORDER_USDT + dca_total

    @staticmethod
    def _reduced_margin_from_error(current_margin: float, margin_context: Optional[dict]) -> float:
        if not margin_context:
            return 0.0
        available = float(margin_context.get("available_balance") or 0)
        required_additional = float(margin_context.get("required_additional_balance") or 0)
        required_total = available + required_additional
        if available <= 0 or required_total <= 0:
            return 0.0
        return current_margin * (available / required_total) * ORDER_MARGIN_SAFETY_PCT

    def _record_budget_use(self, margin: float) -> None:
        self.round_margin_used = min(
            self.round_margin_budget,
            self.round_margin_used + max(0.0, margin),
        )

    def _place_tp_sl(self):
        s = self.session

        # Cancel old TP / SL if any
        for oid in (s.tp_order_id, s.sl_order_id):
            if oid:
                self.client.cancel_order(SYMBOL, oid)

        try:
            tp_order = self.client.reduce_only_limit_order(
                SYMBOL, s.close_side(), s.tp_price(), s.total_qty
            )
            s.tp_order_id = tp_order["orderId"]
        except Exception as e:
            logger.error(f"_place_tp_sl (TP): {e}")

        if STOP_LOSS_PCT and STOP_LOSS_PCT > 0:
            try:
                sl_order = self.client.stop_market(
                    SYMBOL, s.close_side(), s.sl_price()
                )
                s.sl_order_id = sl_order["orderId"]
            except Exception as e:
                logger.error(f"_place_tp_sl (SL): {e}")
        else:
            s.sl_order_id = None

        logger.info(f"TP @ {s.tp_price():.2f}  SL @ {s.sl_price():.2f}")

    # ──────────────────────────────────────────────────────────
    #  Round monitoring — called every ROUND_POLL_INTERVAL secs
    # ──────────────────────────────────────────────────────────

    def _check_round(self):
        s = self.session

        # ── 1. Check if position still open ───────────────────
        pos      = self.client.get_position(SYMBOL)
        pos_qty  = abs(float(pos.get("positionAmt", 0)))

        if pos_qty < 1e-8:
            # Position fully closed → round ended
            self._close_round()
            return

        # ── 2. Detect filled DCA orders ────────────────────────
        if not s.dca_order_ids:
            return

        open_orders    = self.client.get_open_orders(SYMBOL)
        open_order_ids = {o["orderId"] for o in open_orders}

        newly_filled = [oid for oid in s.dca_order_ids
                        if oid not in open_order_ids]

        if not newly_filled:
            return   # nothing new

        # Remove filled DCA ids from tracking list
        for oid in newly_filled:
            s.dca_order_ids.remove(oid)

        # ── 3. Sync state from exchange ───────────────────────
        #  The exchange's entryPrice and positionAmt are authoritative
        new_entry = abs(float(pos.get("entryPrice", s.avg_entry)))
        new_qty   = pos_qty
        filled_n  = len(newly_filled)

        ts = _now_iso()
        for i, oid in enumerate(newly_filled):
            abs_level  = s.dca_count + i
            dca_margin = s.dca_order_margins.pop(oid, s._dca_margin(abs_level))
            fill_order = self.client.get_order(SYMBOL, oid)
            fill_price = float(fill_order.get("avgPrice") or s._dca_price(abs_level))
            fill_qty = float(fill_order.get("executedQty") or 0)
            db.insert_fill(s.session_id, f"DCA_{abs_level+1}", oid,
                           fill_price, fill_qty, dca_margin, ts)
            s.total_margin += dca_margin

        s.dca_count        += filled_n
        s.avg_entry         = new_entry
        s.total_qty         = new_qty

        # Persist
        db.update_session(
            s.session_id,
            avg_entry_price = new_entry,
            total_quantity  = new_qty,
            total_margin    = s.total_margin,
            dca_count       = s.dca_count,
        )

        # ── 4. Update TP / SL ─────────────────────────────────
        self._place_tp_sl()

        logger.info(
            f"DCA #{s.dca_count} filled | "
            f"avg entry={new_entry:.2f}  qty={new_qty:.5f}  "
            f"margin={s.total_margin:.2f} USDT"
        )
        _tg(
            f"⬇️ <b>DCA #{s.dca_count} Filled</b>\n"
            f"Symbol   : {SYMBOL}\n"
            f"Avg Entry: {new_entry:.2f}\n"
            f"Total Qty: {new_qty:.5f}\n"
            f"Margin   : {s.total_margin:.2f} USDT\n"
            f"New TP → {s.tp_price():.2f}  |  SL → {s.sl_price():.2f}\n"
            f"DCA slots remaining: {s.remaining_dca_slots()}"
        )

    def _close_round(self, reason_override: Optional[str] = None):
        s = self.session
        # Cancel every remaining open order
        self.client.cancel_all_orders(SYMBOL)

        # Determine PnL from mark price (approximation; exchange reports exact)
        mark = self.client.get_mark_price(SYMBOL)
        if s.direction == "LONG":
            raw_pnl = (mark - s.avg_entry) * s.total_qty
        else:
            raw_pnl = (s.avg_entry - mark) * s.total_qty

        # Decide close reason by comparing mark vs TP / SL
        if reason_override:
            reason = reason_override
        elif s.direction == "LONG":
            reason = "TP" if mark >= s.tp_price() else "SL"
        else:
            reason = "TP" if mark <= s.tp_price() else "SL"

        db.close_session(s.session_id, _now_iso(), raw_pnl, reason)

        self.total_realized_pnl += raw_pnl
        self.rounds_completed   += 1
        if raw_pnl > 0:
            self.wins += 1

        emoji = "✅" if raw_pnl > 0 else "❌"
        _tg(
            f"{emoji} <b>DCA Round Closed — {reason}</b>\n"
            f"Symbol   : {SYMBOL}\n"
            f"Direction: {s.direction}\n"
            f"DCA fills: {s.dca_count}\n"
            f"PnL      : {raw_pnl:+.2f} USDT\n"
            f"Total PnL: {self.total_realized_pnl:+.2f} USDT\n"
            f"Win rate : {self.wins}/{self.rounds_completed}"
        )
        logger.info(
            f"Round closed | reason={reason}  pnl={raw_pnl:+.2f}  "
            f"total_pnl={self.total_realized_pnl:+.2f}"
        )

        self.session = None
        self.round_margin_budget = 0.0
        self.round_margin_used = 0.0
        if STOP_CONDITION == "END_AFTER_ROUND":
            self.running = False
            logger.info("Stop condition END_AFTER_ROUND reached; bot will not start a new round.")

    # ──────────────────────────────────────────────────────────
    #  Manual controls
    # ──────────────────────────────────────────────────────────

    def manual_close(self):
        """Market-close the active position and end the round."""
        if not self.session:
            return
        s = self.session
        self.client.cancel_all_orders(SYMBOL)
        self.client.market_order(SYMBOL, s.close_side(), s.total_qty)
        self._close_round("MANUAL")

    def stop(self):
        self.running = False
        if self.session:
            self.manual_close()
        logger.info("DCA bot stopped.")

    def _start_condition_ready(self, direction: str) -> bool:
        if START_CONDITION != "TRIGGER_PRICE" or START_TRIGGER_PRICE is None:
            return True

        mark = self.client.get_mark_price(SYMBOL)
        if direction == "LONG":
            ready = mark >= float(START_TRIGGER_PRICE)
        else:
            ready = mark <= float(START_TRIGGER_PRICE)

        if not ready:
            logger.info(
                f"Start trigger waiting | direction={direction} mark={mark:.2f} "
                f"trigger={float(START_TRIGGER_PRICE):.2f}"
            )
        return ready

    def _stop_condition_reached(self) -> bool:
        if STOP_CONDITION != "TRIGGER_PRICE" or STOP_TRIGGER_PRICE is None:
            return False

        mark = self.client.get_mark_price(SYMBOL)
        direction = self.session.direction if self.session else None
        if direction == "SHORT":
            return mark >= float(STOP_TRIGGER_PRICE)
        return mark <= float(STOP_TRIGGER_PRICE)

    def _stop_by_condition(self):
        logger.info("Stop condition trigger price reached; ending Futures DCA bot.")
        if self.session:
            s = self.session
            self.client.cancel_all_orders(SYMBOL)
            self.client.market_order(SYMBOL, s.close_side(), s.total_qty)
            self._close_round("STOP_CONDITION")
        self.running = False

    # ──────────────────────────────────────────────────────────
    #  Main loop
    # ──────────────────────────────────────────────────────────

    def run(self):
        try:
            self._setup_symbol()
        except Exception as e:
            self.running = False
            self.startup_error = str(e)
            logger.error(f"Bot startup failed: {e}")
            return

        logger.info(
            f"Futures DCA Bot running | {SYMBOL} {LEVERAGE}× "
            f"EMA{EMA_FAST}/{EMA_SLOW} {TIMEFRAME}"
        )
        _tg(
            f"🤖 <b>Futures DCA Bot Started</b>\n"
            f"Symbol   : {SYMBOL}\n"
            f"Leverage : {LEVERAGE}×  ({MARGIN_TYPE})\n"
            f"Signal   : EMA{EMA_FAST}/{EMA_SLOW} on {TIMEFRAME}\n"
            f"TP {TAKE_PROFIT_PCT}%  |  SL {STOP_LOSS_PCT}%  |  MaxDCA {MAX_DCA_ORDERS}"
        )

        last_signal_check = 0.0

        while self.running:
            try:
                now = time.time()

                if self._stop_condition_reached():
                    self._stop_by_condition()
                    break

                if self.paused:
                    time.sleep(ROUND_POLL_INTERVAL)
                    continue

                if self.session is None:
                    # ── Idle: scan for entry signal ───────────
                    if now - last_signal_check >= SIGNAL_POLL_INTERVAL:
                        last_signal_check = now
                        direction = self._trade_direction(get_signal(self.client))

                        if direction == "LONG" and ALLOW_LONG and self._start_condition_ready("LONG"):
                            self._start_round("LONG")
                        elif direction == "SHORT" and ALLOW_SHORT and self._start_condition_ready("SHORT"):
                            self._start_round("SHORT")

                    time.sleep(ROUND_POLL_INTERVAL)

                else:
                    # ── Active round: monitor fills & closure ──
                    if now - last_signal_check >= SIGNAL_POLL_INTERVAL:
                        last_signal_check = now
                        direction = self._trade_direction(get_signal(self.client))
                        if direction and direction != self.session.direction:
                            logger.info(
                                "Opposite EMA cross detected: closing %s and starting %s",
                                self.session.direction,
                                direction,
                            )
                            self.manual_close()
                            if direction == "LONG" and ALLOW_LONG and self._start_condition_ready("LONG"):
                                self._start_round("LONG")
                            elif direction == "SHORT" and ALLOW_SHORT and self._start_condition_ready("SHORT"):
                                self._start_round("SHORT")
                            time.sleep(ROUND_POLL_INTERVAL)
                            continue

                    self._check_round()
                    time.sleep(ROUND_POLL_INTERVAL)

            except KeyboardInterrupt:
                logger.info("KeyboardInterrupt received.")
                self.stop()
                break
            except Exception as e:
                logger.error(f"Bot loop error: {e}", exc_info=True)
                time.sleep(10)


from config import EMA_FAST, EMA_SLOW, TIMEFRAME  # noqa: E402
