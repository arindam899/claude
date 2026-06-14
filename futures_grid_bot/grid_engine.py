"""
╔══════════════════════════════════════════════════════════════════╗
║     GRID ENGINE                                                  ║
║     Manages: Orders · Trailing · TP/SL · Trigger · Fills        ║
╚══════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

import time
import logging
from dataclasses import dataclass, field
from regime_detector import GridMode, RegimeResult
import config as cfg

log = logging.getLogger("GridEngine")


@dataclass
class GridState:
    mode:            GridMode      = GridMode.NEUTRAL
    lower_price:     float         = 0.0
    upper_price:     float         = 0.0
    num_grids:       int           = 30
    leverage:        int           = 3
    qty_per_grid:    float         = 0.0
    grid_prices:     list          = field(default_factory=list)
    active_orders:   dict          = field(default_factory=dict)   # price → order_id
    active_order_sides: dict       = field(default_factory=dict)
    tp_order_id:     int           = 0
    sl_order_id:     int           = 0
    triggered:       bool          = False
    is_active:       bool          = False
    total_fills:     int           = 0


class GridEngine:

    def __init__(self, client, alerter):
        self.client  = client
        self.alerter = alerter
        self.state   = GridState()
        self.symbol  = cfg.SYMBOL

    # ── Grid Price Calculation ─────────────────────────────────────────
    def _calc_grid_prices(self, lower: float, upper: float,
                           n: int) -> list[float]:
        if cfg.GRID_MODE == "ARITHMETIC":
            step   = (upper - lower) / n
            prices = [lower + i * step for i in range(n + 1)]
        else:  # GEOMETRIC
            ratio  = (upper / lower) ** (1 / n)
            prices = [lower * (ratio ** i) for i in range(n + 1)]
        return [round(p, 2) for p in prices]

    def _profit_per_grid_pct(self, lower, upper, n) -> float:
        if cfg.GRID_MODE == "ARITHMETIC":
            step = (upper - lower) / n
            return round(step / lower * 100, 3)
        else:
            ratio = (upper / lower) ** (1 / n)
            return round((ratio - 1) * 100, 3)

    def _calc_qty_per_grid(
        self,
        investment: float,
        leverage: float,
        grid_prices: list,
        mode: GridMode,
        num_grids: int,
    ) -> float:
        """Position size per grid level."""
        initial_slots = 0
        if cfg.OPEN_ON_CREATION and mode != GridMode.NEUTRAL:
            initial_slots = max(1, num_grids // 4)
        total_slots = len(grid_prices) + initial_slots
        notional    = investment * leverage
        avg_price   = sum(grid_prices) / len(grid_prices)
        raw_qty     = notional / (total_slots * avg_price)
        return self.client.round_qty(raw_qty, self.symbol)

    def _min_contract_margin(self, leverage: float, grid_prices: list) -> float:
        """Approximate margin needed for one minimum-size order at each grid price."""
        contract_value = float(self.client.get_symbol_info(self.symbol)["contract_value"])
        avg_price = sum(grid_prices) / len(grid_prices)
        return (contract_value * avg_price) / leverage

    def _fit_grid_count_to_wallet(
        self,
        mode: GridMode,
        requested_grids: int,
        lower: float,
        upper: float,
        investment: float,
        leverage: float,
    ) -> tuple[int, list[float]]:
        """Reduce grid levels when Delta's minimum contract size exceeds wallet sizing."""
        num_grids = max(1, requested_grids)
        grid_prices = self._calc_grid_prices(lower, upper, num_grids)
        min_margin = self._min_contract_margin(leverage, grid_prices)
        max_orders = int(investment // min_margin) if min_margin > 0 else len(grid_prices)

        if max_orders < 2:
            raise ValueError(
                f"Wallet balance too small for minimum Delta contract size. "
                f"Need about {min_margin:.2f} {cfg.QUOTE_ASSET} per grid order at {leverage}x; "
                f"deployable balance is {investment:.2f} {cfg.QUOTE_ASSET}."
            )

        fitted_grids = num_grids
        while fitted_grids > 1:
            grid_order_slots = fitted_grids + 1
            initial_slots = 0
            if cfg.OPEN_ON_CREATION and mode != GridMode.NEUTRAL:
                initial_slots = max(1, fitted_grids // 4)
            if grid_order_slots + initial_slots <= max_orders:
                break
            fitted_grids -= 1

        grid_order_slots = fitted_grids + 1
        initial_slots = max(1, fitted_grids // 4) if cfg.OPEN_ON_CREATION and mode != GridMode.NEUTRAL else 0
        if grid_order_slots + initial_slots > max_orders:
            raise ValueError(
                f"Wallet balance too small for the minimum grid. Need about "
                f"{(grid_order_slots + initial_slots) * min_margin:.2f} {cfg.QUOTE_ASSET}; "
                f"deployable balance is {investment:.2f} {cfg.QUOTE_ASSET}."
            )

        if fitted_grids < num_grids:
            log.warning(
                "Reducing grids from %s to %s to fit %.2f %s deployable balance",
                num_grids,
                fitted_grids,
                investment,
                cfg.QUOTE_ASSET,
            )
            num_grids = fitted_grids
            grid_prices = self._calc_grid_prices(lower, upper, num_grids)

        return num_grids, grid_prices

    # ── Activate Grid ──────────────────────────────────────────────────
    def activate(self, regime: RegimeResult, current_price: float,
                 lower: float, upper: float, investment: float):
        """Initialize and start a new grid."""
        s = self.state
        s.mode        = regime.mode
        s.lower_price = lower
        s.upper_price = upper
        s.leverage    = regime.leverage
        s.num_grids, s.grid_prices = self._fit_grid_count_to_wallet(
            regime.mode, regime.num_grids, lower, upper, investment, s.leverage
        )
        s.triggered   = not cfg.GRID_TRIGGER_ENABLED
        s.is_active   = True
        s.active_orders = {}
        s.active_order_sides = {}
        s.tp_order_id = 0
        s.sl_order_id = 0
        s.total_fills = 0
        # Ensure no stale open orders block margin type or leverage setup
        self.client.cancel_all_orders(self.symbol)

        # Configure exchange
        self.client.set_margin_type(self.symbol, "CROSSED")
        self.client.set_leverage(self.symbol, s.leverage)

        # Calculate grid
        s.qty_per_grid = self._calc_qty_per_grid(
            investment, s.leverage, s.grid_prices, s.mode, s.num_grids
        )
        profit_pct     = self._profit_per_grid_pct(lower, upper, s.num_grids)

        log.info(f"\n{'═'*56}")
        log.info(f"  GRID ACTIVATED — {s.mode.value}")
        log.info(f"  Range    : {lower:,.1f} → {upper:,.1f}")
        log.info(f"  Grids    : {s.num_grids} | P/Grid: ~{profit_pct}%")
        log.info(f"  Leverage : {s.leverage}x | Qty/Grid: {s.qty_per_grid}")
        log.info(f"  Reason   : {regime.reason}")
        log.info(f"{'═'*56}\n")

        self.alerter.send(
            f"🟢 <b>Grid Activated</b>\n"
            f"Mode: <b>{s.mode.value}</b>\n"
            f"Range: {lower:,.0f} → {upper:,.0f}\n"
            f"Grids: {s.num_grids} | P/Grid: ~{profit_pct}%\n"
            f"Leverage: {s.leverage}x\n"
            f"Reason: {regime.reason}"
        )

        # Open initial position if enabled
        if cfg.OPEN_ON_CREATION and s.mode != GridMode.NEUTRAL:
            self._open_initial_position(current_price)

        # Place grid trigger or immediate orders
        if cfg.GRID_TRIGGER_ENABLED:
            self._place_trigger_order(current_price)
        else:
            self._place_all_grid_orders(current_price)
            self._place_tp_sl(current_price)

    # ── Initial Market Position ────────────────────────────────────────
    def _open_initial_position(self, current_price: float):
        s    = self.state
        side = "BUY" if s.mode == GridMode.LONG else "SELL"
        # Size: proportional to half the grid range
        qty  = max(1, int(s.qty_per_grid * max(1, s.num_grids // 4)))
        try:
            self.client.place_market_order(self.symbol, side, qty)
            log.info(f"📍 Initial {side} position: {qty} @ ~{current_price:,.2f}")
        except Exception as e:
            log.error(f"Initial position error: {e}")

    # ── Grid Trigger ────────────────────────────────────────────────────
    def _place_trigger_order(self, current_price: float):
        s = self.state
        span = s.upper_price - s.lower_price
        # For LONG: trigger when price dips to lower boundary (better entry)
        # For SHORT/NEUTRAL: trigger when price dips into range
        if s.mode == GridMode.LONG:
            trigger_price = s.lower_price * 1.001  # Just above lower
            s.triggered = current_price <= trigger_price + span * 0.1
        elif s.mode == GridMode.SHORT:
            trigger_price = s.upper_price * 0.999
            s.triggered = current_price >= trigger_price - span * 0.1
        else:
            trigger_price = (s.lower_price + s.upper_price) / 2
            s.triggered = abs(current_price - trigger_price) <= span * 0.1

        # We simulate grid trigger by polling; real exchange grid trigger
        # is set via the UI — here we just track it
        if s.triggered:
            log.info(f"⚡ Trigger active: price {current_price:,.2f} within range")
            self._place_all_grid_orders(current_price)
            self._place_tp_sl(current_price)
        else:
            log.info(f"⏳ Waiting for trigger. Price: {current_price:,.2f} | "
                     f"Range: {s.lower_price:,.2f}–{s.upper_price:,.2f}")

    # ── Place All Grid Orders ──────────────────────────────────────────
    def _place_all_grid_orders(self, current_price: float):
        s = self.state
        self.client.cancel_all_orders(self.symbol)
        s.active_orders = {}
        s.active_order_sides = {}

        rounded_prices = [self.client.round_price(price, self.symbol) for price in s.grid_prices]
        below = sorted({p for p in rounded_prices if p < current_price}, reverse=True)
        above = sorted({p for p in rounded_prices if p > current_price})
        pair_count = min(len(below), len(above), max(1, s.num_grids // 2))

        orders_to_place = []
        for p in below[:pair_count]:
            orders_to_place.append(("BUY", p))
        for p in above[:pair_count]:
            orders_to_place.append(("SELL", p))

        skipped = len(rounded_prices) - len(orders_to_place)
        for side, p in sorted(orders_to_place, key=lambda item: item[1]):

            try:
                order = self.client.place_limit_order(
                    self.symbol, side, s.qty_per_grid, p)
                s.active_orders[p] = order["orderId"]
                s.active_order_sides[p] = side
                time.sleep(0.05)
            except Exception as e:
                log.error(f"Grid order {side} @ {p}: {e}")

        if s.active_orders:
            log.info(
                "Grid placed: %s orders (%s BUY / %s SELL)",
                len(s.active_orders),
                pair_count,
                pair_count,
            )
        else:
            log.warning(
                f"⚠️ No grid orders were placed for {self.symbol}. "
                f"Skipped {skipped} nearby prices, and the bot may be waiting for an activation condition or the quantity may be too small."
            )

    # ── TP / SL ────────────────────────────────────────────────────────
    def _place_tp_sl(self, current_price: float):
        s = self.state

        if cfg.SL_ENABLED:
            if s.mode in (GridMode.NEUTRAL, GridMode.LONG):
                sl_price = self.client.round_price(
                    s.lower_price * (1 - cfg.SL_PCT_BELOW_LOWER), self.symbol)
                sl_side = "SELL"
            else:  # SHORT
                sl_price = self.client.round_price(
                    s.upper_price * (1 + cfg.SL_PCT_BELOW_LOWER), self.symbol)
                sl_side = "BUY"
            try:
                order = self.client.place_stop_order(
                    self.symbol, sl_side, s.qty_per_grid * s.num_grids, sl_price)
                s.sl_order_id = order.get("orderId", 0)
                log.info(f"🛑 SL placed @ {sl_price:,.2f}")
            except Exception as e:
                log.error(f"SL placement failed: {e}")

        if cfg.TP_ENABLED:
            if s.mode in (GridMode.NEUTRAL, GridMode.LONG):
                tp_price = self.client.round_price(
                    s.upper_price * (1 + cfg.TP_PCT_ABOVE_UPPER), self.symbol)
                tp_side = "SELL"
            else:  # SHORT
                tp_price = self.client.round_price(
                    s.lower_price * (1 - cfg.TP_PCT_ABOVE_UPPER), self.symbol)
                tp_side = "BUY"
            try:
                order = self.client.place_tp_order(
                    self.symbol, tp_side, s.qty_per_grid * s.num_grids, tp_price)
                s.tp_order_id = order.get("orderId", 0)
                log.info(f"🎯 TP placed @ {tp_price:,.2f}")
            except Exception as e:
                log.error(f"TP placement failed: {e}")

    # ── Refresh Filled Orders ──────────────────────────────────────────
    def check_fills(self, current_price: float):
        """Detect filled grid orders and place counter-orders."""
        s        = self.state
        open_ids = {o["orderId"] for o in self.client.get_open_orders(self.symbol)}

        for price, oid in list(s.active_orders.items()):
            if oid in open_ids:
                continue  # Still open

            # Order was filled
            s.total_fills += 1
            filled_side = s.active_order_sides.pop(price, "")
            del s.active_orders[price]

            idx  = next((i for i, p in enumerate(s.grid_prices) if abs(p - price) < 0.01), -1)
            if idx == -1:
                continue

            # Counter-order logic
            if filled_side == "BUY" and idx < len(s.grid_prices) - 1:
                # BUY was filled → place SELL one grid up
                counter_price = self.client.round_price(s.grid_prices[idx + 1], self.symbol)
                counter_side  = "SELL"
            elif filled_side == "SELL" and idx > 0:
                # SELL was filled → place BUY one grid down
                counter_price = self.client.round_price(s.grid_prices[idx - 1], self.symbol)
                counter_side  = "BUY"
            else:
                continue

            try:
                order = self.client.place_limit_order(
                    self.symbol, counter_side, s.qty_per_grid, counter_price)
                s.active_orders[counter_price] = order["orderId"]
                s.active_order_sides[counter_price] = counter_side
                log.info(f"↺ Fill #{s.total_fills} @ {price:,.2f} → "
                         f"Counter [{counter_side}] @ {counter_price:,.2f}")
            except Exception as e:
                log.error(f"Counter order error: {e}")

    # ── Trailing Logic ─────────────────────────────────────────────────
    def check_trailing(self, current_price: float) -> bool:
        """Returns True if grid was re-placed."""
        if not cfg.TRAILING_ENABLED:
            return False
        s    = self.state
        span = s.upper_price - s.lower_price
        step = current_price * cfg.TRAILING_STEP_PCT
        moved = False

        can_trail_up = s.mode in (GridMode.LONG, GridMode.NEUTRAL)
        can_trail_down = s.mode in (GridMode.SHORT, GridMode.NEUTRAL)

        # Long grids trail upward only. Neutral keeps both directions.
        if can_trail_up and current_price > s.upper_price + current_price * cfg.TRAILING_TRIGGER_PCT:
            s.upper_price = current_price + step
            s.lower_price = s.upper_price - span
            moved = True
            log.info(f"📈 Trailing UP — new range: {s.lower_price:,.0f}–{s.upper_price:,.0f}")
            self.alerter.send(f"📈 <b>Trailing UP</b>\nNew range: {s.lower_price:,.0f}–{s.upper_price:,.0f}")

        # Short grids trail downward only. Neutral keeps both directions.
        elif can_trail_down and current_price < s.lower_price - current_price * cfg.TRAILING_TRIGGER_PCT:
            s.lower_price = current_price - step
            s.upper_price = s.lower_price + span
            moved = True
            log.info(f"📉 Trailing DOWN — new range: {s.lower_price:,.0f}–{s.upper_price:,.0f}")
            self.alerter.send(f"📉 <b>Trailing DOWN</b>\nNew range: {s.lower_price:,.0f}–{s.upper_price:,.0f}")

        if moved:
            s.grid_prices = self._calc_grid_prices(s.lower_price, s.upper_price, s.num_grids)
            self._place_all_grid_orders(current_price)
            self._place_tp_sl(current_price)

        return moved

    # ── TP/SL Hit Detection ────────────────────────────────────────────
    def check_tp_sl_hit(self, current_price: float) -> str | None:
        s = self.state
        if not s.is_active:
            return None

        if cfg.TP_ENABLED:
            if s.mode != GridMode.SHORT and current_price >= s.upper_price * (1 + cfg.TP_PCT_ABOVE_UPPER):
                return "TP"
            if s.mode == GridMode.SHORT and current_price <= s.lower_price * (1 - cfg.TP_PCT_ABOVE_UPPER):
                return "TP"

        if cfg.SL_ENABLED:
            if s.mode != GridMode.SHORT and current_price <= s.lower_price * (1 - cfg.SL_PCT_BELOW_LOWER):
                return "SL"
            if s.mode == GridMode.SHORT and current_price >= s.upper_price * (1 + cfg.SL_PCT_BELOW_LOWER):
                return "SL"
        return None

    # ── Stop Grid ─────────────────────────────────────────────────────
    def stop(self, reason: str = "Manual"):
        s = self.state
        log.info(f"\n{'═'*56}")
        log.info(f"  GRID STOPPED — {reason}")
        log.info(f"  Total fills: {s.total_fills}")
        log.info(f"{'═'*56}\n")

        self.client.cancel_all_orders(self.symbol)

        if cfg.CLOSE_ALL_ON_STOP:
            if self.client.close_all_positions(self.symbol):
                log.info("All positions confirmed closed")

        self.alerter.send(
            f"⛔ <b>Grid Stopped</b>\n"
            f"Reason: {reason}\n"
            f"Fills: {s.total_fills}"
        )
        s.is_active = False

    def get_status(self, current_price: float) -> dict:
        s   = self.state
        pos = self.client.get_position(self.symbol)
        return {
            "price":       current_price,
            "mode":        s.mode.value,
            "lower":       s.lower_price,
            "upper":       s.upper_price,
            "fills":       s.total_fills,
            "orders":      len(s.active_orders),
            "pos_amt":     float(pos.get("positionAmt", 0)),
            "unrealized":  float(pos.get("unRealizedProfit", 0)),
        }
