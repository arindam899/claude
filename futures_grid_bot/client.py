"""
Delta Exchange India REST client for the futures grid bot.

The grid engine still works through its original exchange-client interface:
symbols are strings, prices are floats, sides are BUY/SELL, and quantities
are calculated as underlying coin size. This client translates those calls to
Delta's v2 API, where orders are placed by product_id and integer contracts.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
import time
from decimal import Decimal, ROUND_DOWN
from typing import Any
from urllib.parse import urlencode

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import config as cfg

log = logging.getLogger("DeltaIndiaClient")


class DeltaAPIError(Exception):
    """Raised when Delta Exchange returns an error response."""


class DeltaIndiaClient:
    BASE_LIVE = "https://api.india.delta.exchange"
    BASE_TESTNET = "https://cdn-ind.testnet.deltaex.org"

    def __init__(self):
        self.api_key = cfg.API_KEY
        self.api_secret = cfg.API_SECRET
        self.base_url = cfg.DELTA_BASE_URL.rstrip("/")
        self.session = requests.Session()
        adapter = HTTPAdapter(
            max_retries=Retry(
                total=3,
                backoff_factor=0.5,
                status_forcelist=[429, 500, 502, 503, 504],
                allowed_methods=None,
            )
        )
        self.session.mount("https://", adapter)
        self.session.headers.update(
            {"Accept": "application/json", "Content-Type": "application/json"}
        )
        self._product_cache: dict[str, dict] = {}
        self._symbol_aliases: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Requests / signing
    # ------------------------------------------------------------------
    def _sign(self, method: str, path: str, query: str = "", body: str = "") -> dict:
        timestamp = str(int(time.time()))
        signed_query = f"?{query}" if query else ""
        message = method.upper() + timestamp + path + signed_query + body
        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return {
            "api-key": self.api_key,
            "timestamp": timestamp,
            "signature": signature,
        }

    def _request(
        self,
        method: str,
        path: str,
        params: dict | None = None,
        payload: dict | None = None,
        auth: bool = True,
    ) -> Any:
        query = urlencode(sorted((params or {}).items()))
        body = json.dumps(payload, separators=(",", ":")) if payload else ""
        headers = self._sign(method, path, query, body) if auth else {}
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{query}"

        response = self.session.request(
            method.upper(),
            url,
            headers=headers,
            data=body or None,
            timeout=15,
        )

        try:
            data = response.json()
        except ValueError as exc:
            raise DeltaAPIError(
                f"HTTP {response.status_code}: non-JSON response from {path}"
            ) from exc

        if not response.ok or not data.get("success", True):
            error = data.get("error", {}) if isinstance(data, dict) else {}
            code = error.get("code", f"HTTP_{response.status_code}")
            message = error.get("message", response.text)
            if code == "invalid_api_key":
                message = (
                    f"{message} | base_url={self.base_url}. Live India keys use "
                    "https://api.india.delta.exchange; demo keys use "
                    "https://cdn-ind.testnet.deltaex.org."
                )
            log.error("Delta API error %s: %s", code, message)
            raise DeltaAPIError(f"[{code}] {message}")

        return data.get("result", data)

    # ------------------------------------------------------------------
    # Product / symbol helpers
    # ------------------------------------------------------------------
    def _canonical_symbol(self, symbol: str) -> str:
        if symbol in self._symbol_aliases:
            return self._symbol_aliases[symbol]
        return symbol.upper()

    def _symbol_candidates(self, symbol: str) -> list[str]:
        requested = symbol.upper()
        candidates = [requested]
        if requested.endswith("USDT"):
            candidates.append(requested[:-4] + "USD")
        return candidates

    def _get_product(self, symbol: str) -> dict:
        canonical = self._canonical_symbol(symbol)
        if canonical in self._product_cache:
            return self._product_cache[canonical]

        products = self._request("GET", "/v2/products", auth=False)
        perpetual_types = {"perpetual_futures", "perpetual_future"}
        for candidate in self._symbol_candidates(symbol):
            for product in products if isinstance(products, list) else []:
                if (
                    product.get("symbol", "").upper() == candidate
                    and product.get("contract_type") in perpetual_types
                    and product.get("trading_status") == "operational"
                ):
                    canonical = product["symbol"].upper()
                    self._product_cache[canonical] = product
                    self._symbol_aliases[symbol] = canonical
                    self._symbol_aliases[symbol.upper()] = canonical
                    return product
        raise ValueError(f"Delta India perpetual symbol {symbol} not found")

    def _product_id(self, symbol: str) -> int:
        return int(self._get_product(symbol)["id"])

    def _contract_value(self, symbol: str) -> Decimal:
        value = self._get_product(symbol).get("contract_value") or "1"
        return Decimal(str(value))

    # ------------------------------------------------------------------
    # Market data
    # ------------------------------------------------------------------
    def get_last_price(self, symbol: str) -> float:
        ticker = self._request(
            "GET", f"/v2/tickers/{self._canonical_symbol_for_request(symbol)}", auth=False
        )
        for key in ("close", "mark_price", "spot_price"):
            if ticker.get(key) is not None:
                return float(ticker[key])
        raise ValueError(f"No price returned for {symbol}")

    def get_mark_price(self, symbol: str) -> float:
        ticker = self._request(
            "GET", f"/v2/tickers/{self._canonical_symbol_for_request(symbol)}", auth=False
        )
        return float(ticker.get("mark_price") or ticker.get("close") or 0)

    def get_funding_rate(self, symbol: str) -> float:
        ticker = self._request(
            "GET", f"/v2/tickers/{self._canonical_symbol_for_request(symbol)}", auth=False
        )
        for key in ("funding_rate", "annualized_funding"):
            if ticker.get(key) is not None:
                rate = float(ticker[key])
                return rate / 100 if abs(rate) > 1 else rate

        end = int(time.time())
        start = end - 7 * 24 * 60 * 60
        candles = self._request(
            "GET",
            "/v2/history/candles",
            params={
                "resolution": "1h",
                "symbol": f"FUNDING:{self._canonical_symbol_for_request(symbol)}",
                "start": start,
                "end": end,
            },
            auth=False,
        )
        if candles:
            rate = float(candles[-1].get("close", 0))
            return rate / 100 if abs(rate) > 1 else rate
        return 0.0

    def get_klines(self, symbol: str, interval: str, limit: int = 100) -> list:
        seconds = self._interval_seconds(interval)
        end = int(time.time())
        start = end - seconds * (limit + 5)
        candles = self._request(
            "GET",
            "/v2/history/candles",
            params={
                "resolution": interval,
                "symbol": self._canonical_symbol_for_request(symbol),
                "start": start,
                "end": end,
            },
            auth=False,
        )
        candles = sorted(candles if isinstance(candles, list) else [], key=lambda c: c["time"])
        candles = candles[-limit:]
        return [
            [
                int(c["time"]) * 1000,
                str(c["open"]),
                str(c["high"]),
                str(c["low"]),
                str(c["close"]),
                str(c.get("volume", 0)),
            ]
            for c in candles
        ]

    def get_24h_ticker(self, symbol: str) -> dict:
        return self._request(
            "GET", f"/v2/tickers/{self._canonical_symbol_for_request(symbol)}", auth=False
        )

    def _canonical_symbol_for_request(self, symbol: str) -> str:
        return self._get_product(symbol)["symbol"]

    @staticmethod
    def _interval_seconds(interval: str) -> int:
        unit = interval[-1]
        value = int(interval[:-1])
        if unit == "m":
            return value * 60
        if unit == "h":
            return value * 60 * 60
        if unit == "d":
            return value * 24 * 60 * 60
        if unit == "w":
            return value * 7 * 24 * 60 * 60
        raise ValueError(f"Unsupported Delta candle interval: {interval}")

    # ------------------------------------------------------------------
    # Rounding / account
    # ------------------------------------------------------------------
    def get_symbol_info(self, symbol: str) -> dict:
        product = self._get_product(symbol)
        return {
            "tick_size": str(product.get("tick_size") or "0.5"),
            "step_size": "1",
            "min_qty": 1,
            "min_notional": 0,
            "contract_value": str(product.get("contract_value") or "1"),
        }

    def round_price(self, price: float, symbol: str) -> float:
        tick = Decimal(self.get_symbol_info(symbol)["tick_size"])
        rounded = (Decimal(str(price)) / tick).to_integral_value(rounding=ROUND_DOWN) * tick
        return float(rounded)

    def round_qty(self, qty: float, symbol: str) -> int:
        contracts = Decimal(str(qty)) / self._contract_value(symbol)
        return max(1, int(contracts.to_integral_value(rounding=ROUND_DOWN)))

    def get_balance(self, asset: str = "USD") -> float:
        wallets = self._request("GET", "/v2/wallet/balances")
        for wallet in wallets if isinstance(wallets, list) else []:
            if wallet.get("asset_symbol", "").upper() == asset.upper():
                return float(wallet.get("available_balance") or wallet.get("balance") or 0)
        return 0.0

    def get_position(self, symbol: str) -> dict:
        position = self._request(
            "GET", "/v2/positions", params={"product_id": self._product_id(symbol)}
        )
        size = float(position.get("size", 0) or 0) if isinstance(position, dict) else 0.0
        raw_side = ""
        if isinstance(position, dict):
            for side_key in ("side", "position_side", "entry_side"):
                if position.get(side_key):
                    raw_side = str(position.get(side_key)).lower()
                    break

        if raw_side in {"sell", "short"}:
            signed_size = -abs(size)
        elif raw_side in {"buy", "long"}:
            signed_size = abs(size)
        else:
            signed_size = size

        return {
            "positionAmt": signed_size,
            "positionSide": raw_side,
            "unRealizedProfit": float(position.get("unrealized_pnl", 0) or 0)
            if isinstance(position, dict)
            else 0.0,
            "entryPrice": float(position.get("entry_price", 0) or 0)
            if isinstance(position, dict)
            else 0.0,
        }

    def get_open_orders(self, symbol: str) -> list:
        orders = self._request(
            "GET",
            "/v2/orders",
            params={"product_id": self._product_id(symbol), "state": "open"},
        )
        result = []
        for order in orders if isinstance(orders, list) else []:
            result.append(
                {
                    **order,
                    "orderId": order.get("id"),
                }
            )
        return result

    # ------------------------------------------------------------------
    # Exchange configuration
    # ------------------------------------------------------------------
    def set_leverage(self, symbol: str, leverage: int) -> dict:
        return self._request(
            "POST",
            f"/v2/products/{self._product_id(symbol)}/orders/leverage",
            payload={"leverage": str(leverage)},
        )

    def set_margin_type(self, symbol: str, margin_type: str) -> dict:
        log.info("Delta margin mode is account/product controlled; skipping %s", margin_type)
        return {}

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------
    def place_limit_order(self, symbol: str, side: str, qty: float, price: float) -> dict:
        payload = {
            "product_id": self._product_id(symbol),
            "size": int(qty),
            "side": side.lower(),
            "order_type": "limit_order",
            "limit_price": str(price),
            "time_in_force": "gtc",
        }
        order = self._request("POST", "/v2/orders", payload=payload)
        return {**order, "orderId": order.get("id")}

    def place_market_order(
        self, symbol: str, side: str, qty: float, reduce_only: bool = False
    ) -> dict:
        payload = {
            "product_id": self._product_id(symbol),
            "size": int(qty),
            "side": side.lower(),
            "order_type": "market_order",
            "time_in_force": "gtc",
        }
        if reduce_only:
            payload["reduce_only"] = True
        order = self._request("POST", "/v2/orders", payload=payload)
        return {**order, "orderId": order.get("id")}

    def place_stop_order(self, symbol: str, side: str, qty: float, stop_price: float) -> dict:
        return self._place_trigger_order(
            symbol, side, qty, stop_price, stop_order_type="stop_loss_order"
        )

    def place_tp_order(self, symbol: str, side: str, qty: float, stop_price: float) -> dict:
        return self._place_trigger_order(
            symbol, side, qty, stop_price, stop_order_type="take_profit_order"
        )

    def _place_trigger_order(
        self, symbol: str, side: str, qty: float, stop_price: float, stop_order_type: str
    ) -> dict:
        payload = {
            "product_id": self._product_id(symbol),
            "size": int(math.ceil(qty)),
            "side": side.lower(),
            "order_type": "market_order",
            "stop_order_type": stop_order_type,
            "stop_price": str(stop_price),
            "stop_trigger_method": "mark_price",
            "time_in_force": "gtc",
            "reduce_only": True,
        }
        order = self._request("POST", "/v2/orders", payload=payload)
        return {**order, "orderId": order.get("id")}

    def cancel_all_orders(self, symbol: str) -> dict:
        try:
            return self._request(
                "DELETE",
                "/v2/orders/all",
                payload={"product_id": self._product_id(symbol)},
            )
        except Exception as exc:
            log.warning("Cancel all orders failed: %s", exc)
            return {}

    def cancel_all_algo_orders(self, symbol: str) -> dict:
        return {}

    def cancel_order(self, symbol: str, order_id: int) -> dict:
        return self._request(
            "DELETE",
            "/v2/orders",
            payload={"id": order_id, "product_id": self._product_id(symbol)},
        )

    def close_all_positions(self, symbol: str, timeout_sec: float = 10.0) -> bool:
        pos = self.get_position(symbol)
        amt = float(pos.get("positionAmt", 0))
        if abs(amt) < 1:
            return True
        side = "SELL" if amt > 0 else "BUY"
        qty = int(abs(amt))
        log.info("Closing position: %s %s contracts %s", side, qty, symbol)
        self.place_market_order(symbol, side, qty, reduce_only=True)

        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            time.sleep(0.5)
            remaining = float(self.get_position(symbol).get("positionAmt", 0))
            if abs(remaining) < 1:
                return True

        remaining = float(self.get_position(symbol).get("positionAmt", 0))
        raise RuntimeError(
            f"Position not flat after close attempt: {remaining:g} contracts remain on {symbol}"
        )

