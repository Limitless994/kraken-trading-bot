"""
analyzer.py — Fetches live OHLC data from Kraken and computes technical indicators.

All calculations are pure Python (no external math libs needed).
Returns an Analysis dataclass or None on failure.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from . import config, kraken_client, market_data

logger = logging.getLogger(__name__)

# Cache OHLC per intervalli lenti (4h e daily cambiano raramente)
_ohlc_cache: dict = {}
_OHLC_TTL = {240: 1800, 1440: 7200}   # 4h: 30 min  |  daily: 2 ore


def _get_ohlc(pair: str, interval: int) -> list:
    key = f"{pair}_{interval}"
    ttl = _OHLC_TTL.get(interval, 0)
    if ttl:
        now = time.time()
        if key in _ohlc_cache and now - _ohlc_cache[key]["ts"] < ttl:
            return _ohlc_cache[key]["data"]
    data = kraken_client.get_ohlc(pair, interval=interval)
    if ttl:
        _ohlc_cache[key] = {"ts": time.time(), "data": data}
    return data


# ── Data container ────────────────────────────────────────────────────────────

@dataclass
class Analysis:
    pair: str
    price: float
    high_24h: float
    low_24h: float
    volume_24h: float

    rsi_1h: float
    macd_hist: float
    macd_hist_prev: float
    ema20: float
    ema50: float
    volume_ratio: float
    rsi_15m: float
    adx: float
    atr: float
    ob_imbalance: Optional[float]

    # Multi-timeframe
    rsi_4h: float = 50.0
    rsi_daily: float = 50.0
    daily_above_ema50: bool = True
    price_change_24h: float = 0.0

    candles_1h: list = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)

    @property
    def macd_hist_slope(self) -> float:
        return self.macd_hist - self.macd_hist_prev

    @property
    def price_above_ema50(self) -> bool:
        return self.price >= self.ema50

    @property
    def price_above_ema20(self) -> bool:
        return self.price >= self.ema20

    @property
    def trend_strong(self) -> bool:
        return self.adx >= config.ADX_MIN

    @property
    def ob_bullish(self) -> bool:
        return self.ob_imbalance is not None and self.ob_imbalance >= 0.55

    @property
    def ob_bearish(self) -> bool:
        return self.ob_imbalance is not None and self.ob_imbalance <= 0.45


# ── Pure-Python indicator functions ──────────────────────────────────────────

def _rsi(closes: list, period: int = 14) -> float:
    if len(closes) < period + 2:
        return 50.0
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains  = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0.0:
        return 100.0
    return 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))


def _ema(values: list, period: int) -> list:
    if not values:
        return []
    k = 2.0 / (period + 1)
    result = [values[0]]
    for v in values[1:]:
        result.append(v * k + result[-1] * (1.0 - k))
    return result


def _macd_histogram(closes: list, fast: int = 12, slow: int = 26, signal: int = 9) -> list:
    if len(closes) < slow + signal:
        return [0.0, 0.0]
    ema_fast    = _ema(closes, fast)
    ema_slow    = _ema(closes, slow)
    macd_line   = [f - s for f, s in zip(ema_fast, ema_slow)]
    signal_line = _ema(macd_line, signal)
    return [m - s for m, s in zip(macd_line, signal_line)]


def _volume_ratio(volumes: list, period: int = 20) -> float:
    if len(volumes) < period + 1:
        return 1.0
    avg = sum(volumes[-period - 1:-1]) / period
    return (volumes[-1] / avg) if avg > 0 else 1.0


def _adx(highs: list, lows: list, closes: list, period: int = 14) -> float:
    if len(closes) < period * 2 + 1:
        return 20.0

    def wilder(values: list, p: int) -> list:
        if len(values) < p:
            return [0.0]
        result = [sum(values[:p]) / p]
        for v in values[p:]:
            result.append((result[-1] * (p - 1) + v) / p)
        return result

    trs, dmp, dmn = [], [], []
    for i in range(1, len(closes)):
        tr   = max(highs[i] - lows[i],
                   abs(highs[i] - closes[i - 1]),
                   abs(lows[i]  - closes[i - 1]))
        up   = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        trs.append(tr)
        dmp.append(max(up, 0.0)   if up > down  else 0.0)
        dmn.append(max(down, 0.0) if down > up  else 0.0)

    atr  = wilder(trs, period)
    sdmp = wilder(dmp, period)
    sdmn = wilder(dmn, period)

    dx_vals = []
    for a, p, n in zip(atr, sdmp, sdmn):
        if a == 0:
            continue
        pdi = 100 * p / a
        ndi = 100 * n / a
        denom = pdi + ndi
        if denom == 0:
            continue
        dx_vals.append(100 * abs(pdi - ndi) / denom)

    if len(dx_vals) < period:
        return 20.0

    adx_vals = wilder(dx_vals, period)
    return round(adx_vals[-1], 2) if adx_vals else 20.0


def _atr(highs: list, lows: list, closes: list, period: int = 14) -> float:
    """
    Average True Range — misura la volatilità assoluta del prezzo.
    Usato per TP/SL adattivi invece di percentuali fisse.
    """
    if len(closes) < period + 2:
        return (max(highs[-period:]) - min(lows[-period:])) / period if len(highs) >= period else highs[-1] - lows[-1]

    trs = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i]  - closes[i - 1]),
        )
        trs.append(tr)

    atr_val = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr_val = (atr_val * (period - 1) + tr) / period
    return atr_val


# ── Main analysis function ────────────────────────────────────────────────────

def analyze(pair: str) -> Optional[Analysis]:
    try:
        ticker = kraken_client.get_ticker(pair)
        time.sleep(0.6)

        candles_1h = kraken_client.get_ohlc(pair, interval=60)
        time.sleep(0.6)

        candles_15m = kraken_client.get_ohlc(pair, interval=15)

        if len(candles_1h) < config.EMA_SLOW + config.MACD_SIGNAL + 5:
            logger.warning("%s: only %d 1h candles — skipping", pair, len(candles_1h))
            return None

        closes_1h  = [c["close"]  for c in candles_1h]
        highs_1h   = [c["high"]   for c in candles_1h]
        lows_1h    = [c["low"]    for c in candles_1h]
        volumes_1h = [c["volume"] for c in candles_1h]

        rsi_1h = _rsi(closes_1h, config.RSI_PERIOD)

        hist        = _macd_histogram(closes_1h, config.MACD_FAST, config.MACD_SLOW, config.MACD_SIGNAL)
        macd_h      = hist[-1] if hist else 0.0
        macd_h_prev = hist[-2] if len(hist) >= 2 else 0.0

        ema20_series = _ema(closes_1h, config.EMA_FAST)
        ema50_series = _ema(closes_1h, config.EMA_SLOW)

        vol_ratio = _volume_ratio(volumes_1h)
        adx_val   = _adx(highs_1h, lows_1h, closes_1h, config.ADX_PERIOD)
        atr_val   = _atr(highs_1h, lows_1h, closes_1h, config.ATR_PERIOD)

        if len(candles_15m) >= config.RSI_PERIOD + 2:
            rsi_15m = _rsi([c["close"] for c in candles_15m], config.RSI_PERIOD)
        else:
            rsi_15m = rsi_1h

        # ── Multi-timeframe: 4h e daily (cached) ─────────────────────────────
        rsi_4h = rsi_1h
        try:
            candles_4h = _get_ohlc(pair, 240)
            if len(candles_4h) >= config.RSI_PERIOD + 2:
                rsi_4h = round(_rsi([c["close"] for c in candles_4h], config.RSI_PERIOD), 2)
        except Exception:
            pass

        rsi_daily = rsi_1h
        daily_above_ema50 = True
        price_change_24h  = 0.0
        try:
            candles_daily = _get_ohlc(pair, 1440)
            if len(candles_daily) >= config.EMA_SLOW + 2:
                closes_daily  = [c["close"] for c in candles_daily]
                rsi_daily     = round(_rsi(closes_daily, config.RSI_PERIOD), 2)
                ema50_daily   = _ema(closes_daily, config.EMA_SLOW)
                daily_above_ema50 = closes_daily[-1] >= ema50_daily[-1] if ema50_daily else True
                if len(closes_daily) >= 2:
                    price_change_24h = round(
                        (closes_daily[-1] - closes_daily[-2]) / closes_daily[-2] * 100, 2
                    )
        except Exception:
            pass

        # Order book imbalance
        ob = market_data.get_order_book_imbalance(pair)

        return Analysis(
            pair=pair,
            price=ticker["last"],
            high_24h=ticker["high_24h"],
            low_24h=ticker["low_24h"],
            volume_24h=ticker["volume_24h"],
            rsi_1h=round(rsi_1h, 2),
            macd_hist=macd_h,
            macd_hist_prev=macd_h_prev,
            ema20=ema20_series[-1] if ema20_series else ticker["last"],
            ema50=ema50_series[-1] if ema50_series else ticker["last"],
            volume_ratio=round(vol_ratio, 3),
            rsi_15m=round(rsi_15m, 2),
            adx=adx_val,
            atr=round(atr_val, 8),
            ob_imbalance=ob,
            rsi_4h=rsi_4h,
            rsi_daily=rsi_daily,
            daily_above_ema50=daily_above_ema50,
            price_change_24h=price_change_24h,
            candles_1h=candles_1h[-60:],
        )

    except Exception as exc:
        logger.error("analyze(%s) failed: %s", pair, exc)
        return None
