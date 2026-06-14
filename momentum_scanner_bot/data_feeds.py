# ─────────────────────────────────────────────
#  momentum_scanner / data_feeds.py
#
#  Async REST polling + WebSocket streams.
#  All raw data flows into SymbolState objects.
# ─────────────────────────────────────────────

from __future__ import annotations
import asyncio
import json
import logging
import time
from typing import Dict, List, Optional

import aiohttp
import websockets
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK

import config
from signals import SymbolState, add_liquidation_event

log = logging.getLogger("data_feeds")


# ── Shared HTTP session (created once in main) ──────────────────────────────
_session: Optional[aiohttp.ClientSession] = None

def get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession()
    return _session


# ── REST helpers ─────────────────────────────────────────────────────────────

async def _get(path: str, params: dict = None, retries: int = 3) -> Optional[dict | list]:
    url = config.BASE_URL + path
    for attempt in range(retries):
        try:
            async with get_session().get(url, params=params, timeout=aiohttp.ClientTimeout(total=8)) as r:
                if r.status == 200:
                    return await r.json()
                log.warning(f"HTTP {r.status} on {path} (attempt {attempt+1})")
        except asyncio.TimeoutError:
            log.warning(f"Timeout on {path} (attempt {attempt+1})")
        except Exception as e:
            log.warning(f"Request error on {path}: {e}")
        await asyncio.sleep(0.5 * (attempt + 1))
    return None


async def fetch_all_usdt_perp_symbols() -> List[str]:
    """Get every active USD-M perpetual symbol."""
    data = await _get("/fapi/v1/exchangeInfo")
    if not data:
        return []
    symbols = [
        s["symbol"] for s in data.get("symbols", [])
        if s.get("contractType") == "PERPETUAL"
        and s.get("quoteAsset") == "USDT"
        and s.get("status") == "TRADING"
    ]
    if config.WHITELIST:
        symbols = [s for s in symbols if s in config.WHITELIST]
    return sorted(symbols)


async def fetch_open_interest(symbol: str) -> Optional[float]:
    data = await _get("/fapi/v1/openInterest", {"symbol": symbol})
    if data:
        return float(data.get("openInterest", 0))
    return None


async def fetch_funding_rate(symbol: str) -> Optional[float]:
    data = await _get("/fapi/v1/fundingRate", {"symbol": symbol, "limit": 1})
    if data and isinstance(data, list) and data:
        return float(data[0].get("fundingRate", 0))
    # fallback: premiumIndex has lastFundingRate
    data2 = await _get("/fapi/v1/premiumIndex", {"symbol": symbol})
    if data2:
        return float(data2.get("lastFundingRate", 0))
    return None


async def fetch_order_book(symbol: str) -> Optional[dict]:
    return await _get("/fapi/v1/depth", {"symbol": symbol, "limit": config.DEPTH_LEVELS})


async def fetch_taker_ratio(symbol: str) -> Optional[dict]:
    """Returns {"buySellRatio", "buyVol", "sellVol", ...}"""
    data = await _get("/fapi/v1/takerlongshortRatio",
                      {"symbol": symbol, "period": config.TAKER_PERIOD, "limit": 1})
    if data and isinstance(data, list) and data:
        return data[0]
    return None


async def fetch_klines(symbol: str, limit: int = 22) -> Optional[list]:
    """Returns list of [open_time, open, high, low, close, volume, ...]"""
    data = await _get("/fapi/v1/klines", {
        "symbol": symbol,
        "interval": config.KLINE_INTERVAL,
        "limit": limit,
    })
    if not data:
        return None
    # Parse to (open, high, low, close, volume) tuples
    return [
        (float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5]))
        for k in data
    ]


# ── Full single-symbol poll ───────────────────────────────────────────────────

