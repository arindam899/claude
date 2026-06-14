"""EMA crossover signal for the ETH futures grid bot."""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass

import config as cfg

log = logging.getLogger("Signal")

_last_signal_candle: int | None = None


@dataclass
class Signal:
    direction: str
    candle_time: int
    fast_prev: float
    fast_curr: float
    slow_prev: float
    slow_curr: float
    bb_bandwidth: float = 0.0
    atr_upper: float = 0.0
    atr_lower: float = 0.0
    atr_pct: float = 0.0
    crossed: bool = False
    is_squeeze: bool = False


def _bb_bandwidth(values: list[float]) -> float:
    if len(values) < cfg.BB_PERIOD:
        return 0.0

    window = values[-cfg.BB_PERIOD:]
    basis_values = _ema(values, cfg.BB_PERIOD)
    basis = basis_values[-1]
    if basis is None or basis <= 0:
        return 0.0

    std_dev = statistics.pstdev(window)
    upper = basis + cfg.BB_STD_DEV * std_dev
    lower = basis - cfg.BB_STD_DEV * std_dev
    return (upper - lower) / basis


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


def _atr_range(highs: list[float], lows: list[float], closes: list[float]) -> tuple[float, float, float]:
    if len(closes) < cfg.ATR_LENGTH + 1:
        return 0.0, 0.0, 0.0

    true_ranges = []
    for i in range(len(closes)):
        if i == 0:
            true_ranges.append(highs[i] - lows[i])
        else:
            true_ranges.append(
                max(
                    highs[i] - lows[i],
                    abs(highs[i] - closes[i - 1]),
                    abs(lows[i] - closes[i - 1]),
                )
            )

    atr_values = _ema(true_ranges, cfg.ATR_LENGTH)
    atr = atr_values[-1]
    if atr is None or closes[-1] <= 0:
        return 0.0, 0.0, 0.0

    atr_value = atr * cfg.ATR_MULTIPLIER
    atr_upper = highs[-1] + atr_value
    atr_lower = lows[-1] - atr_value
    atr_pct = atr_value / closes[-1]
    return atr_lower, atr_upper, atr_pct


def get_signal(client) -> Signal | None:
    """Return the current EMA trend state, with Bollinger squeeze as NEUTRAL."""
    global _last_signal_candle

    try:
        klines = client.get_klines(
            cfg.SYMBOL,
            cfg.KLINE_INTERVAL,
            limit=max(cfg.EMA_SLOW_PERIOD * 6, cfg.LOOKBACK_CANDLES),
        )
    except Exception as exc:
        log.error("Signal fetch failed: %s", exc)
        return None

    if len(klines) <= cfg.EMA_SLOW_PERIOD + 2:
        log.info("Waiting for enough %s candles for EMA%s/%s", cfg.KLINE_INTERVAL, cfg.EMA_FAST_PERIOD, cfg.EMA_SLOW_PERIOD)
        return None

    closed = klines[:-1]
    candle_time = int(closed[-1][0])
    highs = [float(k[2]) for k in closed]
    lows = [float(k[3]) for k in closed]
    closes = [float(k[4]) for k in closed]
    fast = _ema(closes, cfg.EMA_FAST_PERIOD)
    slow = _ema(closes, cfg.EMA_SLOW_PERIOD)

    fast_prev, fast_curr = fast[-2], fast[-1]
    slow_prev, slow_curr = slow[-2], slow[-1]
    if None in (fast_prev, fast_curr, slow_prev, slow_curr):
        return None

    bb_bandwidth = _bb_bandwidth(closes)
    atr_lower, atr_upper, atr_pct = _atr_range(highs, lows, closes)
    is_squeeze = bb_bandwidth > 0 and bb_bandwidth <= cfg.BB_SQUEEZE_THRESHOLD
    if is_squeeze:
        log.info(
            "Bollinger squeeze -> NEUTRAL | bandwidth %.4f <= %.4f | ATR range %.2f - %.2f",
            bb_bandwidth,
            cfg.BB_SQUEEZE_THRESHOLD,
            atr_lower,
            atr_upper,
        )
        return Signal(
            direction="NEUTRAL",
            candle_time=candle_time,
            fast_prev=fast_prev,
            fast_curr=fast_curr,
            slow_prev=slow_prev,
            slow_curr=slow_curr,
            bb_bandwidth=bb_bandwidth,
            atr_upper=atr_upper,
            atr_lower=atr_lower,
            atr_pct=atr_pct,
            crossed=False,
            is_squeeze=True,
        )

    direction = None
    if fast_prev <= slow_prev and fast_curr > slow_curr:
        direction = "LONG"
    elif fast_prev >= slow_prev and fast_curr < slow_curr:
        direction = "SHORT"
    elif fast_curr > slow_curr:
        direction = "LONG"
    elif fast_curr < slow_curr:
        direction = "SHORT"

    if direction is None:
        log.info(
            "EMA neutral | EMA%s %.2f -> %.2f | EMA%s %.2f -> %.2f",
            cfg.EMA_FAST_PERIOD,
            fast_prev,
            fast_curr,
            cfg.EMA_SLOW_PERIOD,
            slow_prev,
            slow_curr,
        )
        return None

    crossed = (
        (fast_prev <= slow_prev and fast_curr > slow_curr)
        or (fast_prev >= slow_prev and fast_curr < slow_curr)
    )
    if crossed:
        if _last_signal_candle == candle_time:
            log.info("EMA %s cross already handled for closed candle %s", direction, candle_time)
        else:
            _last_signal_candle = candle_time

    log.info(
        "EMA trend -> %s | EMA%s %.2f -> %.2f | EMA%s %.2f -> %.2f | BB bandwidth %.4f | ATR range %.2f - %.2f",
        direction,
        cfg.EMA_FAST_PERIOD,
        fast_prev,
        fast_curr,
        cfg.EMA_SLOW_PERIOD,
        slow_prev,
        slow_curr,
        bb_bandwidth,
        atr_lower,
        atr_upper,
    )
    return Signal(
        direction=direction,
        candle_time=candle_time,
        fast_prev=fast_prev,
        fast_curr=fast_curr,
        slow_prev=slow_prev,
        slow_curr=slow_curr,
        bb_bandwidth=bb_bandwidth,
        atr_upper=atr_upper,
        atr_lower=atr_lower,
        atr_pct=atr_pct,
        crossed=crossed,
        is_squeeze=False,
    )
