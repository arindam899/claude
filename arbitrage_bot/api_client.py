"""
api_client.py ─ Thin wrapper around Binance Futures + Spot REST APIs.
Handles HMAC-SHA256 authentication, request signing, and error logging.
"""
import hmac
import hashlib
import time
import urllib.parse
import logging
import requests
from config import Config

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────


class BinanceClient:
    """Thread-safe Binance API client for Spot, Margin, and USD-M Futures."""

    REQUEST_TIMEOUT = 10  # seconds

    def __init__(self):
        self.api_key    = Config.API_KEY
        self.api_secret = Config.API_SECRET
        self.futures_url = Config.FUTURES_BASE
        self.spot_url    = Config.SPOT_BASE

        self._session = requests.Session()
        self._session.headers.update({"X-MBX-APIKEY": self.api_key})

        # Cache for exchange info (step sizes / min qty)
        self._futures_info: dict = {}
        self._spot_info: dict    = {}

    # ── Signing ───────────────────────────────────────────────────────────────

    def _sign(self, params: dict) -> str:
        query = urllib.parse.urlencode(params)
        return hmac.new(
            self.api_secret.encode("utf-8"),
            query.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _ts(self) -> int:
        return int(time.time() * 1000)

    # ── Core HTTP helpers ─────────────────────────────────────────────────────

    def _get(self, base: str, path: str, params: dict = None, signed: bool = False):
        params = dict(params or {})
        if signed:
            params["timestamp"] = self._ts()
            params["signature"] = self._sign(params)
        try:
            r = self._session.get(f"{base}{path}", params=params,
                                  timeout=self.REQUEST_TIMEOUT)
            if not r.ok:
                logger.error(f"GET {path} → {r.status_code}: {r.text[:200]}")
                return None
            return r.json()
        except Exception as exc:
            logger.error(f"GET {path} exception: {exc}")
            return None

    def _post(self, base: str, path: str, params: dict = None):
        params = dict(params or {})
        params["timestamp"] = self._ts()
        params["signature"] = self._sign(params)
        try:
            r = self._session.post(f"{base}{path}", params=params,
                                   timeout=self.REQUEST_TIMEOUT)
            if not r.ok:
                logger.error(f"POST {path} → {r.status_code}: {r.text[:200]}")
                return None
            return r.json()
        except Exception as exc:
            logger.error(f"POST {path} exception: {exc}")
            return None

    # ── Exchange Info / Precision ─────────────────────────────────────────────

    def _load_futures_info(self):
        data = self._get(self.futures_url, "/fapi/v1/exchangeInfo")
        if data and "symbols" in data:
            for s in data["symbols"]:
                step = 1.0
                for f in s.get("filters", []):
                    if f["filterType"] == "LOT_SIZE":
                        step = float(f["stepSize"])
                self._futures_info[s["symbol"]] = {"stepSize": step}

    def _load_spot_info(self):
        data = self._get(self.spot_url, "/api/v3/exchangeInfo")
        if data and "symbols" in data:
            for s in data["symbols"]:
                step = 1.0
                for f in s.get("filters", []):
                    if f["filterType"] == "LOT_SIZE":
                        step = float(f["stepSize"])
                self._spot_info[s["symbol"]] = {"stepSize": step}

    def _floor_qty(self, qty: float, step: float) -> float:
        """Floor quantity to step size precision."""
        if step <= 0:
            return round(qty, 6)
        decimals = max(0, -int(f"{step:.10f}".rstrip("0").find(".") - len(
            f"{step:.10f}".rstrip("0"))) + 1)
        floored = int(qty / step) * step
        return round(floored, decimals)

    def get_futures_qty(self, symbol: str, usdt_amount: float, price: float) -> float:
        if not self._futures_info:
            self._load_futures_info()
        step = self._futures_info.get(symbol, {}).get("stepSize", 1.0)
        raw = usdt_amount / price
        return self._floor_qty(raw, step)

    def get_spot_qty(self, symbol: str, usdt_amount: float, price: float) -> float:
        if not self._spot_info:
            self._load_spot_info()
        step = self._spot_info.get(symbol, {}).get("stepSize", 1.0)
        raw = usdt_amount / price
        return self._floor_qty(raw, step)

    # ── Futures Endpoints ─────────────────────────────────────────────────────

    def get_premium_index(self, symbol: str = None):
        """
        Returns funding rate data for all (or one) USDT-M perpetuals.
        Fields: symbol, markPrice, indexPrice, lastFundingRate, nextFundingTime
        Note: lastFundingRate is used as next-rate proxy; actual estimate
        requires Binance's internal premium-index rolling average.
        """
        params = {}
        if symbol:
            params["symbol"] = symbol
        return self._get(self.futures_url, "/fapi/v1/premiumIndex", params)

    def get_futures_book_ticker(self, symbol: str = None):
        params = {}
        if symbol:
            params["symbol"] = symbol
        return self._get(self.futures_url, "/fapi/v1/ticker/bookTicker", params)

    def get_futures_account(self):
        return self._get(self.futures_url, "/fapi/v2/account", signed=True)

    def set_leverage(self, symbol: str, leverage: int = 1):
        return self._post(self.futures_url, "/fapi/v1/leverage",
                          {"symbol": symbol, "leverage": leverage})

    def futures_order(self, symbol: str, side: str, qty: float,
                      order_type: str = "MARKET"):
        """side: 'BUY' (long) or 'SELL' (close long)."""
        return self._post(self.futures_url, "/fapi/v1/order", {
            "symbol":   symbol,
            "side":     side,
            "type":     order_type,
            "quantity": qty,
        })

    def get_futures_positions(self, symbol: str = None):
        params = {}
        if symbol:
            params["symbol"] = symbol
        return self._get(self.futures_url, "/fapi/v2/positionRisk", params, signed=True)

    def get_funding_income(self, symbol: str = None, start_time_ms: int = None):
        """Fetch FUNDING_FEE income records."""
        params: dict = {"incomeType": "FUNDING_FEE", "limit": 200}
        if symbol:
            params["symbol"] = symbol
        if start_time_ms:
            params["startTime"] = start_time_ms
        return self._get(self.futures_url, "/fapi/v1/income", params, signed=True)

    # ── Spot Endpoints ────────────────────────────────────────────────────────

    def get_spot_book_ticker(self, symbol: str = None):
        params = {}
        if symbol:
            params["symbol"] = symbol
        return self._get(self.spot_url, "/api/v3/ticker/bookTicker", params)

    def get_spot_account(self):
        return self._get(self.spot_url, "/api/v3/account", signed=True)

    # ── Margin Endpoints ──────────────────────────────────────────────────────

    def get_margin_account(self):
        return self._get(self.spot_url, "/sapi/v1/margin/account", signed=True)

    def margin_order(self, symbol: str, side: str, qty: float,
                     side_effect: str = "AUTO_BORROW_REPAY",
                     order_type: str = "MARKET"):
        """
        side_effect:
          AUTO_BORROW_REPAY → borrow coin to sell (short) OR repay on buy-back.
          NO_SIDE_EFFECT    → normal margin trade without auto-borrow.
        """
        return self._post(self.spot_url, "/sapi/v1/margin/order", {
            "symbol":         symbol,
            "side":           side,
            "type":           order_type,
            "quantity":       qty,
            "sideEffectType": side_effect,
            "isIsolated":     "FALSE",  # cross-margin
        })

    # ── Convenience helpers ───────────────────────────────────────────────────

    def get_usdt_balances(self) -> dict:
        """Return {'spot': float, 'futures': float, 'total': float}."""
        spot_usdt = 0.0
        acc = self.get_spot_account()
        if acc and "balances" in acc:
            for b in acc["balances"]:
                if b["asset"] == "USDT":
                    spot_usdt = float(b["free"])
                    break

        futures_usdt = 0.0
        facc = self.get_futures_account()
        if facc and "assets" in facc:
            for a in facc["assets"]:
                if a["asset"] == "USDT":
                    futures_usdt = float(a["availableBalance"])
                    break

        return {
            "spot":    spot_usdt,
            "futures": futures_usdt,
            "total":   spot_usdt + futures_usdt,
        }
