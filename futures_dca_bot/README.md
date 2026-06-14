# ⚡ Futures DCA Bot

A **Delta Exchange India** Futures DCA Bot with:
- EMA 5/10 crossover signal on 1H + RSI 14 filter
- Automatic DCA limit order pre-placement
- Dynamic TP/SL that resets on every fill
- Plotly Dash live dashboard
- Telegram alerts
- SQLite trade history

---

## 📁 File Structure

```
futures_dca_bot/
├── config.py          ← ALL settings live here
├── delta_client.py    ← Delta Exchange India API wrapper
├── signal_engine.py   ← EMA crossover + RSI signal
├── dca_engine.py      ← Core DCA bot logic
├── database.py        ← SQLite persistence
├── dashboard.py       ← Plotly Dash UI
├── main.py            ← Entry point
└── requirements.txt
```

---

## 🚀 Quick Start

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Configure `.env`
```bash
USE_TESTNET=true
DELTA_TESTNET_API_KEY=your_demo_api_key
DELTA_TESTNET_API_SECRET=your_demo_api_secret

# For live trading, set USE_TESTNET=false and use:
# DELTA_API_KEY=your_live_api_key
# DELTA_API_SECRET=your_live_api_secret

TELEGRAM_BOT_TOKEN=your_telegram_bot_token   # optional
TELEGRAM_CHAT_ID=your_chat_id
```

### 3. Run
```bash
python main.py
```

Dashboard → **http://localhost:8051**

---

## ⚙️ Key Parameters (`config.py`)

| Parameter | Default | Description |
|-----------|---------|-------------|
| `SYMBOL` | `BTCUSD` | Delta product symbol |
| `LEVERAGE` | `10` | Futures leverage |
| `MARGIN_TYPE` | `ISOLATED` | ISOLATED or CROSSED |
| `PRICE_DEVIATION` | `0.5%` | Gap between DCA levels |
| `TAKE_PROFIT_PCT` | `1.0%` | TP above avg entry |
| `STOP_LOSS_PCT` | `2.0%` | SL below avg entry |
| `MAX_DCA_ORDERS` | `8` | Max DCA entries per round |
| `BASE_ORDER_USDT` | `12` | Base order margin |
| `DCA_ORDER_USDT` | `12` | First DCA margin |
| `DCA_SIZE_MULTIPLIER` | `1.1` | Each DCA 1.1× bigger |
| `WALLET_DEPLOY_PCT` | `0.50` | Max wallet share reserved for one round |
| `MIN_DCA_ORDER_USDT` | `5.0` | Skip capped leftover DCA orders below this margin |
| `EMA_FAST` | `5` | Fast EMA period |
| `EMA_SLOW` | `10` | Slow EMA period |
| `TIMEFRAME` | `1h` | Signal timeframe |

---

## 📊 How the Signal Works

```
Every 60 seconds (when no active round):
  1. Fetch last 100 × 1H candles
  2. Compute EMA 5 and EMA 10
  3. Detect crossover on last CLOSED candle:
       EMA5 crosses ABOVE EMA10  →  LONG signal
       EMA5 crosses BELOW EMA10  →  SHORT signal
  4. RSI 14 filter:
       LONG  valid only if RSI ≥ 50
       SHORT valid only if RSI ≤ 50
  5. If signal passes → start round
```

---

## 🔄 DCA Round Lifecycle

```
Signal detected
     │
     ▼
Market order (BASE_ORDER_USDT margin)
     │
     ▼
Pre-place 8 DCA LIMIT orders at:
  LONG:  entry × (1 - 0.5%), (1 - 1.0%), ..., (1 - 4.0%)
  SHORT: entry × (1 + 0.5%), (1 + 1.0%), ..., (1 + 4.0%)
     │
     ▼
Place TAKE_PROFIT_MARKET @ avg_entry + 1%
Place STOP_MARKET        @ avg_entry - 2%
     │
     ├─ DCA limit fills? → recalc avg entry
     │                   → cancel old TP/SL
     │                   → place new TP/SL
     │
     └─ Position = 0? → round closed
                      → back to signal scanning
```

---

## 🛡️ Risk Notes

1. **This is leverage trading** — a 10% adverse move at 10× wipes your margin.
2. Start with `LEVERAGE = 1` or use a testnet key first.
3. `MARGIN_TYPE = "ISOLATED"` limits losses to your allocated margin.
4. The `STOP_LOSS_PCT = 2%` triggers at 2% below average entry — with 10×
   leverage that's ~20% of your allocated margin.
5. 8 DCA orders at 0.5% steps means the final DCA fires 4% from entry.
   Total capital required ≈ BASE + sum(DCA_i) — ensure your account can cover it.

---

## 🔑 Required API Permissions

On Delta Exchange India, your API key needs:
- ✅ Read Data
- ✅ Trading
- ❌ Enable Withdrawals (NOT needed — never grant this to a bot)

If startup fails with `ip_not_whitelisted_for_api_key`, add the `client_ip`
shown in the log to that API key's IP whitelist on Delta, then restart the bot.

---

## 📡 Telegram Setup

1. Talk to [@BotFather](https://t.me/botfather) → `/newbot`
2. Copy the token into `TELEGRAM_TOKEN`
3. Start a chat with your bot, then visit:
   `https://api.telegram.org/bot<TOKEN>/getUpdates`
4. Copy your `chat.id` into `TELEGRAM_CHAT_ID`
