# ⚡ Advanced Delta Exchange India Futures Grid Controller

Auto regime detection · Mode switching · Trailing · TP/SL · Telegram · Funding filter

---

## 📁 File Structure

```
grid_system/
├── config.py           ← Edit ONLY this file
├── client.py           ← Delta Exchange India API wrapper
├── regime_detector.py  ← ADX + BB Squeeze + Funding logic
├── grid_engine.py      ← Orders, trailing, TP/SL, fills
├── alerter.py          ← Telegram notifications
└── main.py             ← Entry point (run this)
```

---

## 🚀 Setup

```bash
pip install requests

export DELTA_API_KEY="your_key"
export DELTA_API_SECRET="your_secret"
export TELEGRAM_BOT_TOKEN="your_token"   # optional
export TELEGRAM_CHAT_ID="your_chat_id"  # optional

cd grid_system
python main.py
```

---

## ⚙️ How the Auto Regime Engine Works

```
Every 5 minutes the bot runs this decision tree:

                        ┌─────────────────────────┐
                        │  Fetch 100 x 15m candles │
                        └────────────┬────────────┘
                                     │
                              ┌──────▼──────┐
                              │  BB Squeeze? │
                              │  BW < 0.015 │
                              └──────┬──────┘
                              YES ↓  │ NO
                           ┌────────▼───────────────┐
                           │      PAUSE mode         │
                           │  Wait for breakout dir  │
                           └─────────────────────────┘

                                     │ (no squeeze)
                              ┌──────▼──────┐
                              │   ADX < 22? │
                              └──────┬──────┘
                              YES ↓  │ NO
                        ┌───────────▼──────────┐
                        │   NEUTRAL GRID        │
                        │   Lev: 3x             │
                        │   Range: ±3%          │
                        └──────────────────────┘

                                     │ (ADX > 28)
                         ┌───────────▼──────────────┐
                         │   Trend Direction (EMA)   │
                         └───────┬────────┬──────────┘
                          UP ↓   │        │ DOWN
             ┌─────────────▼──┐  │  ┌─────▼───────────┐
             │  Funding check │  │  │  Funding check   │
             │  if > 0.1%    │  │  │  if < -0.1%      │
             │  → NEUTRAL     │  │  │  → NEUTRAL        │
             │  else LONG     │  │  │  else SHORT       │
             └────────────────┘  │  └─────────────────┘
                                 │
                         (ambiguous ADX)
                        → NEUTRAL (safe default)
```

---

## 📐 Parameter Guide

### Direction
| Mode    | When to Use                        | Leverage |
|---------|------------------------------------|----------|
| NEUTRAL | ADX < 22, sideways chop            | 3x       |
| LONG    | ADX > 28, uptrend, neg. funding    | 5x       |
| SHORT   | ADX > 28, downtrend, pos. funding  | 5x       |

### Range Sizing
| Style    | Range  | Grids | Best For              |
|----------|--------|-------|-----------------------|
| Scalping | ±2–3%  | 40–60 | High volatility BTC   |
| Swing    | ±4–6%  | 20–35 | Normal conditions     |
| Wide     | ±8–10% | 15–20 | Low volatility alts   |

### Leverage Rules
- **NEUTRAL**: 2–3x (safest, grid can absorb moves)
- **LONG/SHORT**: 4–6x (directional, has initial position)
- **NEVER**: >10x for grid trading (liquidation risk too close)

### Trailing
- **Trail UP**: price breaks above upper boundary by `TRAILING_TRIGGER_PCT`
- **Trail DOWN**: price breaks below lower boundary
- Shifts entire grid and replaces all orders automatically
- **Disable** in NEUTRAL markets (breaks range logic)

### TP / SL Placement
```
LONG/NEUTRAL grid:
  TP = upper_price × (1 + TP_PCT_ABOVE_UPPER)   e.g. upper × 1.03
  SL = lower_price × (1 - SL_PCT_BELOW_LOWER)   e.g. lower × 0.975

SHORT grid:
  TP = lower_price × (1 - TP_PCT_ABOVE_UPPER)
  SL = upper_price × (1 + SL_PCT_BELOW_LOWER)
```

### Grid Trigger
Bot waits for price to enter the grid range before placing orders.
This avoids entering mid-range (worst case — all orders on one side).

### Open on Creation
- **LONG**: Opens a small market BUY before grid starts → earns if price rises
- **SHORT**: Opens a small market SELL → earns if price falls
- **NEUTRAL**: No initial position (grid profits from oscillation only)

### Close All on Stop
Always `True`. Prevents orphaned leveraged positions.

### Auto-Add Margin
Keep `False`. If the grid moves against you, auto-add margin delays
your loss signal and can drain your whole account silently.

---

## 💡 Funding Rate Logic

| Funding   | What It Means              | Bot Action           |
|-----------|----------------------------|----------------------|
| > +0.1%   | Market is overleveraged long | Avoid LONG, use NEUTRAL |
| < -0.1%   | Market is overleveraged short | Avoid SHORT, use NEUTRAL |
| -0.1%–0%  | Slight negative             | LONG grid earns funding |
| 0%–+0.1%  | Slight positive             | NEUTRAL or SHORT preferred |

---

## ⏰ Session Guide (UTC)

| Session          | Hours (UTC) | Grid Behavior       |
|------------------|-------------|---------------------|
| Asia             | 00:00–08:00 | 🟢 Best — low volatility |
| London open      | 08:00–10:00 | 🟡 OK — watch for moves |
| US pre-market    | 12:00–13:30 | 🔴 Avoid — news risk |
| US session       | 13:30–20:00 | 🟡 OK if no major news |
| US close         | 20:00–21:00 | 🔴 Avoid             |

---

## ⚠️ Risk Checklist (Before Going Live)

- [ ] Tested on TESTNET for 48+ hours
- [ ] Capital deployed ≤ 40% of total
- [ ] Reserve buffer set (15%+)
- [ ] Stop loss is always set
- [ ] Leverage ≤ 5x
- [ ] Telegram alerts working
- [ ] Session filter reviewed
- [ ] `TESTNET = False` only after above
