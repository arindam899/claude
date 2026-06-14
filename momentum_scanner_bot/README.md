# Momentum Scanner — Early Signal Detection for Binance Futures

## Setup
```bash
pip install -r requirements.txt
```

Edit `config.py`:
- Add your Binance API_KEY / API_SECRET (read-only is enough — no trading yet)
- Add your Telegram bot token + chat ID
- Optionally populate WHITELIST with your 533-coin list

## Run
```bash
python main.py
```

## Signal Pipeline
```
Signal 1  OI Spike + Funding Shift      ← –15 to –9 min before Top Movers
Signal 2  Liquidation Cascade (WebSocket) ← –13 to –7 min
Signal 3  Order Book Thinning (ask wall)  ← –11 to –5 min
Signal 4  Taker Buy/Sell Imbalance        ← –9  to –3 min
Signal 5  Volume × Rolling Avg Spike      ← –6  to –2 min
Signal 6  Price Velocity + Acceleration   ← –4  to –1 min
```

Alert fires when **3 or more signals agree on the same direction within 120 seconds**.

## Tuning (config.py)
| Parameter | Default | Effect |
|-----------|---------|--------|
| MIN_SIGNALS_FOR_ALERT | 3 | Lower = more alerts, more noise |
| SIGNAL_WINDOW_SECONDS | 120 | Wider = more lax confluence timing |
| OI_CHANGE_THRESHOLD_PCT | 3.0 | Lower = catch smaller OI moves |
| LIQ_VALUE_USD_THRESHOLD | 50,000 | Lower for smaller-cap coins |
| VOLUME_SPIKE_MULTIPLIER | 2.5 | Lower = more volume alerts |
| POLL_INTERVAL_SECONDS | 30 | Lower = faster but more API weight |

## Architecture
```
main.py          — asyncio orchestrator, launches all tasks
signals.py       — 6 signal detectors + confluence checker
data_feeds.py    — REST polling + liquidation WebSocket
alerts.py        — Telegram dispatch + console output
config.py        — all thresholds in one place
```

## Notes
- Liquidation stream uses a SINGLE WebSocket for ALL symbols (Binance supports this)
- REST calls are fully async + concurrent (up to MAX_CONCURRENT_SCANS in parallel)
- Per-symbol cooldown prevents duplicate alerts on the same coin within 2 minutes
- All state is in-memory; add SQLite (from your arb bot) if you want persistence
