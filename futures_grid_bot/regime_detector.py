from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass
from enum import Enum

import config as cfg

log = logging.getLogger("RegimeDetector")


class GridMode(Enum):
    NEUTRAL = "NEUTRAL"
    LONG = "LONG"
    SHORT = "SHORT"
    PAUSE = "PAUSE"


@dataclass
class RegimeResult:
    mode: GridMode
    bb_bandwidth: float
    funding_rate: float
    atr_pct: float
    is_squeeze: bool
    num_grids: int
    leverage: int
    reason: str
    atr_upper: float = 0.0
    atr_lower: float = 0.0


def _ema(values: list[float], period: int) -> list[float | None]:
    if len(values) < period:
        return [None] * len(values)

    k = 2 / (period + 1)
    ema = sum(values[:period]) / period
    result: list[float | None] = [None] * (period - 1) + [ema]
    for value in values[period:]:
        ema = value * k + ema * (1 - k)
        result.append(ema)
    return result


def _latest_ema(values: list[float], period: int) -> float:
    ema_values = _ema(values, period)
    latest = next((value for value in reversed(ema_values) if value is not None), None)
    if latest is None:
        raise ValueError(f"Need at least {period} closed candles for EMA{period}")
    return latest


def calc_atr_stop_range(highs: list[float], lows: list[float], closes: list[float]) -> tuple[float, float, float]:
    true_ranges = []
    for i in range(len(closes)):
        if i == 0:
            true_ranges.append(highs[i] - lows[i])
        else:
            true_ranges.append(max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            ))
    if len(true_ranges) < cfg.ATR_LENGTH or closes[-1] <= 0:
        return 0.0, 0.0, 0.0

    atr_values = _ema(true_ranges, cfg.ATR_LENGTH)
    atr = atr_values[-1]
    if atr is None:
        return 0.0, 0.0, 0.0

    atr_value = atr * cfg.ATR_MULTIPLIER
    atr_upper = highs[-1] + atr_value
    atr_lower = lows[-1] - atr_value
    atr_pct = atr_value / closes[-1]
    return round(atr_lower, 1), round(atr_upper, 1), round(atr_pct, 5)


def calc_bb_bandwidth(closes: list[float]) -> float:
    if len(closes) < cfg.BB_PERIOD:
        return 0.0

    window = closes[-cfg.BB_PERIOD:]
    basis = _latest_ema(closes, cfg.BB_PERIOD)
    if basis <= 0:
        return 0.0

    std_dev = statistics.pstdev(window)
    upper = basis + cfg.BB_STD_DEV * std_dev
    lower = basis - cfg.BB_STD_DEV * std_dev
    return round((upper - lower) / basis, 5)


