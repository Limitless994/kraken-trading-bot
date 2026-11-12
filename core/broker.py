"""
broker.py — Market access behind one small interface.

The trend engine (core/trend_engine.py) talks only to a Broker, so the same
strategy code runs on crypto (Kraken) and on US stocks/ETFs (Alpaca) without
knowing which one it is. The legacy 1h momentum path still calls kraken_client
directly and is not routed through here.

    daily_candles(symbol)  COMPLETED daily bars, oldest first (forming bar excluded)
    quote(symbol)          {"bid", "ask", "last"}
    is_market_open()       crypto: always True; stocks: exchange session
    cash()                 free cash in the broker's currency
    market_buy / market_sell(symbol, qty) -> order id
    order_fill(order_id)   Fill(status, price, qty)

AlpacaBroker is PAPER ONLY by construction: it refuses any trading host other
than Alpaca's paper endpoint, so no configuration mistake can route an order to
a live account.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

import requests

from . import config, kraken_client

logger = logging.getLogger(__name__)

# Fill statuses returned by order_fill()
FILLED  = "filled"    # fully executed: price/qty are final
OPEN    = "open"      # accepted, not (fully) executed yet — check again later
DEAD    = "dead"      # canceled / expired / rejected — nothing was traded
UNKNOWN = "unknown"   # the broker could not be asked — check again later


@dataclass
class Fill:
    status: str
    price: float = 0.0
    qty: float = 0.0


class BrokerError(RuntimeError):
    """A broker call failed in a way the caller must not paper over."""


class Broker:
    """Interface. Subclasses implement every method below."""
    name: str = "broker"
    currency: str = "EUR"
    fee_rate: float = 0.0          # taker fee per side, as a fraction of notional
    min_order: float = 10.0        # smallest order worth sending, in `currency`

    def daily_candles(self, symbol: str) -> list:
        raise NotImplementedError

    def quote(self, symbol: str) -> dict:
        raise NotImplementedError

    def is_market_open(self) -> bool:
        raise NotImplementedError

    def cash(self) -> float:
        raise NotImplementedError

    def market_buy(self, symbol: str, qty: float) -> str:
        raise NotImplementedError

    def market_sell(self, symbol: str, qty: float) -> str:
        raise NotImplementedError

    def order_fill(self, order_id: str, wait_s: float = 5.0) -> Fill:
        raise NotImplementedError

    def round_qty(self, symbol: str, qty: float) -> float:
        return qty


# ── Kraken (crypto, EUR) ──────────────────────────────────────────────────────

class KrakenBroker(Broker):
    name = "kraken"
    currency = "EUR"
    min_order = 10.0

    # Refetch daily candles at most this often while waiting for a new close.
    _REFRESH_SEC = 600

    def __init__(self) -> None:
        self.fee_rate = config.KRAKEN_TAKER_FEE
        self._daily: dict = {}        # symbol -> (fetched_ts, candles)
        self._lock = threading.Lock()

    def daily_candles(self, symbol: str) -> list:
        """Completed daily candles (kraken_client.get_ohlc drops the forming one).

        Cached, but never across a daily close: once yesterday's candle should
        exist and the cached series does not end with it, refetch (throttled).
        The shared 4h OHLC cache would deliver a new daily close up to 4h late.
        """
        now = time.time()
        last_close_open_ts = (int(now) // 86400 - 1) * 86400   # open time of the last completed day
        with self._lock:
            cached = self._daily.get(symbol)
        if cached:
            fetched_ts, candles = cached
            up_to_date = bool(candles) and candles[-1]["time"] >= last_close_open_ts
            if up_to_date or now - fetched_ts < self._REFRESH_SEC:
                return candles
        candles = kraken_client.get_ohlc(symbol, interval=1440)
        with self._lock:
            self._daily[symbol] = (now, candles)
        return candles

    def quote(self, symbol: str) -> dict:
        t = kraken_client.get_ticker(symbol)
        return {"bid": float(t["bid"]), "ask": float(t["ask"]), "last": float(t["last"])}

    def is_market_open(self) -> bool:
        return True

    def cash(self) -> float:
        bal = kraken_client.get_balance()
        return float(bal.get("ZEUR", bal.get("EUR", 0.0)))

    def round_qty(self, symbol: str, qty: float) -> float:
        return float(kraken_client.format_volume(symbol, qty))

    def _order(self, symbol: str, side: str, qty: float) -> str:
        order = kraken_client.add_order(
            symbol, side, "market", kraken_client.format_volume(symbol, qty),
        )
        txids = order.get("txid", [])
        if not txids:
            raise BrokerError(f"Kraken non ha restituito un txid per {side} {symbol}")
        return txids[0]

    def market_buy(self, symbol: str, qty: float) -> str:
        return self._order(symbol, "buy", qty)

    def market_sell(self, symbol: str, qty: float) -> str:
        return self._order(symbol, "sell", qty)

    def order_fill(self, order_id: str, wait_s: float = 5.0) -> Fill:
        deadline = time.time() + wait_s
        last = Fill(UNKNOWN)
        while True:
            try:
                order = kraken_client.query_orders([order_id]).get(order_id, {})
                status = order.get("status")
                vol_exec = float(order.get("vol_exec", 0) or 0)
                price = float(order.get("price", 0) or 0)
                if status == "closed" and vol_exec > 0 and price > 0:
                    return Fill(FILLED, price, vol_exec)
                if status in ("canceled", "expired"):
                    # A canceled order may still have partially executed.
                    if vol_exec > 0 and price > 0:
                        return Fill(FILLED, price, vol_exec)
                    return Fill(DEAD)
                last = Fill(OPEN) if status else Fill(UNKNOWN)
            except Exception as exc:
                logger.debug("order_fill(%s) Kraken: %s", order_id, exc)
                last = Fill(UNKNOWN)
            if time.time() >= deadline:
                return last
            time.sleep(0.5)


# ── Alpaca (US stocks/ETFs, USD) — paper only ─────────────────────────────────

ALPACA_PAPER_URL = "https://paper-api.alpaca.markets"
ALPACA_DATA_URL  = "https://data.alpaca.markets"


class AlpacaBroker(Broker):
    name = "alpaca-paper"
    currency = "USD"
    fee_rate = 0.0        # commission-free; spread/slippage is modelled by the engine
    min_order = 1.0       # Alpaca's minimum for fractional/notional orders

    _BARS_TTL  = 900      # daily bars: refetch every 15 min
    _CLOCK_TTL = 60
    _TIMEOUT   = 15

    def __init__(self, key: str = None, secret: str = None,
                 trading_url: str = ALPACA_PAPER_URL, feed: str = None,
                 session: requests.Session = None) -> None:
        key = key if key is not None else config.ALPACA_API_KEY
        secret = secret if secret is not None else config.ALPACA_API_SECRET
        if not key or not secret:
            raise BrokerError("ALPACA_API_KEY / ALPACA_API_SECRET mancanti nel .env")
        if trading_url.rstrip("/") != ALPACA_PAPER_URL:
            # Hard stop, not a warning: this class must never reach a live account.
            raise BrokerError(
                f"AlpacaBroker è solo paper: host di trading rifiutato ({trading_url})"
            )
        self._trading_url = ALPACA_PAPER_URL
        self._feed = feed or config.ALPACA_DATA_FEED
        self._http = session or requests.Session()
        self._headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
        self._bars: dict = {}     # symbol -> (fetched_ts, candles)
        self._clock: tuple = (0.0, None)
        self._lock = threading.Lock()

    # -- HTTP ------------------------------------------------------------------

    def _request(self, method: str, url: str, **kw) -> dict:
        try:
            resp = self._http.request(method, url, headers=self._headers,
                                      timeout=self._TIMEOUT, **kw)
        except requests.RequestException as exc:
            raise BrokerError(f"Alpaca non raggiungibile: {exc}") from exc
        if resp.status_code >= 400:
            raise BrokerError(f"Alpaca HTTP {resp.status_code}: {resp.text[:200]}")
        return resp.json()

    # -- Market state ----------------------------------------------------------

    def _get_clock(self) -> dict:
        now = time.time()
        with self._lock:
            ts, clock = self._clock
        if clock is not None and now - ts < self._CLOCK_TTL:
            return clock
        clock = self._request("GET", f"{self._trading_url}/v2/clock")
        with self._lock:
            self._clock = (now, clock)
        return clock

    def is_market_open(self) -> bool:
        return bool(self._get_clock().get("is_open"))

    # -- Data ------------------------------------------------------------------

    def daily_candles(self, symbol: str) -> list:
        """Completed daily bars, split/dividend adjusted, oldest first.

        Today's bar is still forming until the session has closed, so it is
        dropped until then; after the close it is a completed bar. "Today"
        is read from the exchange clock's own timestamp (New York time), so no
        timezone database is needed on the host.
        """
        now = time.time()
        with self._lock:
            cached = self._bars.get(symbol)
        if cached and now - cached[0] < self._BARS_TTL:
            raw = cached[1]
        else:
            # ~450 calendar days ≈ 310 sessions: enough for EMA50 + the 40d lookback.
            raw = self._fetch_bars(symbol, time.strftime("%Y-%m-%d", time.gmtime(now - 450 * 86400)))
            with self._lock:
                self._bars[symbol] = (now, raw)

        clock = self._get_clock()
        today_ny = str(clock.get("timestamp", ""))[:10]
        candles = self._to_candles(raw)
        # Today's session is unfinished while the market is open, and also before
        # the open (pre-market prints): next_open still falls on today then.
        session_pending = bool(clock.get("is_open")) or str(clock.get("next_open", ""))[:10] == today_ny
        if candles and session_pending and candles[-1]["date"] == today_ny:
            candles = candles[:-1]
        return candles

    def _fetch_bars(self, symbol: str, start: str) -> list:
        """Raw daily bars from `start` (YYYY-MM-DD), split/dividend adjusted."""
        raw, token = [], None
        while True:
            params = {"timeframe": "1Day", "start": start, "limit": 1000,
                      "adjustment": "all", "feed": self._feed, "sort": "asc"}
            if token:
                params["page_token"] = token
            data = self._request(
                "GET", f"{ALPACA_DATA_URL}/v2/stocks/{symbol}/bars", params=params,
            )
            raw.extend(data.get("bars") or [])
            token = data.get("next_page_token")
            if not token:
                return raw

    @staticmethod
    def _to_candles(raw: list) -> list:
        return [
            {"time": b["t"], "date": str(b["t"])[:10], "open": float(b["o"]),
             "high": float(b["h"]), "low": float(b["l"]), "close": float(b["c"]),
             "volume": float(b.get("v", 0))}
            for b in raw
        ]

    def history(self, symbol: str, start: str) -> list:
        """Daily candles from `start` for backtests (may include today's forming bar)."""
        return self._to_candles(self._fetch_bars(symbol, start))

    def quote(self, symbol: str) -> dict:
        data = self._request(
            "GET", f"{ALPACA_DATA_URL}/v2/stocks/{symbol}/trades/latest",
            params={"feed": self._feed},
        )
        last = float(data["trade"]["p"])
        # The free IEX feed often has wide or empty quotes; the last trade is the
        # dependable number, so bid/ask are the last price and the engine applies
        # its slippage estimate on top.
        return {"bid": last, "ask": last, "last": last}

    def cash(self) -> float:
        return float(self._request("GET", f"{self._trading_url}/v2/account").get("cash", 0))

    # -- Orders (paper account) ------------------------------------------------

    def round_qty(self, symbol: str, qty: float) -> float:
        return round(qty, 6)      # fractional shares

    def _order(self, symbol: str, side: str, qty: float) -> str:
        body = {"symbol": symbol, "qty": str(self.round_qty(symbol, qty)), "side": side,
                "type": "market", "time_in_force": "day"}   # fractional ⇒ market + day
        order = self._request("POST", f"{self._trading_url}/v2/orders", json=body)
        if not order.get("id"):
            raise BrokerError(f"Alpaca non ha restituito un id per {side} {symbol}")
        return order["id"]

    def market_buy(self, symbol: str, qty: float) -> str:
        return self._order(symbol, "buy", qty)

    def market_sell(self, symbol: str, qty: float) -> str:
        return self._order(symbol, "sell", qty)

    def order_fill(self, order_id: str, wait_s: float = 5.0) -> Fill:
        deadline = time.time() + wait_s
        last = Fill(UNKNOWN)
        while True:
            try:
                order = self._request("GET", f"{self._trading_url}/v2/orders/{order_id}")
                status = order.get("status")
                qty = float(order.get("filled_qty") or 0)
                price = float(order.get("filled_avg_price") or 0)
                if status == "filled" and qty > 0 and price > 0:
                    return Fill(FILLED, price, qty)
                if status in ("canceled", "expired", "rejected", "done_for_day"):
                    if qty > 0 and price > 0:      # partially executed before dying
                        return Fill(FILLED, price, qty)
                    return Fill(DEAD)
                last = Fill(OPEN)
            except BrokerError as exc:
                logger.debug("order_fill(%s) Alpaca: %s", order_id, exc)
                last = Fill(UNKNOWN)
            if time.time() >= deadline:
                return last
            time.sleep(0.5)
