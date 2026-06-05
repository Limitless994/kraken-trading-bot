"""
kraken_client.py — Thin wrapper around the Kraken REST API.

Public methods (no auth): get_system_status, get_ticker, get_ohlc, get_asset_pairs
Private methods (auth required): get_balance, add_order, cancel_order,
                                  get_open_orders, get_closed_orders
"""
from __future__ import annotations

import base64
import functools
import hashlib
import hmac
import time
import urllib.parse
import requests
from typing import Any

from . import config

BASE_URL = "https://api.kraken.com"
_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": "KrakenTradingBot/1.0"})

import logging as _logging
_kc_logger = _logging.getLogger(__name__)


def _with_retry(func):
    """Retry transient Kraken errors: max 3 attempts, backoff 0.2→0.4→0.8 s.

    Retries on: requests.Timeout, requests.ConnectionError, HTTP 5xx.
    Does NOT retry on HTTP 4xx (logical client errors).
    After the 3rd failure the original exception is re-raised.
    """
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        delay = 0.2
        last_exc = None
        for attempt in range(3):
            try:
                return func(*args, **kwargs)
            except (requests.Timeout, requests.ConnectionError) as exc:
                last_exc = exc
                _kc_logger.warning("%s: transient error (attempt %d/3): %s", func.__name__, attempt + 1, exc)
            except requests.HTTPError as exc:
                if exc.response is not None and exc.response.status_code >= 500:
                    last_exc = exc
                    _kc_logger.warning("%s: HTTP %d (attempt %d/3)", func.__name__, exc.response.status_code, attempt + 1)
                else:
                    raise   # 4xx — logic error, do not retry
            if attempt < 2:
                time.sleep(delay)
                delay *= 2
        raise last_exc
    return wrapper


# ── Helpers ───────────────────────────────────────────────────────────────────

