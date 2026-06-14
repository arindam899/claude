# ─────────────────────────────────────────────
#  momentum_scanner / signals.py
#
#  Each detector returns: (triggered: bool, direction: str, score: float, meta: dict)
#  direction is "LONG", "SHORT", or "NEUTRAL"
# ─────────────────────────────────────────────

from __future__ import annotations
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Tuple
import config

# ── Result type ─────────────────────────────
@dataclass
class SignalResult:
    name:      str
    triggered: bool
    direction: str          # "LONG" | "SHORT" | "NEUTRAL"
    score:     float        # 0.0 – 1.0 strength
    meta:      dict = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def __str__(self):
        arrow = "🟢 LONG" if self.direction == "LONG" else ("🔴 SHORT" if self.direction == "SHORT" else "⚪ NEUTRAL")
        return f"[{self.name}] {arrow}  score={self.score:.2f}  {self.meta}"


# ── Per-symbol rolling state ─────────────────
class SymbolState:
    """Holds all rolling history for one symbol."""

    def __init__(self, symbol: str):
        self.symbol = symbol

        # OI history: list of (timestamp, oi_value)
        self.oi_history:      Deque[Tuple[float, float]] = deque(maxlen=20)
        # Funding history: list of (timestamp, rate)
        self.funding_history: Deque[Tuple[float, float]] = deque(maxlen=20)
        # Depth history: list of (timestamp, ask_sum_top_N)
        self.depth_history:   Deque[Tuple[float, float]] = deque(
            maxlen=config.DEPTH_HISTORY_MINUTES * 2 + 5
        )
        # Liquidation events: list of (timestamp, side, value_usd)
        self.liq_events:      Deque[Tuple[float, str, float]] = deque(maxlen=500)
        # Kline cache: list of (open, high, low, close, volume)
        self.klines:          List[Tuple] = []

        # Fired signal log for confluence window
        self.signal_log:      Deque[SignalResult] = deque(maxlen=50)


# ─────────────────────────────────────────────
#  SIGNAL 1 — OI Spike + Funding Shift
# ─────────────────────────────────────────────
def detect_oi_funding(state: SymbolState,
                      current_oi: float,
                      current_funding: float) -> SignalResult:

    now = time.time()
    state.oi_history.append((now, current_oi))
    state.funding_history.append((now, current_funding))

    if len(state.oi_history) < 2 or len(state.funding_history) < 2:
        return SignalResult("OI+Funding", False, "NEUTRAL", 0.0)

    # OI change over last candle (30s poll ≈ every ~10 polls = 5 min)
    prev_oi     = state.oi_history[-2][1]
    oi_change   = (current_oi - prev_oi) / prev_oi * 100 if prev_oi else 0.0

    prev_funding   = state.funding_history[-2][1]
    funding_shift  = current_funding - prev_funding

    oi_triggered      = abs(oi_change) >= config.OI_CHANGE_THRESHOLD_PCT
    funding_triggered = abs(funding_shift) >= config.FUNDING_SHIFT_THRESHOLD

    triggered = oi_triggered and funding_triggered

    # Direction: OI rising + funding rising → longs building → LONG bias
    #            OI rising + funding falling → shorts building → SHORT bias
    if triggered:
        if oi_change > 0 and funding_shift > 0:
            direction = "LONG"
        elif oi_change > 0 and funding_shift < 0:
            direction = "SHORT"
        else:
            direction = "NEUTRAL"
    else:
        direction = "NEUTRAL"

    score = min(1.0, abs(oi_change) / (config.OI_CHANGE_THRESHOLD_PCT * 2)) * 0.5 + \
            min(1.0, abs(funding_shift) / (config.FUNDING_SHIFT_THRESHOLD * 2)) * 0.5

    return SignalResult(
        "OI+Funding", triggered, direction, score,
        meta={
            "oi_change_pct": round(oi_change, 3),
            "funding_shift": round(funding_shift, 6),
            "current_oi":    round(current_oi, 2),
        }
    )


# ─────────────────────────────────────────────
#  SIGNAL 2 — Liquidation Cascade
# ─────────────────────────────────────────────
def add_liquidation_event(state: SymbolState,
                          side: str,
                          value_usd: float) -> None:
    """Called directly from the WebSocket feed."""
    state.liq_events.append((time.time(), side, value_usd))


def detect_liquidation_cascade(state: SymbolState) -> SignalResult:
    now    = time.time()
    cutoff = now - config.LIQ_WINDOW_SECONDS

    recent = [(ts, side, val) for ts, side, val in state.liq_events if ts >= cutoff]

    buy_liq  = sum(val for _, side, val in recent if side == "BUY")   # longs liquidated
    sell_liq = sum(val for _, side, val in recent if side == "SELL")  # shorts liquidated
    total    = buy_liq + sell_liq

    triggered = total >= config.LIQ_VALUE_USD_THRESHOLD

    # After long liquidation cascade → price reverses UP → LONG
    # After short liquidation cascade → price reverses DOWN → SHORT
    if triggered:
        direction = "LONG" if buy_liq > sell_liq else "SHORT"
    else:
        direction = "NEUTRAL"

    score = min(1.0, total / (config.LIQ_VALUE_USD_THRESHOLD * 2))

    return SignalResult(
        "Liquidation", triggered, direction, score,
        meta={
            "total_liq_usd":  round(total, 2),
            "long_liq_usd":   round(buy_liq, 2),
            "short_liq_usd":  round(sell_liq, 2),
            "events_in_window": len(recent),
        }
    )


