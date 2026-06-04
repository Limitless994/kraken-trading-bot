"""
market_data.py — External market context: Fear & Greed, funding rates, order book.

All APIs are free and require no authentication.
Results are cached for 15 minutes to avoid redundant calls.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

import requests

from . import config, kraken_client

logger = logging.getLogger(__name__)

_cache: dict = {}
_TTL = 900   # 15 minutes


def _cached(key: str, fetch_fn):
    now = time.time()
    if key in _cache and now - _cache[key]["ts"] < _TTL:
        return _cache[key]["data"]
    data = fetch_fn()
    if data is not None:
        _cache[key] = {"ts": now, "data": data}
    return data


# ── Fear & Greed Index ────────────────────────────────────────────────────────

def get_fear_greed() -> Optional[dict]:
    """
    Returns {"value": int 0-100, "label": str}.
    0-24  = Extreme Fear   → blocca longs
    25-49 = Fear           → reduce size
    50-74 = Greed          → neutral
    75-100= Extreme Greed  → blocks longs (overheated market)
    """
    def fetch():
        try:
            r = requests.get(
                "https://api.alternative.me/fng/?limit=1", timeout=4
            )
            d = r.json()["data"][0]
            return {"value": int(d["value"]), "label": d["value_classification"]}
        except Exception as exc:
            logger.warning("Fear&Greed fetch failed: %s", exc)
            return None
    return _cached("fear_greed", fetch)


# ── Funding Rates (Binance perpetuals) ───────────────────────────────────────

def get_funding_rates() -> Optional[dict]:
    """
    Returns {"BTCUSDT": float, "ETHUSDT": float}.
    > +0.001  (0.1%): crowded longs → long squeeze risk
    < -0.0005 (-0.05%): crowded shorts → favorable for spot longs
    """
    def fetch():
        rates = {}
        for symbol in ["BTCUSDT", "ETHUSDT"]:
            try:
                r = requests.get(
                    f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={symbol}",
                    timeout=4,
                )
                rates[symbol] = float(r.json()["lastFundingRate"])
                time.sleep(0.2)
            except Exception as exc:
                logger.warning("Funding rate %s failed: %s", symbol, exc)
        return rates if rates else None
    return _cached("funding_rates", fetch)


# ── Order Book Imbalance (Kraken) ─────────────────────────────────────────────

def get_order_book_imbalance(pair: str) -> Optional[float]:
    """
    Returns bid / (bid + ask) volume ratio for top 10 levels.
    > 0.60 = buy pressure (bullish)
    < 0.40 = sell pressure (bearish)
    ~0.50  = neutral
    """
    try:
        result = kraken_client._public("Depth", {"pair": pair, "count": 10})
        key    = next(iter(result))
        bids   = sum(float(b[1]) for b in result[key]["bids"])
        asks   = sum(float(a[1]) for a in result[key]["asks"])
        total  = bids + asks
        return round(bids / total, 3) if total > 0 else None
    except Exception as exc:
        logger.warning("Order book %s failed: %s", pair, exc)
        return None


# ── Crypto News (CoinGecko free) ─────────────────────────────────────────────

def get_crypto_news(limit: int = 4) -> list:
    """
    Returns latest crypto news headlines from CoinGecko.
    Free API, no authentication required. Cached 1 hour.
    """
    def fetch():
        try:
            r = requests.get(
                "https://api.coingecko.com/api/v3/news",
                params={"per_page": limit},
                headers={"Accept": "application/json"},
                timeout=4,
            )
            items = r.json().get("data", [])
            return [
                {"title": item.get("title", ""), "url": item.get("url", "")}
                for item in items[:limit]
                if item.get("title")
            ]
        except Exception as exc:
            logger.warning("News fetch failed: %s", exc)
            return None
    return _cached("news", fetch) or []


# ── On-chain data ─────────────────────────────────────────────────────────────

def get_btc_mempool() -> Optional[dict]:
    """
    BTC mempool stats from mempool.space (free, no auth).
    High fee rate = high activity = bullish congestion.
    High pending tx count = accumulation of demand.
    """
    def fetch():
        try:
            r = requests.get("https://mempool.space/api/v1/fees/recommended", timeout=4)
            fees = r.json()
            r2   = requests.get("https://mempool.space/api/mempool", timeout=4)
            pool = r2.json()
            return {
                "fee_fastest":  fees.get("fastestFee", 0),   # sat/vB
                "fee_hour":     fees.get("hourFee", 0),
                "tx_count":     pool.get("count", 0),
                "size_mb":      round(pool.get("vsize", 0) / 1_000_000, 2),
            }
        except Exception as exc:
            logger.warning("Mempool fetch failed: %s", exc)
            return None
    return _cached("mempool", fetch)


def get_exchange_inflow_proxy() -> Optional[dict]:
    """
    Proxy for exchange inflow using CoinGecko exchange volume data (free).
    High centralized exchange volume vs total = potential selling pressure.
    """
    def fetch():
        try:
            # BTC volume on top exchanges vs total market volume
            r = requests.get(
                "https://api.coingecko.com/api/v3/coins/bitcoin",
                params={"localization": "false", "tickers": "false",
                        "market_data": "true", "community_data": "false"},
                timeout=4,
            )
            data = r.json().get("market_data", {})
            total_vol = data.get("total_volume", {}).get("eur", 0)
            mcap      = data.get("market_cap", {}).get("eur", 1)
            vol_ratio = total_vol / mcap if mcap > 0 else 0
            return {
                "btc_volume_eur":    total_vol,
                "vol_mcap_ratio":    round(vol_ratio, 4),
                "high_activity":     vol_ratio > 0.05,   # >5% vol/mcap = high activity
            }
        except Exception as exc:
            logger.warning("Exchange inflow proxy failed: %s", exc)
            return None
    return _cached("exchange_inflow", fetch)


# ── Consolidated context ──────────────────────────────────────────────────────

def get_market_context() -> dict:
    """Single call that returns all market context for Groq evaluation."""
    fg      = get_fear_greed()
    fr      = get_funding_rates()
    news    = get_crypto_news() if config.NEWS_ENABLED else []
    mempool = get_btc_mempool() if config.ONCHAIN_ENABLED else None
    inflow  = get_exchange_inflow_proxy() if config.ONCHAIN_ENABLED else None
    return {
        "fear_greed_value": fg["value"]  if fg else None,
        "fear_greed_label": fg["label"]  if fg else None,
        "btc_funding":      fr.get("BTCUSDT") if fr else None,
        "eth_funding":      fr.get("ETHUSDT") if fr else None,
        "news_headlines":   [n["title"] for n in news],
        "mempool":          mempool,
        "exchange_inflow":  inflow,
    }


def fear_greed_blocks_trading(fg_value: Optional[int]) -> bool:
    """Blocks LONG trades: extreme fear (low) or extreme greed (high)."""
    if fg_value is None:
        return False
    return fg_value < config.FEAR_GREED_MIN or fg_value > config.FEAR_GREED_MAX


def fear_greed_blocks_short(fg_value: Optional[int]) -> bool:
    """Blocks SHORT trades in extreme greed (overheating) and extreme fear (capitulation bounce risk)."""
    if fg_value is None:
        return False
    return fg_value > config.FEAR_GREED_MAX or fg_value < config.FEAR_GREED_SHORT_MIN


def funding_rate_warning(btc_rate: Optional[float]) -> bool:
    """Returns True if BTC funding rate signals crowded longs."""
    if btc_rate is None:
        return False
    return btc_rate > 0.001   # > 0.1% = heavily crowded longs
