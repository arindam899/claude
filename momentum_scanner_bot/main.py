# ─────────────────────────────────────────────
#  momentum_scanner / main.py
#
#  Run:  python main.py
# ─────────────────────────────────────────────

from __future__ import annotations
import asyncio
import logging
import time
from typing import Dict

import config
import data_feeds as feeds
import signals as sig
import alerts

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level  = logging.INFO,
    format = "%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
    datefmt= "%H:%M:%S",
)
log = logging.getLogger("main")

# ── Global state store ────────────────────────────────────────────────────────
states: Dict[str, sig.SymbolState] = {}

# ── Stats ─────────────────────────────────────────────────────────────────────
_stats = {
    "scans":        0,
    "alerts_fired": 0,
    "start_time":   time.time(),
}


# ─────────────────────────────────────────────
#  Per-symbol scan cycle
# ─────────────────────────────────────────────
async def scan_symbol(symbol: str) -> None:
    state = states[symbol]

    # 1. Fetch all REST data concurrently
    raw = await feeds.poll_symbol(symbol, state)

    results: list[sig.SignalResult] = []

    # 2. Signal 1 — OI + Funding
    if raw["oi"] is not None and raw["funding"] is not None:
        r = sig.detect_oi_funding(state, raw["oi"], raw["funding"])
        results.append(r)
        if r.triggered:
            log.debug(f"  {symbol}  {r}")

    # 3. Signal 2 — Liquidation cascade (data already flowing from WebSocket)
    r = sig.detect_liquidation_cascade(state)
    results.append(r)
    if r.triggered:
        log.debug(f"  {symbol}  {r}")

    # 4. Signal 3 — Order book thinning
    if raw["book"]:
        ask_levels = [(float(p), float(q)) for p, q in raw["book"].get("asks", [])]
        bid_levels = [(float(p), float(q)) for p, q in raw["book"].get("bids", [])]
        r = sig.detect_book_thinning(state, ask_levels, bid_levels)
        results.append(r)
        if r.triggered:
            log.debug(f"  {symbol}  {r}")

    # 5. Signal 4 — Taker imbalance
    #    Prefer real-time aggTrade ratio if that feed is running, else REST
    buy_vol, sell_vol = feeds.get_rolling_taker_ratio(state)
    if buy_vol is None and raw["taker"]:
        buy_vol  = float(raw["taker"].get("buyVol",  0))
        sell_vol = float(raw["taker"].get("sellVol", 0))
    if buy_vol is not None:
        r = sig.detect_taker_imbalance(state, buy_vol, sell_vol)
        results.append(r)
        if r.triggered:
            log.debug(f"  {symbol}  {r}")

    # 6. Signals 5 + 6 — Volume spike + Price velocity (share kline data)
    if raw["klines"]:
        r5 = sig.detect_volume_spike(state, raw["klines"])
        r6 = sig.detect_price_velocity(state, raw["klines"])
        results += [r5, r6]
        for r in (r5, r6):
            if r.triggered:
                log.debug(f"  {symbol}  {r}")

    # 7. Confluence check
    alert = sig.check_confluence(symbol, results, state)
    if alert:
        _stats["alerts_fired"] += 1
        log.info(f"🚨 CONFLUENCE  {symbol}  {alert.direction}  "
                 f"score={alert.score:.2f}  signals={alert.signal_names}")
        await alerts.dispatch_alert(alert)

    _stats["scans"] += 1


# ─────────────────────────────────────────────
#  Scanning loop — all symbols with concurrency cap
# ─────────────────────────────────────────────
async def scanning_loop(symbols: list[str]) -> None:
    sem = asyncio.Semaphore(config.MAX_CONCURRENT_SCANS)

    async def bounded_scan(symbol: str):
        async with sem:
            try:
                await scan_symbol(symbol)
            except Exception as e:
                log.error(f"scan_symbol({symbol}) crashed: {e}", exc_info=True)

    log.info(f"Starting scan loop  {len(symbols)} symbols  "
             f"interval={config.POLL_INTERVAL_SECONDS}s")

    while True:
        cycle_start = time.time()

        tasks = [asyncio.create_task(bounded_scan(s)) for s in symbols]
        await asyncio.gather(*tasks)

        elapsed = time.time() - cycle_start
        uptime  = time.time() - _stats["start_time"]
        log.info(
            f"Cycle done  {len(symbols)} coins  {elapsed:.1f}s  "
            f"alerts={_stats['alerts_fired']}  "
            f"uptime={uptime/60:.0f}m"
        )

        sleep_for = max(0, config.POLL_INTERVAL_SECONDS - elapsed)
        await asyncio.sleep(sleep_for)


# ─────────────────────────────────────────────
#  Liquidation callback
# ─────────────────────────────────────────────
async def on_large_liq(symbol: str, side: str, value_usd: float) -> None:
    await alerts.dispatch_liq_watch(symbol, side, value_usd)


# ─────────────────────────────────────────────
#  Print live dashboard to console
# ─────────────────────────────────────────────
async def dashboard_loop(symbols: list[str]) -> None:
    """Prints a compact live summary every 60 seconds."""
    while True:
        await asyncio.sleep(60)
        uptime = (time.time() - _stats["start_time"]) / 60
        print(f"\n{'═'*52}")
        print(f"  MOMENTUM SCANNER  |  uptime {uptime:.0f}m  |  "
              f"scans {_stats['scans']}  |  alerts {_stats['alerts_fired']}")
        print(f"  Watching {len(symbols)} symbols  |  "
              f"{time.strftime('%H:%M:%S')}")

        # Show any coins with multiple active signals
        active = []
        cutoff = time.time() - config.SIGNAL_WINDOW_SECONDS
        for sym, state in states.items():
            recent = [s for s in state.signal_log if s.timestamp >= cutoff and s.triggered]
            if len(recent) >= 2:
                dirs = set(s.direction for s in recent if s.direction != "NEUTRAL")
                active.append((sym, len(recent), dirs))

        if active:
            print(f"\n  🔥 Heating up ({len(active)} coins with 2+ signals):")
            for sym, cnt, dirs in sorted(active, key=lambda x: -x[1])[:10]:
                print(f"    {sym:<14}  {cnt} signals  {dirs}")
        print(f"{'═'*52}\n")


# ─────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────
async def main() -> None:
    print("=" * 52)
    print("  EARLY MOMENTUM SCANNER — Binance Futures")
    print(f"  Signals required : {config.MIN_SIGNALS_FOR_ALERT}")
    print(f"  Confluence window: {config.SIGNAL_WINDOW_SECONDS}s")
    print(f"  Poll interval    : {config.POLL_INTERVAL_SECONDS}s")
    print("=" * 52)

    # 1. Load symbol list
    log.info("Fetching symbol list…")
    symbols = await feeds.fetch_all_usdt_perp_symbols()
    if not symbols:
        log.error("No symbols found. Check API connectivity.")
        return
    log.info(f"Loaded {len(symbols)} symbols")

    # 2. Init state objects
    for s in symbols:
        states[s] = sig.SymbolState(s)

    # 3. Launch tasks
    tasks = [
        asyncio.create_task(scanning_loop(symbols),        name="scanner"),
        asyncio.create_task(feeds.liquidation_feed(        # WebSocket
            states, on_liq_callback=on_large_liq),         name="liq_ws"),
        asyncio.create_task(dashboard_loop(symbols),       name="dashboard"),
    ]

    log.info("All tasks launched. Ctrl+C to stop.")
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        log.info("Shutting down…")
    finally:
        session = feeds._session
        if session and not session.closed:
            await session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
