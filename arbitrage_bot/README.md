# ⚡ Funding Rate Arbitrage Bot — Binance USD-M

A fully automated, delta-neutral funding rate arbitrage bot for Binance
USD-M perpetual futures, with a live Dash dashboard.

---

## Strategy

| Condition | Action |
|-----------|--------|
| Funding rate is **negative** | **Long** Perp Futures + **Short** Spot (margin) |
| Entry timing | **5 minutes before** each funding settlement |
| Minimum hold | Until the **first funding settlement** passes (one full cycle) |
| Exit signal | Spread compresses to **≤ 0.1 %** |

**Why it works:**  
Negative funding → shorts pay longs. Holding long perp earns funding income.
Delta-neutral hedge (short spot) eliminates price risk.

References:
- https://www.binance.com/en/support/faq/detail/f330e17d6fc04679b9b21d6f9350e787
- https://www.binance.com/en-IN/blog/tech/3611863022773164727
- https://www.binance.com/en/support/faq/detail/2c65b90111e14be6b0156d32e0ff94d9

---

## Requirements

- Python 3.10+
- Binance account with **Futures + Spot Margin** enabled
- API key with permissions: **Read, Spot & Margin Trading, Futures**

---

## Quick Start

```bash
# 1. Clone / unzip project
cd arbitrage_bot

# 2. Create virtual env
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure
cp .env.example .env
# → Edit .env and fill in your API keys

# 5. Run
python main.py
```

Dashboard opens at **http://localhost:8050**

---

## Configuration (config.py / .env)

| Key | Default | Description |
|-----|---------|-------------|
| `MAX_POSITIONS` | 10 | Max concurrent coins |
| `DEFAULT_LEVERAGE` | 1 | 1× leverage on both legs |
| `ENTRY_BEFORE_SECONDS` | 300 | Enter 5 min before funding |
| `MIN_FUNDING_RATE` | −0.0001 | Only trade if rate < −0.01 % |
| `EXIT_SPREAD_THRESHOLD` | 0.10 | Exit when spread ≤ 0.1 % |
| `STOP_LOSS_PCT` | 5.0 | Stop-loss on futures leg |
| `SPOT_MODE` | margin | `margin` or `futures_only` |

---

## Capital Allocation

```
total_usdt   = spot_free_USDT + futures_available_USDT
per_position = total_usdt / MAX_POSITIONS          (default: ÷ 10)
```

Each leg (futures long + spot short) is sized to `per_position` USDT notional.
With 1× leverage on futures this requires ~`per_position` USDT margin in the
futures wallet, plus `per_position` USDT as cross-margin collateral for the
spot short.

---

## Spot Mode

| Mode | Description |
|------|-------------|
| `margin` | **Recommended.** Long futures + Short spot via cross-margin. Delta-neutral. |
| `futures_only` | Long futures only. Simpler but NOT delta-neutral (directional risk). |

Enable cross-margin at:  
Binance → Wallet → Margin → Enable Cross Margin

---

## Dashboard Panels

| Panel | Description |
|-------|-------------|
| Stat cards | Spot USDT, Futures USDT, per-position size, total realised P&L |
| Opportunities | Top 10 negative-funding coins with countdown timers |
| Active positions | Entry spread, current spread, funding collected, hold status |
| Spread chart | 24-h spread history for all open positions |
| Trade history | Closed trades with full P&L breakdown |
| Logs | Real-time bot event log |

---

## Important Warnings

> **USE AT YOUR OWN RISK.**  
> Crypto trading carries significant financial risk.  
> - Always test on **testnet** first (`USE_TESTNET=true`).  
> - Start with small capital.  
> - Funding rates can flip positive; the bot handles this automatically.  
> - Cross-margin short selling has liquidation risk if prices move sharply.  
> - Spot margin carries borrowing interest which reduces profits.  
> - Indian users: verify that Binance margin trading is permitted in your region.

---

## File Structure

```
arbitrage_bot/
├── .env.example       ← copy to .env and add API keys
├── requirements.txt
├── config.py          ← all tunable parameters
├── api_client.py      ← Binance REST API wrapper (signed)
├── database.py        ← SQLite persistence
├── bot.py             ← strategy + execution engine
├── dashboard.py       ← Plotly Dash UI
├── main.py            ← entry point
└── arbitrage_bot.db   ← created automatically on first run
```