async def poll_symbol(symbol: str, state: SymbolState) -> dict:
    """
    Fire all REST calls for one symbol concurrently.
    Returns dict with raw results.
    """
    oi_task      = asyncio.create_task(fetch_open_interest(symbol))
    funding_task = asyncio.create_task(fetch_funding_rate(symbol))
    book_task    = asyncio.create_task(fetch_order_book(symbol))
    taker_task   = asyncio.create_task(fetch_taker_ratio(symbol))
    kline_task   = asyncio.create_task(fetch_klines(symbol, config.VOLUME_LOOKBACK_CANDLES + 1))

    results = await asyncio.gather(
        oi_task, funding_task, book_task, taker_task, kline_task,
        return_exceptions=True
    )

    oi, funding, book, taker, klines = results

    return {
        "oi":      oi      if not isinstance(oi, Exception) else None,
        "funding": funding if not isinstance(funding, Exception) else None,
        "book":    book    if not isinstance(book, Exception) else None,
        "taker":   taker   if not isinstance(taker, Exception) else None,
        "klines":  klines  if not isinstance(klines, Exception) else None,
    }


# ── WebSocket: Liquidation feed (all symbols) ────────────────────────────────

async def liquidation_feed(states: Dict[str, SymbolState],
                           on_liq_callback=None) -> None:
    """
    Connects to the global force-order stream.
    Dispatches liquidation events to the correct SymbolState.
    Reconnects automatically on drop.
    """
    url = f"{config.WS_BASE}/ws/!forceOrder@arr"

    while True:
        try:
            log.info(f"Connecting to liquidation stream: {url}")
            async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
                log.info("Liquidation WebSocket connected ✓")
                async for raw in ws:
                    try:
                        msg  = json.loads(raw)
                        data = msg.get("o", {})
                        symbol   = data.get("s", "")
                        side     = data.get("S", "")   # BUY = long liquidated, SELL = short liq
                        qty      = float(data.get("z", 0))    # filled qty
                        price    = float(data.get("ap", 0))   # avg price
                        value    = qty * price                 # USD value

                        if symbol in states:
                            add_liquidation_event(states[symbol], side, value)

                        if on_liq_callback and value >= config.LIQ_VALUE_USD_THRESHOLD * 0.3:
                            # Pre-alert on smaller events (30% of threshold) for monitoring
                            await on_liq_callback(symbol, side, value)

                    except (json.JSONDecodeError, KeyError, ValueError) as e:
                        log.debug(f"Liq parse error: {e}")

        except (ConnectionClosedError, ConnectionClosedOK) as e:
            log.warning(f"Liquidation WS closed ({e}), reconnecting in 3s…")
        except Exception as e:
            log.error(f"Liquidation WS error: {e}, reconnecting in 5s…")
            await asyncio.sleep(2)
        await asyncio.sleep(3)


# ── WebSocket: Individual aggTrade (optional, for real-time taker ratio) ─────

async def agg_trade_feed(symbol: str,
                         state: SymbolState,
                         window_seconds: int = 300) -> None:
    """
    Real-time taker buy/sell volume tracker via aggTrade stream.
    Updates state.taker_buy / state.taker_sell rolling buckets.
    (Only use for top-priority coins to avoid too many open sockets.)
    """
    url = f"{config.WS_BASE}/ws/{symbol.lower()}@aggTrade"

    # Attach rolling buckets to state dynamically
    if not hasattr(state, "agg_trades"):
        from collections import deque
        state.agg_trades = deque(maxlen=10_000)  # type: ignore

    while True:
        try:
            async with websockets.connect(url, ping_interval=20) as ws:
                async for raw in ws:
                    msg   = json.loads(raw)
                    price = float(msg["p"])
                    qty   = float(msg["q"])
                    # m=True means the buyer is the market maker → seller-initiated (SELL taker)
                    is_buyer_maker = msg["m"]
                    side = "SELL" if is_buyer_maker else "BUY"
                    state.agg_trades.append((time.time(), side, qty * price))  # type: ignore

        except Exception as e:
            log.warning(f"aggTrade WS {symbol} error: {e}, reconnecting…")
        await asyncio.sleep(3)


def get_rolling_taker_ratio(state: SymbolState, window_seconds: int = 300):
    """Compute buy ratio from aggTrade deque (if feed is running)."""
    if not hasattr(state, "agg_trades"):
        return None, None

    cutoff = time.time() - window_seconds
    recent = [(side, val) for ts, side, val in state.agg_trades  # type: ignore
              if ts >= cutoff]
    if not recent:
        return None, None

    buy_vol  = sum(v for s, v in recent if s == "BUY")
    sell_vol = sum(v for s, v in recent if s == "SELL")
    return buy_vol, sell_vol
