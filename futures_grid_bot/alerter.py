"""Telegram alert module."""
import logging
import requests
import config as cfg

log = logging.getLogger("Alerter")

class Alerter:
    def send(self, msg: str):
        if not cfg.TELEGRAM_TOKEN or not cfg.TELEGRAM_CHAT_ID:
            return
        try:
            requests.post(
                f"https://api.telegram.org/bot{cfg.TELEGRAM_TOKEN}/sendMessage",
                data={"chat_id": cfg.TELEGRAM_CHAT_ID, "text": msg,
                      "parse_mode": "HTML"},
                timeout=5
            )
        except Exception as e:
            log.warning(f"Telegram error: {e}")