class RegimeDetector:
    """Long/short/neutral grid direction from 4h EMA and Bollinger squeeze."""

    def __init__(self, client):
        self.client = client

    def fetch_klines(self) -> tuple[list[float], list[float], list[float], list[float]]:
        klines = self.client.get_klines(
            cfg.SYMBOL, cfg.KLINE_INTERVAL, cfg.LOOKBACK_CANDLES
        )

        # Delta returns the still-forming candle. Use only closed candles so
        # the 4h EMA signal does not repaint inside the active bar.
        min_needed = max(cfg.EMA_FAST_PERIOD, cfg.EMA_SLOW_PERIOD)
        if len(klines) > min_needed + 1:
            klines = klines[:-1]

        opens = [float(k[1]) for k in klines]
        highs = [float(k[2]) for k in klines]
        lows = [float(k[3]) for k in klines]
        closes = [float(k[4]) for k in klines]
        return opens, highs, lows, closes

    def detect(self, funding_rate: float) -> RegimeResult:
        _opens, highs, lows, closes = self.fetch_klines()
        ema_fast = _latest_ema(closes, cfg.EMA_FAST_PERIOD)
        ema_slow = _latest_ema(closes, cfg.EMA_SLOW_PERIOD)
        atr_lower, atr_upper, atr_pct = calc_atr_stop_range(highs, lows, closes)
        bb_bandwidth = calc_bb_bandwidth(closes)
        is_squeeze = bb_bandwidth > 0 and bb_bandwidth <= cfg.BB_SQUEEZE_THRESHOLD
        high_vol = atr_pct > cfg.VOL_HIGH_THRESHOLD
        num_grids = cfg.GRIDS_HIGH_VOL if high_vol else cfg.GRIDS_LOW_VOL

        log.info(
            f"[EMA 4h] EMA{cfg.EMA_FAST_PERIOD}={ema_fast:,.2f} | "
            f"EMA{cfg.EMA_SLOW_PERIOD}={ema_slow:,.2f} | "
            f"ATR%={atr_pct:.4f} | BB width={bb_bandwidth:.4f} | Funding={funding_rate*100:.4f}%"
        )

        if is_squeeze:
            return RegimeResult(
                mode=GridMode.NEUTRAL,
                bb_bandwidth=bb_bandwidth,
                funding_rate=funding_rate,
                atr_pct=atr_pct,
                is_squeeze=True,
                num_grids=num_grids,
                leverage=cfg.LEVERAGE_NEUTRAL,
                reason=(
                    f"Bollinger squeeze ({bb_bandwidth:.4f} <= "
                    f"{cfg.BB_SQUEEZE_THRESHOLD:.4f}) -> NEUTRAL grid with trailing both ways"
                ),
                atr_upper=atr_upper,
                atr_lower=atr_lower,
            )

        if ema_fast > ema_slow:
            return RegimeResult(
                mode=GridMode.LONG,
                bb_bandwidth=bb_bandwidth,
                funding_rate=funding_rate,
                atr_pct=atr_pct,
                is_squeeze=False,
                num_grids=num_grids,
                leverage=cfg.LEVERAGE_LONG,
                reason=(
                    f"4h EMA{cfg.EMA_FAST_PERIOD} above EMA{cfg.EMA_SLOW_PERIOD} "
                    f"({ema_fast:,.2f} > {ema_slow:,.2f}) -> LONG grid with trailing up"
                ),
                atr_upper=atr_upper,
                atr_lower=atr_lower,
            )

        if ema_fast < ema_slow:
            return RegimeResult(
                mode=GridMode.SHORT,
                bb_bandwidth=bb_bandwidth,
                funding_rate=funding_rate,
                atr_pct=atr_pct,
                is_squeeze=False,
                num_grids=num_grids,
                leverage=cfg.LEVERAGE_SHORT,
                reason=(
                    f"4h EMA{cfg.EMA_FAST_PERIOD} below EMA{cfg.EMA_SLOW_PERIOD} "
                    f"({ema_fast:,.2f} < {ema_slow:,.2f}) -> SHORT grid with trailing down"
                ),
                atr_upper=atr_upper,
                atr_lower=atr_lower,
            )

        return RegimeResult(
            mode=GridMode.PAUSE,
            bb_bandwidth=bb_bandwidth,
            funding_rate=funding_rate,
            atr_pct=atr_pct,
            is_squeeze=False,
            num_grids=num_grids,
            leverage=cfg.LEVERAGE_NEUTRAL,
            reason=(
                f"4h EMA{cfg.EMA_FAST_PERIOD} equals EMA{cfg.EMA_SLOW_PERIOD}; waiting"
            ),
            atr_upper=atr_upper,
            atr_lower=atr_lower,
        )

    def calc_grid_range(self, current_price: float,
                        regime: RegimeResult) -> tuple[float, float]:
        if regime.atr_lower > 0 and regime.atr_upper > regime.atr_lower:
            return regime.atr_lower, regime.atr_upper

        if regime.mode in (GridMode.LONG, GridMode.SHORT):
            half = current_price * cfg.RANGE_PCT_TRENDING / 2
        else:
            half = current_price * cfg.RANGE_PCT_NEUTRAL / 2

        if regime.mode == GridMode.LONG:
            lower = current_price - half * 0.4
            upper = current_price + half * 1.6
        elif regime.mode == GridMode.SHORT:
            lower = current_price - half * 1.6
            upper = current_price + half * 0.4
        else:
            lower = current_price - half
            upper = current_price + half

        return round(lower, 1), round(upper, 1)
