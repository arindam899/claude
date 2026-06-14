"""Grid-style EMA, Bollinger squeeze, and ATR signal for the Futures DCA bot."""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass

from delta_client import DeltaClient
from config import (
    ATR_LENGTH,
    ATR_MULTIPLIER,
    BB_PERIOD,
    BB_SQUEEZE_THRESHOLD,
    BB_STD_DEV,
    EMA_FAST,
    EMA_SLOW,
    LOOKBACK_CANDLES,
    SYMBOL,
    TIMEFRAME,
)

logger = logging.getLogger(__name__)

_last_signal_candle: int | None = None


@dataclass
class Signal:
    direction: str  # LONG | SHORT | NEUTRAL
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


def _bb_bandwidth(values: list[float]) -> float:
    if len(values) < BB_PERIOD:
        return 0.0

    window = values[-BB_PERIOD:]
    basis_values = _ema(values, BB_PERIOD)
    basis = basis_values[-1]
    if basis is None or basis <= 0:
        return 0.0

    std_dev = statistics.pstdev(window)
    upper = basis + BB_STD_DEV * std_dev
    lower = basis - BB_STD_DEV * std_dev
    return (upper - lower) / basis


def _atr_range(
    highs: list[float], lows: list[float], closes: list[float]
) -> tuple[float, float, float]:
    if len(closes) < ATR_LENGTH + 1:
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

    atr_values = _ema(true_ranges, ATR_LENGTH)
    atr = atr_values[-1]
    if atr is None or closes[-1] <= 0:
        return 0.0, 0.0, 0.0

    atr_value = atr * ATR_MULTIPLIER
    atr_upper = highs[-1] + atr_value
    atr_lower = lows[-1] - atr_value
    atr_pct = atr_value / closes[-1]
    return atr_lower, atr_upper, atr_pct


def get_signal(client: DeltaClient) -> Signal | None:
    """Return EMA trend state, with Bollinger squeeze mapped to NEUTRAL."""
    global _last_signal_candle

    try:
        klines = client.get_klines(
            SYMBOL,
            TIMEFRAME,
            limit=max(EMA_SLOW * 6, LOOKBACK_CANDLES),
        )
    except Exception as exc:
        logger.error("Signal fetch failed: %s", exc)
        return None

    if len(klines) <= EMA_SLOW + 2:
        logger.info("Waiting for enough %s candles for EMA%s/%s", TIMEFRAME, EMA_FAST, EMA_SLOW)
        return None

    closed = klines[:-1]
    candle_time = int(closed[-1][0])
    highs = [float(k[2]) for k in closed]
    lows = [float(k[3]) for k in closed]
    closes = [float(k[4]) for k in closed]
    fast = _ema(closes, EMA_FAST)
    slow = _ema(closes, EMA_SLOW)

    prev_fast, curr_fast = fast[-2], fast[-1]
    prev_slow, curr_slow = slow[-2], slow[-1]
    if None in (prev_fast, curr_fast, prev_slow, curr_slow):
        return None

    bb_bandwidth = _bb_bandwidth(closes)
    atr_lower, atr_upper, atr_pct = _atr_range(highs, lows, closes)
    is_squeeze = bb_bandwidth > 0 and bb_bandwidth <= BB_SQUEEZE_THRESHOLD
    if is_squeeze:
        logger.info(
            "Bollinger squeeze -> NEUTRAL | bandwidth %.4f <= %.4f | ATR range %.2f - %.2f",
            bb_bandwidth,
            BB_SQUEEZE_THRESHOLD,
            atr_lower,
            atr_upper,
        )
        return Signal(
            direction="NEUTRAL",
            candle_time=candle_time,
            fast_prev=prev_fast,
            fast_curr=curr_fast,
            slow_prev=prev_slow,
            slow_curr=curr_slow,
            bb_bandwidth=bb_bandwidth,
            atr_upper=atr_upper,
            atr_lower=atr_lower,
            atr_pct=atr_pct,
            crossed=False,
            is_squeeze=True,
        )

    direction = None
    if prev_fast <= prev_slow and curr_fast > curr_slow:
        direction = "LONG"
    elif prev_fast >= prev_slow and curr_fast < curr_slow:
        direction = "SHORT"
    elif curr_fast > curr_slow:
        direction = "LONG"
    elif curr_fast < curr_slow:
        direction = "SHORT"

    if direction is None:
        logger.info(
            "EMA neutral | EMA%s %.2f -> %.2f | EMA%s %.2f -> %.2f",
            EMA_FAST,
            prev_fast,
            curr_fast,
            EMA_SLOW,
            prev_slow,
            curr_slow,
        )
        return None

    crossed = (
        (prev_fast <= prev_slow and curr_fast > curr_slow)
        or (prev_fast >= prev_slow and curr_fast < curr_slow)
    )
    if crossed:
        if _last_signal_candle == candle_time:
            logger.info("EMA %s cross already handled for closed candle %s", direction, candle_time)
        else:
            _last_signal_candle = candle_time

    logger.info(
        "EMA trend -> %s | EMA%s %.2f -> %.2f | EMA%s %.2f -> %.2f | BB bandwidth %.4f | ATR range %.2f - %.2f",
        direction,
        EMA_FAST,
        prev_fast,
        curr_fast,
        EMA_SLOW,
        prev_slow,
        curr_slow,
        bb_bandwidth,
        atr_lower,
        atr_upper,
    )
    return Signal(
        direction=direction,
        candle_time=candle_time,
        fast_prev=prev_fast,
        fast_curr=curr_fast,
        slow_prev=prev_slow,
        slow_curr=curr_slow,
        bb_bandwidth=bb_bandwidth,
        atr_upper=atr_upper,
        atr_lower=atr_lower,
        atr_pct=atr_pct,
        crossed=crossed,
        is_squeeze=False,
    )
