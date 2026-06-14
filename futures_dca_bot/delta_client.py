"""
delta_client.py - Thin wrapper around Delta Exchange India REST API.

The rest of the bot expects a small exchange-client interface. This adapter keeps
that interface stable while translating symbols, product ids, contract sizes,
orders, positions, and candles to Delta's API shape.
"""

import hashlib
import hmac
import json
import logging
import time
from decimal import Decimal, ROUND_DOWN
from urllib.parse import urlencode

import requests

logger = logging.getLogger(__name__)


class DeltaAPIError(Exception):
    def __init__(self, error):
        self.error = error
        super().__init__(error)


class DeltaAuthError(DeltaAPIError):
    @property
    def code(self):
        if isinstance(self.error, dict):
            return self.error.get("code")
        return None

    @property
    def client_ip(self):
        if isinstance(self.error, dict):
            context = self.error.get("context") or {}
            return context.get("client_ip")
        return None


class DeltaClient:
    PROD_URL = "https://api.india.delta.exchange"
    TESTNET_URL = "https://cdn-ind.testnet.deltaex.org"

    def __init__(self, api_key: str, api_secret: str, testnet: bool = False):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = self.TESTNET_URL if testnet else self.PROD_URL
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "futures-dca-bot"})
        self._symbol_meta: dict = {}
        if testnet:
            logger.info("Delta Exchange India client using TESTNET / demo endpoint")

    # ------------------------------------------------------------------
    # Low-level REST helpers
    # ------------------------------------------------------------------
    def _sign(self, method: str, path: str, query_string: str, payload: str) -> dict:
        timestamp = str(int(time.time()))
        message = method + timestamp + path + query_string + payload
        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return {
            "api-key": self.api_key,
            "timestamp": timestamp,
            "signature": signature,
            "User-Agent": "futures-dca-bot",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _request(self, method: str, path: str, params=None, body=None, auth: bool = False):
        params = params or {}
        query_string = f"?{urlencode(params)}" if params else ""
        payload = json.dumps(body, separators=(",", ":")) if body is not None else ""
        headers = {"Accept": "application/json", "User-Agent": "futures-dca-bot"}
        if auth:
            headers = self._sign(method, path, query_string, payload)

        response = self.session.request(
            method,
            f"{self.base_url}{path}",
            params=params,
            data=payload if body is not None else None,
            headers=headers,
            timeout=(3, 27),
        )
        try:
            data = response.json()
        except ValueError as exc:
            raise DeltaAPIError(f"Non-JSON response {response.status_code}: {response.text}") from exc

        if not response.ok or data.get("success") is False:
            error = data.get("error") or data
            if auth:
                raise DeltaAuthError(error)
            raise DeltaAPIError(error)
        return data.get("result", data)

    # ------------------------------------------------------------------
    # Symbol metadata and formatting
    # ------------------------------------------------------------------
    def _load_symbol_meta(self, symbol: str):
        if symbol in self._symbol_meta:
            return
        product = self._request("GET", f"/v2/products/{symbol}", auth=False)
        contract_value = Decimal(str(product.get("contract_value") or "1"))
        tick_size = Decimal(str(product.get("tick_size") or "0"))
        if contract_value <= 0:
            contract_value = Decimal("1")
        self._symbol_meta[symbol] = {
            "product_id": int(product["id"]),
            "symbol": product["symbol"],
            "tick_size": tick_size,
            "contract_value": contract_value,
        }

    def _product_id(self, symbol: str) -> int:
        self._load_symbol_meta(symbol)
        return self._symbol_meta[symbol]["product_id"]

    def price_precision(self, symbol: str) -> int:
        self._load_symbol_meta(symbol)
        tick = self._symbol_meta[symbol]["tick_size"]
        return max(0, -tick.as_tuple().exponent)

    def qty_precision(self, symbol: str) -> int:
        return 0

    def _fmt_price(self, symbol: str, price: float) -> str:
        self._load_symbol_meta(symbol)
        return self._floor_to_step(price, self._symbol_meta[symbol]["tick_size"])

    def _contracts_from_base_qty(self, symbol: str, qty: float) -> int:
        self._load_symbol_meta(symbol)
        contract_value = self._symbol_meta[symbol]["contract_value"]
        contracts = (Decimal(str(qty)) / contract_value).to_integral_value(rounding=ROUND_DOWN)
        return max(1, int(contracts))

    def _base_qty_from_contracts(self, symbol: str, contracts) -> float:
        self._load_symbol_meta(symbol)
        return float(Decimal(str(contracts or 0)) * self._symbol_meta[symbol]["contract_value"])

    @staticmethod
    def _floor_to_step(value: float, step: Decimal) -> str:
        value_dec = Decimal(str(value))
        if step <= 0:
            return format(value_dec, "f")
        floored = (value_dec / step).to_integral_value(rounding=ROUND_DOWN) * step
        return format(floored.quantize(step), "f")

    @staticmethod
    def _side(side: str) -> str:
        return side.lower()

    def _normalize_order(self, symbol: str, order: dict) -> dict:
        size = Decimal(str(order.get("size") or 0))
        unfilled = Decimal(str(order.get("unfilled_size") or 0))
        executed_contracts = max(Decimal("0"), size - unfilled)
        avg_price = (
            order.get("average_fill_price")
            or order.get("avg_fill_price")
            or order.get("limit_price")
            or order.get("stop_price")
            or 0
        )
        return {
            **order,
            "orderId": order.get("id"),
            "symbol": order.get("product_symbol", symbol),
            "avgPrice": avg_price,
            "executedQty": self._base_qty_from_contracts(symbol, executed_contracts),
        }

    # ------------------------------------------------------------------
    # Market data
    # ------------------------------------------------------------------
    def get_mark_price(self, symbol: str) -> float:
        ticker = self._request("GET", f"/v2/tickers/{symbol}", auth=False)
        return float(ticker.get("mark_price") or ticker.get("close") or ticker.get("spot_price"))

    def get_klines(self, symbol: str, interval: str, limit: int = 150):
        seconds = self._interval_seconds(interval)
        end = int(time.time())
        start = end - (seconds * (limit + 2))
        candles = self._request(
            "GET",
            "/v2/history/candles",
            params={"resolution": interval, "symbol": symbol, "start": start, "end": end},
            auth=False,
        )
        candles = sorted(candles, key=lambda c: int(c["time"]))[-limit:]
        rows = []
        for candle in candles:
            open_time = int(candle["time"]) * 1000
            close_time = open_time + (seconds * 1000) - 1
            rows.append([
                open_time,
                str(candle["open"]),
                str(candle["high"]),
                str(candle["low"]),
                str(candle["close"]),
                str(candle.get("volume", 0)),
                close_time,
                "0",
                0,
                "0",
                "0",
                "0",
            ])
        return rows

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
    # Account / position
    # ------------------------------------------------------------------
    def get_wallet_balances(self) -> list:
        return self._request("GET", "/v2/wallet/balances", auth=True)

    def get_available_balance(self, asset_symbols=("USD", "USDT", "INR")) -> float:
        wallets = self.get_wallet_balances()
        for wallet in wallets:
            if wallet.get("asset_symbol") in asset_symbols:
                return float(wallet.get("available_balance") or 0)
        return 0.0

    def get_usdt_balance(self) -> float:
        return self.get_available_balance()

    def verify_futures_access(self) -> bool:
        try:
            self._request("GET", "/v2/wallet/balances", auth=True)
            return True
        except DeltaAuthError as e:
            if e.code == "ip_not_whitelisted_for_api_key" and e.client_ip:
                logger.error(
                    "Delta Exchange India API auth failed: %s. Add public IP %s "
                    "to this API key's IP whitelist, then restart the bot.",
                    e.error,
                    e.client_ip,
                )
            else:
                logger.error(
                    "Delta Exchange India API auth failed: %s. Check API key/secret, "
                    "Trading permission, and IP whitelist.",
                    e.error,
                )
            raise
        except Exception as e:
            logger.error(
                "Delta Exchange India API auth failed: %s. Check API key/secret, "
                "Trading permission, and IP whitelist.",
                e,
            )
            return False

    def get_position(self, symbol: str) -> dict:
        product_id = self._product_id(symbol)
        position = self._request(
            "GET", "/v2/positions", params={"product_id": product_id}, auth=True
        )
        size = Decimal(str(position.get("size") or 0))
        signed_qty = self._base_qty_from_contracts(symbol, abs(size))
        if size < 0:
            signed_qty *= -1
        return {
            **position,
            "symbol": symbol,
            "positionAmt": signed_qty,
            "entryPrice": position.get("entry_price") or 0,
        }

    def get_open_orders(self, symbol: str) -> list:
        orders = self._request(
            "GET",
            "/v2/orders",
            params={"product_ids": self._product_id(symbol), "states": "open,pending"},
            auth=True,
        )
        return [self._normalize_order(symbol, order) for order in orders]

    # ------------------------------------------------------------------
    # Account setup
    # ------------------------------------------------------------------
    def set_leverage(self, symbol: str, leverage: int) -> bool:
        try:
            self._request(
                "POST",
                f"/v2/products/{self._product_id(symbol)}/orders/leverage",
                body={"leverage": str(leverage)},
                auth=True,
            )
            logger.info(f"Order leverage set to {leverage}x for {symbol}")
            return True
        except Exception as e:
            logger.warning(f"set_leverage: {e}")
            return False

    def set_margin_type(self, symbol: str, margin_type: str) -> bool:
        logger.info(f"Using Delta Exchange India margin mode for {symbol} ({margin_type})")
        return True

    # ------------------------------------------------------------------
    # Order placement
    # ------------------------------------------------------------------
    def market_order(self, symbol: str, side: str, qty: float) -> dict:
        body = {
            "product_id": self._product_id(symbol),
            "size": self._contracts_from_base_qty(symbol, qty),
            "side": self._side(side),
            "order_type": "market_order",
        }
        return self._normalize_order(symbol, self._request("POST", "/v2/orders", body=body, auth=True))

    def limit_order(self, symbol: str, side: str, price: float, qty: float) -> dict:
        body = {
            "product_id": self._product_id(symbol),
            "limit_price": self._fmt_price(symbol, price),
            "size": self._contracts_from_base_qty(symbol, qty),
            "side": self._side(side),
            "order_type": "limit_order",
            "time_in_force": "gtc",
        }
        return self._normalize_order(symbol, self._request("POST", "/v2/orders", body=body, auth=True))

    def reduce_only_limit_order(self, symbol: str, side: str, price: float, qty: float) -> dict:
        body = {
            "product_id": self._product_id(symbol),
            "limit_price": self._fmt_price(symbol, price),
            "size": self._contracts_from_base_qty(symbol, qty),
            "side": self._side(side),
            "order_type": "limit_order",
            "time_in_force": "gtc",
            "reduce_only": True,
        }
        return self._normalize_order(symbol, self._request("POST", "/v2/orders", body=body, auth=True))

    def take_profit_market(self, symbol: str, side: str, stop_price: float) -> dict:
        return self._stop_order(symbol, side, stop_price, "take_profit_order")

    def stop_market(self, symbol: str, side: str, stop_price: float) -> dict:
        return self._stop_order(symbol, side, stop_price, "stop_loss_order")

    def _stop_order(self, symbol: str, side: str, stop_price: float, stop_order_type: str) -> dict:
        position = self.get_position(symbol)
        qty = abs(float(position.get("positionAmt") or 0))
        if qty <= 0:
            raise DeltaAPIError("Cannot place stop order without an open position")
        body = {
            "product_id": self._product_id(symbol),
            "size": self._contracts_from_base_qty(symbol, qty),
            "side": self._side(side),
            "order_type": "market_order",
            "stop_order_type": stop_order_type,
            "stop_price": self._fmt_price(symbol, stop_price),
            "stop_trigger_method": "mark_price",
            "reduce_only": True,
        }
        return self._normalize_order(symbol, self._request("POST", "/v2/orders", body=body, auth=True))

    # ------------------------------------------------------------------
    # Order management
    # ------------------------------------------------------------------
    def cancel_order(self, symbol: str, order_id):
        try:
            body = {"id": order_id, "product_id": self._product_id(symbol)}
            return self._request("DELETE", "/v2/orders", body=body, auth=True)
        except Exception as e:
            logger.warning(f"cancel_order({order_id}): {e}")

    def cancel_all_orders(self, symbol: str):
        try:
            body = {
                "product_id": self._product_id(symbol),
                "cancel_limit_orders": True,
                "cancel_stop_orders": True,
                "cancel_reduce_only_orders": True,
            }
            self._request("DELETE", "/v2/orders/all", body=body, auth=True)
            logger.info(f"All open orders cancelled for {symbol}")
        except Exception as e:
            logger.warning(f"cancel_all_orders: {e}")

    def get_order(self, symbol: str, order_id) -> dict:
        try:
            order = self._request("GET", f"/v2/orders/{order_id}", auth=True)
            return self._normalize_order(symbol, order)
        except Exception as e:
            logger.warning(f"get_order({order_id}): {e}")
            return {}