@_with_retry
def _public(endpoint: str, params: dict | None = None) -> dict:
    url = f"{BASE_URL}/0/public/{endpoint}"
    resp = _SESSION.get(url, params=params or {}, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    if data.get("error"):
        raise RuntimeError(f"Kraken public error [{endpoint}]: {data['error']}")
    return data["result"]


def _sign(api_path: str, post_data_str: str, nonce: str) -> str:
    """HMAC-SHA512 signature as required by Kraken private API."""
    message = (nonce + post_data_str).encode()
    sha256_hash = hashlib.sha256(message).digest()
    secret_bytes = base64.b64decode(config.KRAKEN_API_SECRET)
    hmac_data = api_path.encode() + sha256_hash
    return base64.b64encode(
        hmac.new(secret_bytes, hmac_data, hashlib.sha512).digest()
    ).decode()


@_with_retry
def _private(endpoint: str, params: dict | None = None) -> dict:
    if not config.KRAKEN_API_KEY or not config.KRAKEN_API_SECRET:
        raise RuntimeError("Kraken API credentials not set in .env")

    api_path = f"/0/private/{endpoint}"
    url = f"{BASE_URL}{api_path}"
    nonce = str(int(time.time() * 1000))

    data: dict[str, Any] = dict(params or {})
    data["nonce"] = nonce
    post_data_str = urllib.parse.urlencode(data)

    sign = _sign(api_path, post_data_str, nonce)
    headers = {
        "API-Key":  config.KRAKEN_API_KEY,
        "API-Sign": sign,
        "Content-Type": "application/x-www-form-urlencoded",
    }

    resp = _SESSION.post(url, data=post_data_str, headers=headers, timeout=10)
    resp.raise_for_status()
    result = resp.json()
    if result.get("error"):
        raise RuntimeError(f"Kraken private error [{endpoint}]: {result['error']}")
    return result["result"]


def _result_key(result: dict) -> str:
    """Return the first key that is not 'last' (Kraken paginates OHLC with 'last')."""
    for k in result:
        if k != "last":
            return k
    raise KeyError(f"No data key in result. Keys: {list(result.keys())}")


def format_volume(pair: str, volume: float) -> str:
    decimals = config.PAIR_VOLUME_DECIMALS.get(pair, 6)
    return f"{volume:.{decimals}f}"


# ── Public endpoints ──────────────────────────────────────────────────────────

# Possible values: "online" | "cancel_only" | "post_only" | "limit_only" | "maintenance"
KRAKEN_ONLINE = "online"

def get_system_status() -> tuple[str, str]:
    """
    Returns (status, timestamp).
    status is one of: online, cancel_only, post_only, limit_only, maintenance
    Raises on network failure.
    """
    result = _public("SystemStatus")
    return result["status"], result["timestamp"]


def get_ticker(pair: str) -> dict:
    """
    Returns normalised ticker data for a single pair.
    Keys: pair, ask, bid, last, volume_24h, vwap_24h, high_24h, low_24h, trades_24h
    """
    result = _public("Ticker", {"pair": pair})
    key = _result_key(result)
    t = result[key]
    return {
        "pair":       pair,
        "ask":        float(t["a"][0]),
        "bid":        float(t["b"][0]),
        "last":       float(t["c"][0]),
        "volume_24h": float(t["v"][1]),
        "vwap_24h":   float(t["p"][1]),
        "high_24h":   float(t["h"][1]),
        "low_24h":    float(t["l"][1]),
        "trades_24h": int(t["t"][1]),
    }


def get_ohlc(pair: str, interval: int = 60, since: int | None = None) -> list[dict]:
    """
    Returns completed OHLCV candles (last candle excluded — it's still forming).

    interval values: 1 | 5 | 15 | 30 | 60 | 240 | 1440 minutes
    Kraken returns up to 720 candles per call.
    """
    params: dict[str, Any] = {"pair": pair, "interval": interval}
    if since:
        params["since"] = since
    result = _public("OHLC", params)
    key = _result_key(result)
    raw = result[key]
    # drop the last (incomplete) candle
    candles = raw[:-1] if len(raw) > 1 else raw
    return [
        {
            "time":   int(c[0]),
            "open":   float(c[1]),
            "high":   float(c[2]),
            "low":    float(c[3]),
            "close":  float(c[4]),
            "vwap":   float(c[5]),
            "volume": float(c[6]),
            "trades": int(c[7]),
        }
        for c in candles
    ]


def get_asset_pairs(pairs: list[str] | None = None) -> dict:
    """Returns pair info (useful for min order size, decimal precision)."""
    params = {}
    if pairs:
        params["pair"] = ",".join(pairs)
    return _public("AssetPairs", params)


# ── Private endpoints ─────────────────────────────────────────────────────────

def get_balance() -> dict[str, float]:
    """Returns {currency: balance} for all currencies with non-zero balance."""
    raw = _private("Balance")
    return {k: float(v) for k, v in raw.items() if float(v) > 0}


def add_order(
    pair: str,
    side: str,          # "buy" | "sell"
    order_type: str,    # "market" | "limit"
    volume: str,        # string to preserve precision
    price: str | None = None,
    validate: bool = False,
    leverage: int | None = None,   # per margin orders (short)
) -> dict:
    """
    Place a new order. Set validate=True to dry-run without executing.
    Returns Kraken's order result dict (txid, descr).
    """
    params: dict[str, Any] = {
        "pair":      pair,
        "type":      side,
        "ordertype": order_type,
        "volume":    volume,
    }
    if price:
        params["price"] = price
    if validate:
        params["validate"] = "true"
    if leverage:
        params["leverage"] = str(leverage)
    try:
        return _private("AddOrder", params)
    except Exception as exc:
        _kc_logger.error("add_order(%s %s) failed after retries: %s — checking for ghost orders", side, pair, exc)
        try:
            open_data = get_open_orders()
            open_ids = list(open_data.get("open", {}).keys())
            if open_ids:
                _kc_logger.warning(
                    "add_order failed but %d open order(s) found on Kraken — verify manually: %s",
                    len(open_ids), open_ids,
                )
        except Exception as check_exc:
            _kc_logger.debug("Cannot verify open orders after add_order failure: %s", check_exc)
        raise


def cancel_order(txid: str) -> dict:
    return _private("CancelOrder", {"txid": txid})


def get_open_orders() -> dict:
    return _private("OpenOrders")


def get_closed_orders() -> dict:
    return _private("ClosedOrders")


def query_orders(txids: list[str]) -> dict:
    return _private("QueryOrders", {"txid": ",".join(txids), "trades": False})
