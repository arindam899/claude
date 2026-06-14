# ─────────────────────────────────────────────
#  momentum_scanner / alerts.py
# ─────────────────────────────────────────────

from __future__ import annotations
import asyncio
import logging
import time
from typing import Optional

import aiohttp

import config
from signals import ConfluenceAlert

log = logging.getLogger("alerts")

# ── De-duplication: don't re-alert same symbol within N seconds ──────────────
_last_alert_time: dict[str, float] = {}
ALERT_COOLDOWN_SECONDS = 120


def _is_cooling_down(symbol: str) -> bool:
    last = _last_alert_time.get(symbol, 0)
    return (time.time() - last) < ALERT_COOLDOWN_SECONDS


def _record_alert(symbol: str):
    _last_alert_time[symbol] = time.time()


# ── Telegram sender ───────────────────────────────────────────────────────────

async def send_telegram(message: str) -> bool:
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        return False

    url     = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id":    config.TELEGRAM_CHAT_ID,
        "text":       message,
        "parse_mode": "HTML",
    }

    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=5)) as r:
                if r.status == 200:
                    return True
                body = await r.text()
                log.warning(f"Telegram error {r.status}: {body[:200]}")
    except Exception as e:
        log.error(f"Telegram send failed: {e}")
    return False


# ── Format alert for Telegram ─────────────────────────────────────────────────

def _format_telegram(alert: ConfluenceAlert) -> str:
    dir_icon = "🟢" if alert.direction == "LONG" else "🔴"
    signal_lines = "\n".join(
        f"  • <b>{s.name}</b> (score {s.score:.2f})  {_meta_short(s.meta)}"
        for s in alert.signals
    )

    entry_note = _entry_note(alert)

    return (
        f"{dir_icon} <b>{alert.symbol} — {alert.direction}</b>\n"
        f"Confluence score: <b>{alert.score:.2f}</b>  ({len(alert.signals)} signals)\n\n"
        f"<b>Signals fired:</b>\n{signal_lines}\n\n"
        f"{entry_note}"
        f"\n<i>{time.strftime('%H:%M:%S')} UTC+0</i>"
    )


def _meta_short(meta: dict) -> str:
    """Pick the most informative single field for display."""
    for key in ("vol_multiplier", "roc_pct", "buy_ratio", "ask_ratio_vs_10m_avg",
                "total_liq_usd", "oi_change_pct"):
        if key in meta:
            return f"{key}={meta[key]}"
    return ""


def _entry_note(alert: ConfluenceAlert) -> str:
    """
    Rough entry suggestion — for reference only, not financial advice.
    """
    if alert.direction == "LONG":
        return (
            f"📌 Watch for: breakout above last 5m high\n"
            f"   SL suggestion: -{config.STOP_LOSS_PCT}% | TP: +{config.TAKE_PROFIT_PCT}%\n"
        )
    else:
        return (
            f"📌 Watch for: breakdown below last 5m low\n"
            f"   SL suggestion: +{config.STOP_LOSS_PCT}% | TP: -{config.TAKE_PROFIT_PCT}%\n"
        )


# ── Main dispatch ─────────────────────────────────────────────────────────────

async def dispatch_alert(alert: ConfluenceAlert) -> None:
    symbol = alert.symbol

    if _is_cooling_down(symbol):
        log.debug(f"Alert suppressed (cooldown): {symbol}")
        return

    _record_alert(symbol)

    # Console
    print("\n" + alert.summary())

    # Telegram
    tg_msg  = _format_telegram(alert)
    success = await send_telegram(tg_msg)
    if success:
        log.info(f"Telegram alert sent: {symbol} {alert.direction}")
    else:
        log.warning(f"Telegram alert failed for {symbol}")


# ── Liquidation pre-alert (large single event, below confluence) ──────────────

async def dispatch_liq_watch(symbol: str, side: str, value_usd: float) -> None:
    """
    Called for large-but-sub-threshold liquidation events.
    Just logs — useful for monitoring without noise.
    """
    liq_type = "LONG liquidated" if side == "BUY" else "SHORT liquidated"
    log.info(f"⚡ LIQ WATCH  {symbol}  {liq_type}  ${value_usd:,.0f}")
