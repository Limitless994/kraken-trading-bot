"""
regime.py — Market regime detection (Mod 4).

Classifies the market from BTC daily candles into one of four regimes and maps
each regime to per-side score bumps that raise entry thresholds where the
strategy historically bleeds (chop) and keeps them open where it wins (trends).

Classification (daily BTC):
  - volatile    ATR% ≥ VOLATILE_ATR_PCT — violent candles, whipsaw risk on both sides
  - trend_up    daily ADX ≥ TREND_ADX_MIN and price ≥ EMA50
  - trend_down  daily ADX ≥ TREND_ADX_MIN and price < EMA50
  - range       everything else — weak trend, mean-reverting chop

Score bumps (added to MIN_SIGNAL_SCORE_LONG/_SHORT by strategy.py):
  regime      bump_long  bump_short   rationale
  trend_up        0          +1       with-trend longs free, counter-trend shorts need more
  trend_down     +1           0       mirror
  range          +1          +1       chop kills both sides via fees
  volatile       +1          +1       stops get run over; demand stronger setups

The bot recomputes the regime each scan and stores it in settings for the
dashboard. Disable with REGIME_ENABLED=false.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from .analyzer import _adx, _atr, _ema

logger = logging.getLogger(__name__)

TREND_ADX_MIN    = 25.0    # daily ADX above this = real trend
VOLATILE_ATR_PCT = 0.05    # daily ATR ≥ 5% of price = extreme volatility

# regime → (bump_long, bump_short)
SCORE_BUMPS = {
    "trend_up":   (0, 1),
    "trend_down": (1, 0),
    "range":      (1, 1),
    "volatile":   (1, 1),
}


@dataclass
class Regime:
    label: str            # trend_up | trend_down | range | volatile
    adx_daily: float
    atr_pct: float        # daily ATR / price
    ema_dist_pct: float   # (price − EMA50_daily) / EMA50_daily
    bump_long: int
    bump_short: int


def classify(daily_candles: list) -> Regime | None:
    """Classify the regime from daily OHLC candles (dicts with high/low/close).

    Returns None when there is not enough history (< 60 candles) — callers
    should treat that as "no regime adjustment".
    """
    if not daily_candles or len(daily_candles) < 60:
        return None

    highs  = [c["high"]  for c in daily_candles]
    lows   = [c["low"]   for c in daily_candles]
    closes = [c["close"] for c in daily_candles]
    price  = closes[-1]
    if price <= 0:
        return None

    adx_d   = _adx(highs, lows, closes)
    atr_d   = _atr(highs, lows, closes)
    ema50_d = _ema(closes, 50)[-1]

    atr_pct      = atr_d / price
    ema_dist_pct = (price - ema50_d) / ema50_d if ema50_d > 0 else 0.0

    if atr_pct >= VOLATILE_ATR_PCT:
        label = "volatile"
    elif adx_d >= TREND_ADX_MIN:
        label = "trend_up" if price >= ema50_d else "trend_down"
    else:
        label = "range"

    bump_long, bump_short = SCORE_BUMPS[label]
    return Regime(
        label=label,
        adx_daily=round(adx_d, 1),
        atr_pct=round(atr_pct, 4),
        ema_dist_pct=round(ema_dist_pct, 4),
        bump_long=bump_long,
        bump_short=bump_short,
    )