# ─────────────────────────────────────────────
#  SIGNAL 3 — Order Book Thinning
# ─────────────────────────────────────────────
def detect_book_thinning(state: SymbolState,
                         ask_levels: List[Tuple[float, float]],
                         bid_levels: List[Tuple[float, float]]) -> SignalResult:

    now = time.time()

    ask_sum = sum(qty for _, qty in ask_levels[:config.DEPTH_LEVELS])
    bid_sum = sum(qty for _, qty in bid_levels[:config.DEPTH_LEVELS])
    state.depth_history.append((now, ask_sum, bid_sum))  # type: ignore

    cutoff_10m = now - config.DEPTH_HISTORY_MINUTES * 60
    history_10m = [(a, b) for ts, a, b in state.depth_history  # type: ignore
                   if ts >= cutoff_10m]

    if len(history_10m) < 3:
        return SignalResult("BookThinning", False, "NEUTRAL", 0.0)

    avg_ask = sum(a for a, _ in history_10m) / len(history_10m)
    avg_bid = sum(b for _, b in history_10m) / len(history_10m)

    ask_ratio = ask_sum / avg_ask if avg_ask else 1.0
    bid_ratio = bid_sum / avg_bid if avg_bid else 1.0

    # Asks thinning (supply disappearing) + bids stable/growing → LONG incoming
    ask_thin = ask_ratio < config.DEPTH_THIN_RATIO
    bid_thick = bid_ratio >= 0.9

    # Bids thinning + asks stable → SHORT incoming
    bid_thin = bid_ratio < config.DEPTH_THIN_RATIO
    ask_thick = ask_ratio >= 0.9

    if ask_thin and bid_thick:
        triggered, direction = True, "LONG"
    elif bid_thin and ask_thick:
        triggered, direction = True, "SHORT"
    else:
        triggered, direction = False, "NEUTRAL"

    score = max(0, 1 - ask_ratio) if ask_thin else max(0, 1 - bid_ratio) if bid_thin else 0.0
    score = min(1.0, score / (1 - config.DEPTH_THIN_RATIO))

    return SignalResult(
        "BookThinning", triggered, direction, score,
        meta={
            "ask_ratio_vs_10m_avg": round(ask_ratio, 3),
            "bid_ratio_vs_10m_avg": round(bid_ratio, 3),
            "ask_depth_now":        round(ask_sum, 2),
        }
    )


# ─────────────────────────────────────────────
#  SIGNAL 4 — Taker Buy/Sell Imbalance
# ─────────────────────────────────────────────
def detect_taker_imbalance(state: SymbolState,
                           buy_vol: float,
                           sell_vol: float) -> SignalResult:
    total = buy_vol + sell_vol
    if total == 0:
        return SignalResult("TakerImbalance", False, "NEUTRAL", 0.0)

    ratio = buy_vol / total  # 0.0 – 1.0

    if ratio >= config.TAKER_BUY_BULL_THRESHOLD:
        triggered, direction = True, "LONG"
    elif ratio <= config.TAKER_BUY_BEAR_THRESHOLD:
        triggered, direction = True, "SHORT"
    else:
        triggered, direction = False, "NEUTRAL"

    # Score: distance from neutral (0.5) normalised
    score = min(1.0, abs(ratio - 0.5) / 0.25)

    return SignalResult(
        "TakerImbalance", triggered, direction, score,
        meta={
            "buy_ratio":  round(ratio, 3),
            "buy_vol":    round(buy_vol, 2),
            "sell_vol":   round(sell_vol, 2),
        }
    )


# ─────────────────────────────────────────────
#  SIGNAL 5 — Volume vs Rolling Average
# ─────────────────────────────────────────────
def detect_volume_spike(state: SymbolState,
                        klines: List[Tuple]) -> SignalResult:
    """
    klines: list of (open, high, low, close, volume) — newest last.
    Needs at least VOLUME_LOOKBACK_CANDLES entries.
    """
    state.klines = klines

    if len(klines) < config.VOLUME_LOOKBACK_CANDLES:
        return SignalResult("VolSpike", False, "NEUTRAL", 0.0)

    current_candle  = klines[-1]
    historical      = klines[-(config.VOLUME_LOOKBACK_CANDLES):-1]

    current_vol  = current_candle[4]
    avg_vol      = sum(k[4] for k in historical) / len(historical) if historical else 1
    multiplier   = current_vol / avg_vol if avg_vol else 0

    triggered = multiplier >= config.VOLUME_SPIKE_MULTIPLIER

    # Use candle body direction for bias
    c_open, c_close = current_candle[0], current_candle[3]
    if triggered:
        direction = "LONG" if c_close >= c_open else "SHORT"
    else:
        direction = "NEUTRAL"

    score = min(1.0, (multiplier - 1) / (config.VOLUME_SPIKE_MULTIPLIER * 2))

    return SignalResult(
        "VolSpike", triggered, direction, score,
        meta={
            "vol_multiplier": round(multiplier, 2),
            "current_vol":    round(current_vol, 2),
            "avg_vol_20":     round(avg_vol, 2),
        }
    )


