"""Straight-through entry point for the ETH Futures Grid bot."""

from __future__ import annotations

import logging
import sys
import time

import config as cfg
from alerter import Alerter
from client import DeltaIndiaClient
from grid_engine import GridEngine
from regime_detector import GridMode, RegimeResult
from signal_engine import Signal, get_signal


class SafeConsoleHandler(logging.StreamHandler):
    def emit(self, record):
        try:
            super().emit(record)
        except UnicodeEncodeError:
            msg = self.format(record)
            encoding = getattr(self.stream, "encoding", None) or "utf-8"
            safe = msg.encode(encoding, errors="replace").decode(encoding, errors="replace")
            self.stream.write(safe + self.terminator)
            self.flush()


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s - %(message)s",
    handlers=[
        logging.FileHandler(cfg.LOG_FILE, encoding="utf-8"),
        SafeConsoleHandler(sys.stdout),
    ],
)

log = logging.getLogger("main")


class SimpleGridBot:
    def __init__(self):
        if not cfg.API_KEY or not cfg.API_SECRET:
            raise RuntimeError(
                "Missing Delta Exchange India credentials. Set DELTA_API_KEY and "
                "DELTA_API_SECRET, or DELTA_TESTNET_API_KEY and "
                "DELTA_TESTNET_API_SECRET when testnet is enabled."
            )

        self.client = DeltaIndiaClient()
        self.alerter = Alerter()
        self.engine = GridEngine(self.client, self.alerter)
        self.investment = 0.0
        self.active_direction: str | None = None

    def _sync_investment(self) -> None:
        balance = self.client.get_balance(cfg.QUOTE_ASSET)
        self.investment = max(0.0, balance * cfg.CAPITAL_DEPLOY_PCT)

        log.info("Wallet balance: %.2f %s", balance, cfg.QUOTE_ASSET)
        log.info(
            "Deployable grid capital: %.2f %s (%.0f%% of wallet)",
            self.investment,
            cfg.QUOTE_ASSET,
            cfg.CAPITAL_DEPLOY_PCT * 100,
        )
        if self.investment <= 0:
            raise RuntimeError("No deployable wallet balance for grid orders.")

    def _grid_range(self, price: float, signal: Signal) -> tuple[float, float]:
        if signal.atr_lower > 0 and signal.atr_upper > signal.atr_lower:
            return round(signal.atr_lower, 1), round(signal.atr_upper, 1)

        half = price * cfg.RANGE_PCT_NEUTRAL / 2
        log.warning(
            "ATR range unavailable; falling back to neutral %.2f%% range",
            cfg.RANGE_PCT_NEUTRAL * 100,
        )
        return round(price - half, 1), round(price + half, 1)

    def _regime_from_signal(self, signal: Signal) -> RegimeResult:
        if signal.direction == "NEUTRAL":
            mode = GridMode.NEUTRAL
            leverage = cfg.LEVERAGE_NEUTRAL
        else:
            mode = GridMode.LONG if signal.direction == "LONG" else GridMode.SHORT
            leverage = cfg.LEVERAGE_LONG if signal.direction == "LONG" else cfg.LEVERAGE_SHORT
        return RegimeResult(
            mode=mode,
            bb_bandwidth=signal.bb_bandwidth,
            funding_rate=0.0,
            atr_pct=signal.atr_pct,
            is_squeeze=signal.is_squeeze,
            num_grids=cfg.GRIDS_LOW_VOL,
            leverage=leverage,
            reason=(
                f"Bollinger squeeze; neutral grid"
                if signal.direction == "NEUTRAL"
                else f"4h EMA{cfg.EMA_FAST_PERIOD}/{cfg.EMA_SLOW_PERIOD} trend {signal.direction}"
            ),
        )

    def _activate_from_signal(self, signal: Signal) -> None:
        price = self.client.get_last_price(cfg.SYMBOL)
        lower, upper = self._grid_range(price, signal)
        regime = self._regime_from_signal(signal)

        if self.engine.state.is_active:
            self.engine.stop(f"New signal: {signal.direction}")
            time.sleep(2)

        self._sync_investment()
        self.engine.activate(regime, price, lower, upper, self.investment)
        self.active_direction = signal.direction
        self.alerter.send(
            f"<b>ETH Grid Started</b>\n"
            f"Signal: {regime.reason}\n"
            f"Range: {lower:.2f} - {upper:.2f}\n"
            f"TP/SL enabled: {cfg.TP_ENABLED}/{cfg.SL_ENABLED}"
        )

    def run(self) -> None:
        self._sync_investment()
        log.info(
            "ETH Futures Grid bot running | %s | EMA%s/%s %s",
            cfg.SYMBOL,
            cfg.EMA_FAST_PERIOD,
            cfg.EMA_SLOW_PERIOD,
            cfg.KLINE_INTERVAL,
        )
        self.alerter.send(
            f"<b>ETH Futures Grid Bot Started</b>\n"
            f"Symbol: {cfg.SYMBOL}\n"
            f"Signal: EMA{cfg.EMA_FAST_PERIOD}/{cfg.EMA_SLOW_PERIOD} on {cfg.KLINE_INTERVAL}"
        )

        last_signal_check = 0.0
        last_status_log = 0.0

        while True:
            try:
                now = time.time()
                current_price = self.client.get_last_price(cfg.SYMBOL)

                if now - last_signal_check >= cfg.SIGNAL_CHECK_INTERVAL:
                    last_signal_check = now
                    signal = get_signal(self.client)
                    if signal and signal.direction != self.active_direction:
                        self._activate_from_signal(signal)

                if self.engine.state.is_active:
                    hit = self.engine.check_tp_sl_hit(current_price)
                    if hit:
                        reason = f"{'Take Profit' if hit == 'TP' else 'Stop Loss'} hit @ {current_price:.2f}"
                        self.alerter.send(f"<b>{reason}</b>")
                        self.engine.stop(reason)
                        self.active_direction = None
                        time.sleep(cfg.POLL_INTERVAL_SEC)
                        continue

                    if self.engine.state.triggered:
                        self.engine.check_fills(current_price)
                    else:
                        self.engine._place_trigger_order(current_price)
                    self.engine.check_trailing(current_price)

                if now - last_status_log >= 60:
                    state = "ACTIVE" if self.engine.state.is_active else "WAITING"
                    log.info("%s | %s price %.2f", state, cfg.SYMBOL, current_price)
                    last_status_log = now

                time.sleep(cfg.POLL_INTERVAL_SEC)

            except KeyboardInterrupt:
                log.info("Keyboard interrupt received.")
                if self.engine.state.is_active:
                    self.engine.stop("User stopped bot")
                break
            except Exception as exc:
                log.error("Grid bot loop error: %s", exc, exc_info=True)
                self.alerter.send(f"<b>ETH Grid Error</b>: {str(exc)[:200]}")
                time.sleep(cfg.POLL_INTERVAL_SEC * 2)


def main() -> None:
    log.info("=" * 60)
    log.info("ETH Futures Grid bot starting")
    log.info("=" * 60)
    SimpleGridBot().run()


if __name__ == "__main__":
    main()