# ─────────────────────────────────────────────
#  SIGNAL 6 — Price Velocity (ROC + Acceleration)
# ─────────────────────────────────────────────
def detect_price_velocity(state: SymbolState,
                          klines: List[Tuple]) -> SignalResult:
    state.klines = klines

    n = config.ROC_CANDLES + 1
    if len(klines) < n:
        return SignalResult("PriceVelocity", False, "NEUTRAL", 0.0)

    recent = klines[-n:]
    closes = [k[3] for k in recent]

    roc_pct = (closes[-1] - closes[0]) / closes[0] * 100 if closes[0] else 0.0

    # Acceleration: each successive candle change > previous
    per_candle_changes = [closes[i+1] - closes[i] for i in range(len(closes)-1)]
    is_accelerating = all(
        abs(per_candle_changes[i+1]) >= abs(per_candle_changes[i])
        for i in range(len(per_candle_changes)-1)
    ) if len(per_candle_changes) > 1 else True

    roc_hit = abs(roc_pct) >= config.ROC_THRESHOLD_PCT
    accel_ok = is_accelerating or not config.ROC_ACCELERATION_REQUIRED
    triggered = roc_hit and accel_ok

    direction = "LONG" if roc_pct > 0 else "SHORT" if roc_pct < 0 else "NEUTRAL"
    if not triggered:
        direction = "NEUTRAL"

    score = min(1.0, abs(roc_pct) / (config.ROC_THRESHOLD_PCT * 3))

    return SignalResult(
        "PriceVelocity", triggered, direction, score,
        meta={
            "roc_pct":          round(roc_pct, 3),
            "is_accelerating":  is_accelerating,
            "closes":           [round(c, 4) for c in closes],
        }
    )


# ─────────────────────────────────────────────
#  CONFLUENCE CHECKER
# ─────────────────────────────────────────────
@dataclass
class ConfluenceAlert:
    symbol:    str
    direction: str
    signals:   List[SignalResult]
    score:     float
    timestamp: float = field(default_factory=time.time)

    @property
    def signal_names(self) -> str:
        return " + ".join(s.name for s in self.signals)

    def summary(self) -> str:
        dir_emoji = "🟢 LONG" if self.direction == "LONG" else "🔴 SHORT"
        lines = [
            f"{'─'*40}",
            f"  🚨 CONFLUENCE ALERT — {self.symbol}",
            f"  Direction : {dir_emoji}",
            f"  Score     : {self.score:.2f} / 1.00",
            f"  Signals   : {self.signal_names}",
            f"  Time      : {time.strftime('%H:%M:%S', time.localtime(self.timestamp))}",
            f"{'─'*40}",
        ]
        for s in self.signals:
            lines.append(f"  [{s.name}]  score={s.score:.2f}  {s.meta}")
        return "\n".join(lines)


def check_confluence(symbol: str,
                     results: List[SignalResult],
                     state: SymbolState) -> Optional[ConfluenceAlert]:
    """
    Collects all fired signals in the rolling window.
    Returns a ConfluenceAlert if MIN_SIGNALS_FOR_ALERT agree on direction.
    """
    now    = time.time()
    cutoff = now - config.SIGNAL_WINDOW_SECONDS

    # Add new triggered signals to log
    for r in results:
        if r.triggered:
            state.signal_log.append(r)

    # Gather signals within window
    recent = [s for s in state.signal_log if s.timestamp >= cutoff and s.triggered]

    for direction in ("LONG", "SHORT"):
        matching = [s for s in recent if s.direction == direction]
        # Deduplicate by signal name (keep highest-score one)
        seen: Dict[str, SignalResult] = {}
        for s in matching:
            if s.name not in seen or s.score > seen[s.name].score:
                seen[s.name] = s
        unique_signals = list(seen.values())

        if len(unique_signals) >= config.MIN_SIGNALS_FOR_ALERT:
            avg_score = sum(s.score for s in unique_signals) / len(unique_signals)
            alert = ConfluenceAlert(
                symbol    = symbol,
                direction = direction,
                signals   = unique_signals,
                score     = avg_score,
            )
            # Clear log so we don't re-alert on the same batch
            state.signal_log.clear()
            return alert

    return None
